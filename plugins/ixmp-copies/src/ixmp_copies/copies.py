"""File-level copies of HyperSQL databases: backups, read-only seeds made from a backup, and
per-job copies of a seed that let cluster jobs solve in parallel.

A HyperSQL file database is a set of files beside one stem (db.properties, db.script, db.data,
db.lobs, ...) owned by the single process that has it open. A byte copy is a consistent
database only when that process has shut it down cleanly, so every copy here refuses a
database that is not provably closed, and checks the copy against the source read before and
after it (checked_copy). Nothing here opens a platform or starts a JVM: these functions only
read and write files, and are safe to run beside a solve on another database.

The chain is one-way: a live database -> backup -> seed -> job copies. Only `backup` reads a
live database; seeds are made from verified backups, jobs copy verified seeds, and nothing
ever writes into a backup or a seed. Each copy is a folder holding the database files and a
manifest.json with their sizes and SHA-256, which `verify` re-reads.

Refusals raise Refused, a copy that does not match its source raises CopyMismatch; the CLI
maps them to distinct exit codes.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import time
from pathlib import Path

from ixmp_copies import __version__
from ixmp_copies.config import Config, expand
from ixmp_copies.provenance import fingerprint, model_source

CHUNK = 1 << 22
# What a cleanly closed HyperSQL 2.5 database may consist of. Anything else beside the stem
# is a state this module does not understand, so it refuses rather than guess.
COPIED_SUFFIXES = (".properties", ".script", ".data", ".lobs", ".backup", ".log")
MANIFEST = "manifest.json"
PARTIAL = ".partial"
STEM = "db"
# A job copy runs one job; a main collects merged results and outlives the jobs.
COPY_KINDS = {"job": "jobs", "main": "mains"}
NAME = re.compile(r"^[a-z0-9][a-z0-9_]*$")
# GAMS scratch folders (225a, 225b, ...), listings, logs and GDX files are run output, not
# model source; a job's model folder starts without them.
MODEL_IGNORE = shutil.ignore_patterns("225*", "*.lst", "*.log", "*.gdx", "*.~*")


class Refused(RuntimeError):
    """The database or the destination is not in a state the operation may act on."""


class CopyMismatch(RuntimeError):
    """The copy differs from the source, or the source changed while it was copied."""


def reachable(root: Path) -> bool:
    # A CIFS mount whose server is unreachable (VPN down) raises OSError "Host is down"
    # rather than reporting an empty folder; either way there is no H drive to use.
    try:
        return root.is_dir() and any(root.iterdir())
    except OSError:
        return False


def hdrive_candidates(cfg: Config) -> list[Path]:
    return [Path(expand(r)) for r in cfg.roots]


def hdrive_root(cfg: Config) -> Path:
    for root in hdrive_candidates(cfg):
        if reachable(root):
            return root
    raise FileNotFoundError(f"no H drive mounted and reachable at any of "
                            f"{[str(r) for r in hdrive_candidates(cfg)]} (VPN down, or mount missing?)")


def area_root(area: str, cfg: Config, root: Path | None = None) -> Path:
    if area not in cfg.areas:
        raise Refused(f"unknown area {area!r}; known: {sorted(cfg.areas)}")
    return (root or hdrive_root(cfg)) / cfg.areas[area]


def backup_root_for(db: Path, cfg: Config, root: Path | None = None) -> Path:
    """Where a backup of `db` goes: <area>/backups for a database inside an area (a job copy
    or a results main), the project's backups folder otherwise, so trial backups never sit
    beside the real ones."""
    base = root or hdrive_root(cfg)
    for area in cfg.areas:
        area_dir = area_root(area, cfg, base).resolve()
        if area_dir in db.resolve().parents:
            return area_dir / "backups"
    return base / cfg.backups


def copy_label(db: Path, platform: str, cfg: Config, root: Path | None = None) -> str:
    """What a backup or merge of `db` is labelled with: the main's folder name for a database
    inside an area's mains/ (a results main, which a job reaches under the platform name
    through its own IXMP_DATA), `platform` otherwise. Labelled by the platform name, a main's
    records and backups would read as if they were about that platform's usual database."""
    base = root or hdrive_root(cfg)
    for area in cfg.areas:
        mains = area_root(area, cfg, base).resolve() / COPY_KINDS["main"]
        if mains in db.resolve().parents:
            parts = db.resolve().relative_to(mains).parts
            if len(parts) > 1:
                return parts[0]
    return platform


def require_job_result(job_dir: Path) -> dict:
    """The job's result.json, refusing unless the job closed its copy and the copy still
    hashes as it did then."""
    path = job_dir / "result.json"
    if not path.exists():
        raise Refused(f"{path} does not exist: the job did not close its copy (job-close)")
    result = json.loads(path.read_text())
    now = snapshot(database_files(job_dir / "db" / STEM))
    if now != result["files"]:
        raise Refused(f"{job_dir}/db changed after job-close: {result['files']} -> {now}")
    return result


def require_name(name: str) -> str:
    if not NAME.match(name):
        raise Refused(f"name {name!r} must match {NAME.pattern}")
    return name


def read_properties(db: Path) -> dict[str, str]:
    path = Path(f"{db}.properties")
    if not path.exists():
        raise Refused(f"{path} does not exist: not a HyperSQL database")
    pairs = (line.split("=", 1) for line in path.read_text().splitlines()
             if "=" in line and not line.startswith("#"))
    return {k.strip(): v.strip() for k, v in pairs}


def table_types(db: Path) -> dict[str, int]:
    text = Path(f"{db}.script").read_text(errors="replace")
    return {"cached": text.count("CREATE CACHED TABLE"), "memory": text.count("CREATE MEMORY TABLE")}


def require_cached_tables(db: Path) -> int:
    """Refuse a HyperSQL database whose tables are MEMORY tables: the whole database is then
    held in the JVM and reloaded at every open, which slows a local platform to a crawl after
    a few solved scenarios. A database created with `hsqldb.default_table_type=cached` in its
    url has only CACHED tables. Returns the CACHED count; raises FileNotFoundError before the
    database exists, ValueError on a MEMORY table or when the .script lists no table (a fresh
    database still open: check after closing)."""
    script = Path(f"{db}.script")
    if not script.exists():
        raise FileNotFoundError(f"{script} does not exist (the database has not been opened yet)")
    types = table_types(db)
    if types["memory"]:
        raise ValueError(f"{script}: {types['memory']} MEMORY tables; recreate it with "
                         "hsqldb.default_table_type=cached in the url")
    if not types["cached"]:
        # A database opened for the first time has its tables in the .log until it is
        # closed: no table in the .script proves nothing either way.
        raise ValueError(f"{script} holds no table definition yet; check after close_db()")
    return types["cached"]


def processes_holding(db: Path) -> list[str]:
    """'pid cmdline' of every process (readable to this user) with a file of `db` open.
    Sees this host only: across hosts, require_closed rests on HyperSQL's own markers."""
    prefix = f"{db.name}."
    holders = []
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return holders
    for proc in proc_root.iterdir():
        if not proc.name.isdigit():
            continue
        try:
            targets = [Path(os.readlink(fd)) for fd in (proc / "fd").iterdir()]
            cmd = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            continue
        if any(t.parent == db.parent and t.name.startswith(prefix) for t in targets):
            holders.append(f"{proc.name} {cmd.strip()[:160]}")
    return holders


def database_files(db: Path) -> list[Path]:
    """The files to copy; refuses anything beside the stem it does not recognise."""
    if not db.parent.is_dir():
        raise Refused(f"{db.parent} does not exist")
    beside = sorted(p for p in db.parent.iterdir() if p.name.startswith(f"{db.name}."))
    unknown = [p.name for p in beside
               if p.suffix not in COPIED_SUFFIXES + (".tmp", ".lck") or (p.is_dir() != (p.suffix == ".tmp"))]
    if unknown:
        raise Refused(f"unrecognised files beside {db}: {unknown}")
    return [p for p in beside if p.suffix in COPIED_SUFFIXES]


def require_closed(db: Path) -> None:
    """Refuse unless `db` is provably shut down cleanly and held by no process."""
    props = read_properties(db)
    problems = []
    if props.get("modified") != "no":
        problems.append(f"modified={props.get('modified')} in {db}.properties (open, or not shut down cleanly)")
    if Path(f"{db}.lck").exists():
        problems.append(f"{db}.lck exists (open, or left by a process that died)")
    log = Path(f"{db}.log")
    if log.exists() and log.stat().st_size:
        problems.append(f"{log} holds {log.stat().st_size} bytes of unwritten transactions")
    tmp = Path(f"{db}.tmp")
    if tmp.is_dir() and any(tmp.iterdir()):
        problems.append(f"{tmp} is not empty")
    if not Path(f"{db}.script").exists():
        problems.append(f"{db}.script does not exist")
    holders = processes_holding(db)
    if holders:
        problems.append("held open by: " + "; ".join(holders))
    if problems:
        raise Refused(f"{db} is not closed: " + " | ".join(problems))


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(CHUNK):
            h.update(block)
    return h.hexdigest()


def snapshot(files: list[Path]) -> dict[str, dict]:
    return {p.name: {"size": p.stat().st_size, "sha256": digest(p)} for p in files}


def _copy_file(src: Path, dst: Path) -> None:
    # fsync so the bytes are on the file server, not only in this host's cache, before the
    # copy is declared complete.
    with src.open("rb") as fin, dst.open("xb") as fout:
        while block := fin.read(CHUNK):
            fout.write(block)
        fout.flush()
        os.fsync(fout.fileno())


def write_new(path: Path, text: str, mode: int = 0o644) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def checked_copy(db: Path, dest: Path, meta: dict) -> dict:
    """Byte-copy the closed database `db` into the new folder `dest` (files keep their names)
    and return the manifest written there. The copy is built in a '.partial' sibling and
    renamed only after the source, read before and after the copy, and the copy itself hash
    the same, with the source still closed; otherwise CopyMismatch, and the '.partial' folder
    stays for a look."""
    require_closed(db)
    files = database_files(db)
    staging = dest.with_name(dest.name + PARTIAL)
    for path in (dest, staging):
        if path.exists():
            raise Refused(f"{path} already exists")
    before = snapshot(files)
    staging.mkdir(parents=True)
    for src in files:
        _copy_file(src, staging / src.name)
    copied = snapshot([staging / p.name for p in files])
    require_closed(db)
    after = snapshot(database_files(db))
    if not before == after == copied:
        raise CopyMismatch(f"{db}: source before {before}, source after {after}, copy {copied}; "
                           f"partial copy left at {staging}")
    manifest = {
        **meta, "source": str(db), "files": copied,
        "properties": read_properties(db), "tables": table_types(db),
        "host": socket.gethostname(), "user": getpass.getuser(),
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "tool": f"ixmp-copies {__version__}",
    }
    write_new(staging / MANIFEST, json.dumps(manifest, indent=2))
    staging.rename(dest)
    return manifest


def backup(db: Path, dest_root: Path, label: str) -> tuple[Path, dict]:
    """Copy the live database `db` to dest_root/label/<timestamp>/."""
    dest = dest_root / label / time.strftime("%Y%m%d_%H%M%S")
    return dest, checked_copy(db, dest, {"kind": "backup", "label": label})


def verify(folder: Path) -> list[str]:
    """Problems found re-reading a copy against its manifest; empty when it matches."""
    if folder.name.endswith(PARTIAL):
        return [f"{folder} is an incomplete copy"]
    path = folder / MANIFEST
    if not path.exists():
        return [f"{path} does not exist"]
    expected = json.loads(path.read_text())["files"]
    present = {p.name for p in folder.iterdir()} - {MANIFEST}
    problems = [f"missing {n}" for n in sorted(set(expected) - present)]
    problems += [f"unexpected {n}" for n in sorted(present - set(expected))]
    for name in sorted(set(expected) & present):
        p = folder / name
        got = {"size": p.stat().st_size, "sha256": digest(p)}
        if got != expected[name]:
            problems.append(f"{name}: manifest {expected[name]}, found {got}")
    return problems


def require_verified(folder: Path, kind: str) -> dict:
    """The manifest of `folder`, refusing unless it is a `kind` copy that matches it."""
    problems = verify(folder)
    if problems:
        raise Refused(f"{folder} does not match its manifest: {problems}")
    manifest = json.loads((folder / MANIFEST).read_text())
    if manifest.get("kind") != kind:
        raise Refused(f"{folder} is a {manifest.get('kind')!r} copy, not a {kind!r} one")
    return manifest


def make_read_only(folder: Path) -> bool:
    """Drop write permission on `folder` and its files; True if it took (CIFS ignores it)."""
    for p in folder.iterdir():
        p.chmod(p.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    folder.chmod(folder.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    return not os.access(folder, os.W_OK)


def _seedable(db: Path, origin: Path) -> None:
    try:
        require_cached_tables(db)
    except ValueError as err:
        raise Refused(f"{origin} cannot seed jobs: {err}") from err


def seed(backup_folder: Path, area: str, name: str, cfg: Config,
         root: Path | None = None) -> tuple[Path, dict, bool]:
    """A read-only seed <area>/seeds/<name>/ from a verified backup with CACHED tables.
    Returns its folder, manifest and whether read-only took."""
    source = require_verified(backup_folder, "backup")
    db = backup_folder / Path(source["source"]).name
    _seedable(db, backup_folder)
    dest = area_root(area, cfg, root) / "seeds" / require_name(name)
    manifest = checked_copy(db, dest, {"kind": "seed", "name": name, "from_backup": str(backup_folder)})
    return dest, manifest, make_read_only(dest)


def seed_from_job(job_dir: Path, area: str, name: str, cfg: Config,
                  root: Path | None = None) -> tuple[Path, dict, bool]:
    """A read-only seed <area>/seeds/<name>/ from a job copy the job closed (job-close), e.g.
    a solved scenario that later runs start from. Returns folder, manifest, read-only."""
    require_job_result(job_dir)
    db = job_dir / "db" / STEM
    _seedable(db, job_dir)
    dest = area_root(area, cfg, root) / "seeds" / require_name(name)
    manifest = checked_copy(db, dest, {"kind": "seed", "name": name, "from_job": str(job_dir)})
    return dest, manifest, make_read_only(dest)


def restore(backup_folder: Path, dest: Path, cfg: Config) -> dict:
    """A working database from a verified backup, in the new folder `dest` (never over an
    existing one, never on the H drive). Registering it with ixmp is the caller's step:
    nothing here edits the ixmp config."""
    source = require_verified(backup_folder, "backup")
    for base in hdrive_candidates(cfg):
        if reachable(base) and base.resolve() in dest.resolve().parents:
            raise Refused(f"{dest} is on the H drive: restore to a local disk")
    return checked_copy(backup_folder / Path(source["source"]).name, dest,
                        {"kind": "restore", "from_backup": str(backup_folder)})


def hsqldb_url(db: Path) -> str:
    return f"jdbc:hsqldb:file:{db};hsqldb.default_table_type=cached"


def job_copy(seed_folder: Path, job_dir: Path, platform: str, ixmp_config: dict,
             model_src: Path, area: str, cfg: Config, root: Path | None = None,
             kind: str = "job", message_ix_version: str | None = None) -> dict:
    """Set up one job's own copy of a seed in the new folder `job_dir`, below the area's jobs/
    (kind "job"), or a results main below its mains/ (kind "main", which merges go into):
    db/ (the database), model/ (the GAMS model source, without run output, so jobs do not
    share GDX files or cplex.opt) and ixmp/config.json, which maps `platform` to db/ and sets
    message_model_dir to model/. The job sets IXMP_DATA=<job_dir>/ixmp; no other platform in
    the config survives, so a job cannot reach any other database, ixmp-dev included.
    model_source.json records where model/ came from (path, git commit, fingerprint), checked
    against the copy, so a run can be traced to the GAMS source it solved with."""
    if kind not in COPY_KINDS:
        raise Refused(f"unknown copy kind {kind!r}; known: {sorted(COPY_KINDS)}")
    parent = area_root(area, cfg, root) / COPY_KINDS[kind]
    if job_dir.resolve().parent != parent.resolve():
        raise Refused(f"{job_dir} is not directly below {parent}")
    require_name(job_dir.name)
    if job_dir.exists():
        raise Refused(f"{job_dir} already exists")
    source = require_verified(seed_folder, "seed")
    if not model_src.is_dir():
        raise Refused(f"message model dir {model_src} does not exist")
    db_dir = job_dir / "db"
    job_dir.mkdir(parents=True)
    checked_copy(seed_folder / Path(source["source"]).name, db_dir,
                 {"kind": kind, "seed": str(seed_folder), "platform": platform})
    source = model_source(model_src, message_ix_version)
    shutil.copytree(model_src, job_dir / "model", ignore=MODEL_IGNORE)
    for sub in ("data", "output"):
        (job_dir / "model" / sub).mkdir(exist_ok=True)
    copied = fingerprint(job_dir / "model")
    if copied != source["fingerprint"]:
        raise CopyMismatch(f"{job_dir / 'model'} differs from {model_src} (did the source change during "
                           f"the copy?): {copied} vs {source['fingerprint']}")
    write_new(job_dir / "model_source.json", json.dumps(source, indent=2))
    url = hsqldb_url(db_dir / STEM)
    config = {k: v for k, v in ixmp_config.items() if k != "platform"}
    config["platform"] = {"default": platform, platform: {"class": "jdbc", "driver": "hsqldb", "url": url}}
    config["message_model_dir"] = str(job_dir / "model")
    (job_dir / "ixmp").mkdir()
    write_new(job_dir / "ixmp" / "config.json", json.dumps(config, indent=2), mode=0o600)
    return {"job_dir": str(job_dir), "IXMP_DATA": str(job_dir / "ixmp"), "url": url, "model_source": source}


def job_close(job_dir: Path) -> dict:
    """After the job's last platform process: refuse unless its database is closed, then
    record the solved database's checksums in result.json so a merge can tell it is intact."""
    db = job_dir / "db" / STEM
    require_closed(db)
    if (job_dir / "result.json").exists():
        raise Refused(f"{job_dir / 'result.json'} already exists")
    result = {"kind": "job-result", "files": snapshot(database_files(db)),
              "properties": read_properties(db), "host": socket.gethostname(),
              "closed": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    write_new(job_dir / "result.json", json.dumps(result, indent=2))
    return result
