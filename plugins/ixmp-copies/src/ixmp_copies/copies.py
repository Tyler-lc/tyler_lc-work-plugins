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

import fnmatch
import getpass
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
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
# A job that merges several scenarios lists them here, so cleanup can tell one that failed.
EXPECTED_MERGES = "expected_merges.txt"
# What a run job records about its scenario before and after its command (run-mark): the
# evidence a merge needs that the version it brings back is the run's result.
RUN_BEFORE = "run_before.json"
RUN_RESULT = "run_result.json"
# A job copy made only to merge scenarios from a seed names that seed here; its merges are
# marked by the database the seed's versions came from, so resubmitting them, or merging the
# same version through a newer seed, cannot land a scenario twice.
SEED_MERGE = "seed_merge.txt"
# Size and SHA-256 of every file of a run job's code copy, taken right after the copy: what the
# run wrote there later (new or changed files) is output that cleanup must not delete unseen.
CODE_FILES = "code_files.json"
# What a job folder holds besides output a run wrote into it.
JOB_PARTS = {"db", "model", "ixmp", "code", "tmp", "result.json", "model_source.json", EXPECTED_MERGES,
             RUN_BEFORE, RUN_RESULT, SEED_MERGE, CODE_FILES}
# GAMS scratch folders (225a, 225b, ...), listings, logs and GDX files are run output, not
# model source; a job's model folder starts without them.
MODEL_IGNORE = shutil.ignore_patterns("225*", "*.lst", "*.log", "*.gdx", "*.~*")
# What a solve leaves in a job's model folder that may be the only copy of something: GDX files
# (model/data, and model/output, which holds the equation duals), listings and GAMS scratch.
MODEL_OUTPUTS = ("*.gdx", "*.lst", "225*")


class Refused(RuntimeError):
    """The database or the destination is not in a state the operation may act on."""


class CopyMismatch(RuntimeError):
    """The copy differs from the source, or the source changed while it was copied."""


PROBE_SECONDS = 10


def reachable(root: Path) -> bool:
    """True when `root` is a folder with something in it, answered within PROBE_SECONDS. An
    unmounted mount point is an empty folder; a CIFS mount whose server is gone (VPN down) hangs
    or raises "Host is down". The probe runs in a child process, so a hang costs the timeout and
    is never waited for."""
    probe = subprocess.Popen(["sh", "-c", 'test -d "$1" && [ -n "$(ls -A "$1" | head -c 1)" ]', "sh", str(root)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        return probe.wait(timeout=PROBE_SECONDS) == 0
    except subprocess.TimeoutExpired:
        probe.kill()
        return False


def hdrive_candidates(cfg: Config) -> list[Path]:
    return [Path(expand(r, cfg.cluster_user)) for r in cfg.roots]


def hdrive_root(cfg: Config) -> Path:
    for root in hdrive_candidates(cfg):
        if reachable(root):
            return root
    raise Refused(f"no H drive mounted and reachable at any of {[str(r) for r in hdrive_candidates(cfg)]}: "
                  "connect the VPN and mount the share (on WSL after every reboot or VPN drop), "
                  "or fix [storage] roots")


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


def _writable(proc: Path, fd: Path) -> bool:
    # fdinfo's "flags" is octal; the access mode is its lowest two bits (0 = read-only).
    try:
        flags = next(ln for ln in (proc / "fdinfo" / fd.name).read_text().splitlines() if ln.startswith("flags:"))
    except (OSError, StopIteration):
        return True
    return int(flags.split()[1], 8) & 0o3 != 0


def processes_holding(db: Path) -> list[str]:
    """'pid cmdline' of every process (readable to this user) with a file of `db` open for
    writing. Readers are no risk to a copy, and two jobs copying one read-only seed are each
    other's readers. Paths are compared resolved: the kernel reports the real path, while `db`
    may be spelled through a symlink (on UniCC ~/hdrive points to /hdrive/...). Sees this host
    only: across hosts, require_closed rests on HyperSQL's own markers."""
    prefix = f"{db.name}."
    parent = db.parent.resolve()
    holders = []
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return holders
    for proc in proc_root.iterdir():
        if not proc.name.isdigit():
            continue
        try:
            fds = [(fd, Path(os.readlink(fd))) for fd in (proc / "fd").iterdir()]
            cmd = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            continue
        if any(t.parent == parent and t.name.startswith(prefix) and _writable(proc, fd) for fd, t in fds):
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
    """A read-only seed <area>/seeds/<name>/ from a verified backup with CACHED tables. Its
    manifest's `origin` is the database that was backed up: where the seed's versions come from.
    Returns its folder, manifest and whether read-only took."""
    source = require_verified(backup_folder, "backup")
    db = backup_folder / Path(source["source"]).name
    _seedable(db, backup_folder)
    dest = area_root(area, cfg, root) / "seeds" / require_name(name)
    manifest = checked_copy(db, dest, {"kind": "seed", "name": name, "from_backup": str(backup_folder),
                                       "origin": source["source"]})
    return dest, manifest, make_read_only(dest)


def seed_from_job(job_dir: Path, area: str, name: str, cfg: Config,
                  root: Path | None = None) -> tuple[Path, dict, bool]:
    """A read-only seed <area>/seeds/<name>/ from a job copy the job closed (job-close), e.g.
    a solved scenario that later runs start from; its `origin` is the job folder. Returns
    folder, manifest, read-only."""
    require_job_result(job_dir)
    db = job_dir / "db" / STEM
    _seedable(db, job_dir)
    dest = area_root(area, cfg, root) / "seeds" / require_name(name)
    manifest = checked_copy(db, dest, {"kind": "seed", "name": name, "from_job": str(job_dir),
                                       "origin": str(job_dir.resolve())})
    return dest, manifest, make_read_only(dest)


def require_restore_dest(dest: Path, cfg: Config) -> None:
    """A restore goes to a new folder on a local disk: refuse one that exists or is on the share."""
    for path in (dest, dest.with_name(dest.name + PARTIAL)):
        if path.exists():
            raise Refused(f"{path} already exists")
    for base in hdrive_candidates(cfg):
        if reachable(base) and base.resolve() in dest.resolve().parents:
            raise Refused(f"{dest} is on the H drive: restore to a local disk")


def restore(backup_folder: Path, dest: Path, cfg: Config) -> dict:
    """A working database from a verified backup, in the new folder `dest` (never over an
    existing one, never on the H drive). Registering it with ixmp is the caller's step:
    nothing here edits the ixmp config."""
    source = require_verified(backup_folder, "backup")
    require_restore_dest(dest, cfg)
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


def code_files(code_dir: Path) -> dict[str, dict]:
    """Size and SHA-256 of every file below `code_dir`, by path relative to it; Python's bytecode
    caches excepted, which every import in a job writes there."""
    return {str(p.relative_to(code_dir)): {"size": p.stat().st_size, "sha256": digest(p)}
            for p in sorted(code_dir.rglob("*"))
            if p.is_file() and not p.is_symlink() and "__pycache__" not in p.relative_to(code_dir).parts}


def record_code_files(job_dir: Path) -> int:
    """Write <job>/code_files.json for the job's fresh code copy; returns the file count."""
    code = job_dir / "code"
    if not code.is_dir():
        raise Refused(f"{code} does not exist: copy the code into the job folder first")
    files = code_files(code)
    write_new(job_dir / CODE_FILES, json.dumps(files, indent=2))
    return len(files)


def seed_origin(seed_folder: Path) -> str:
    """The database a seed's versions came from, which marks merges from it: the manifest's
    `origin`; for a seed made before it was recorded, the source of the backup it was made from,
    or the job folder; else the seed folder itself."""
    manifest = json.loads((seed_folder / MANIFEST).read_text())
    if manifest.get("origin"):
        return manifest["origin"]
    if manifest.get("from_backup"):
        backup_manifest = Path(manifest["from_backup"]) / MANIFEST
        if backup_manifest.is_file():
            return json.loads(backup_manifest.read_text())["source"]
    if manifest.get("from_job"):
        return str(Path(manifest["from_job"]).resolve())
    return str(seed_folder.resolve())


def merge_marker(source: str, model: str, scenario: str, version: int) -> str:
    """What marks a merged version (scenario meta and merge record): the database it came from
    and which version of what it was there."""
    return f"merged from {source} {model}/{scenario} v{version}"


def legacy_marker(source: str, version: int) -> str:
    """The marker of 0.3.0 and before, which named no model or scenario. Read only together with
    the scenario: on the target's versions of that scenario, or a record about it."""
    return f"merged from {source} v{version}"


def cluster_spelling(path: Path, cfg: Config) -> str | None:
    """`path` (on the share, as this machine sees it) as the cluster spells it: what a job
    script's BACKUP=, SEED= or MAIN= needs. None for a path that is not on the share."""
    user = cfg.cluster_user or "<cluster user>"
    remote = cfg.remote_hdrive.format(cluster_user=user, remote_user=user, user=user)
    for root in hdrive_candidates(cfg):
        if reachable(root) and root.resolve() in path.resolve().parents:
            return f"{remote}/{path.resolve().relative_to(root.resolve())}"
    return None


def area_of(path: Path, cfg: Config) -> str | None:
    """The area a path lies in, whichever spelling of the share it uses."""
    return next((a for a, folder in cfg.areas.items() if within_area(str(path), folder) is not None), None)


def find_merge_record(cfg: Config, into_db: Path, marker: str, model: str, scenario: str,
                      legacy: str | None = None) -> str | None:
    """The path of a passing merge record of `model`/`scenario` with `marker` (or, for records
    written before markers named the scenario, `legacy`) into the database `into_db`, if one
    exists. Lets a merge refuse an obvious repeat before it backs the target up; the target's
    own scenario meta stays the final word."""
    area = area_of(into_db, cfg)
    area_dir = area_root(area, cfg) if area else cfg.records_dir / "_none"
    for record in merge_records(cfg, area_dir):
        if record.get("marker") not in {marker, legacy} - {None} or not record.get("compare", {}).get("ok"):
            continue
        if record.get("model") != model or record.get("scenario") != scenario:
            continue
        same = (within_area(record.get("into_db", ""), cfg.areas[area]) == within_area(str(into_db), cfg.areas[area])
                if area else Path(record.get("into_db", "")).resolve() == into_db.resolve())
        if same:
            return record["_path"]
    return None


def merge_records(cfg: Config, area_dir: Path) -> list[dict]:
    """Every merge record that merges wrote: in the area's code snapshots, where merge jobs run,
    and in the project's records folder, where `collect` brings them. A record is write-once and
    named uniquely, so a name found in both places is one record, counted once. Each carries
    `_path`."""
    paths = [*area_dir.glob(f"code/*/{cfg.records}/merge_*.json"), *cfg.records_dir.glob("merge_*.json")]
    records: dict[str, dict] = {}
    for path in paths:
        records.setdefault(path.name, {**json.loads(path.read_text()), "_path": str(path)})
    return list(records.values())


def within_area(path: str, area_folder: str) -> str | None:
    """The part of `path` below the area folder ("jobs/x_12", "mains/m/db/db"), whichever
    machine wrote it: records carry the cluster's spelling of the share, a workstation has its
    own. None when the path does not pass through that area folder."""
    parts, area = Path(path).parts, Path(area_folder).parts
    for i in range(len(parts) - len(area), -1, -1):
        if parts[i:i + len(area)] == area:
            return "/".join(parts[i + len(area):])
    return None


def uncollected_records(cfg: Config, job: Path) -> list[str]:
    """Records the job wrote in its code copy that are not, byte for byte, in the project's
    records folder: deleting the job would lose them."""
    missing = []
    for path in sorted((job / "code" / cfg.records).glob("*.json")):
        home = cfg.records_dir / path.name
        if not home.exists() or home.read_bytes() != path.read_bytes():
            missing.append(path.name)
    return missing


def job_outputs(cfg: Config, job: Path) -> list[str]:
    """What a run wrote into the job folder that may exist nowhere else, relative to it: files
    beside the job's own parts; new or changed files in its code copy (against code_files.json;
    the json records in the records folder excepted, which `collect` brings home and
    uncollected_records checks); GDX files, listings and GAMS scratch in its model folder. A
    code copy without code_files.json cannot be told apart from the staged code, so it counts."""
    out = sorted(p.name for p in job.iterdir() if p.name not in JOB_PARTS and not p.name.startswith("merge_src_"))
    code = job / "code"
    if code.is_dir():
        recorded = job / CODE_FILES
        if not recorded.exists():
            out.append(f"code/ (no {CODE_FILES}: what the run wrote there cannot be told from the staged code)")
        else:
            staged = json.loads(recorded.read_text())
            for rel, meta in code_files(code).items():
                path = Path(rel)
                if path.parent == Path(cfg.records) and path.suffix == ".json":
                    continue
                if staged.get(rel) != meta:
                    out.append(f"code/{rel}")
    model = job / "model"
    if model.is_dir():
        out += sorted(f"model/{p.relative_to(model)}" for p in model.rglob("*")
                      if p.is_file() and any(fnmatch.fnmatch(part, pat)
                                             for part in p.relative_to(model).parts for pat in MODEL_OUTPUTS))
    return out


def _listed(names: list[str], most: int = 5) -> str:
    return str(names) if len(names) <= most else f"{names[:most]} and {len(names) - most} more"


def _deletable(cfg: Config, job: Path, include_outputs: bool) -> str | None:
    """Why the closed job copy `job` must be kept whatever its merges, or None."""
    if not (job / "result.json").exists():
        return "not closed (running, failed or cancelled)"
    try:
        require_closed(job / "db" / STEM)
    except Refused as err:
        return f"database not closed: {err}"
    outputs = job_outputs(cfg, job)
    if outputs and not include_outputs:
        return (f"holds files a run wrote: {_listed(outputs)} (copy out what is needed, GDX in model/output "
                "included, then pass --include-outputs)")
    uncollected = uncollected_records(cfg, job)
    if uncollected:
        return f"records not collected yet: {uncollected} (ixmp-copies collect)"
    return None


def cleanup_plan(cfg: Config, area: str, main: str, root: Path | None = None,
                 include_outputs: bool = False) -> dict[str, list]:
    """Which job copies below <area>/jobs/ may be deleted: those whose job closed them, whose
    own records are collected, which hold no output a run wrote (job_outputs; unless
    `include_outputs`), and for which merge records show every scenario the job was meant to
    merge merged into <area>/mains/<main> with its comparison passing; or, for a job that was
    meant to merge nothing (an empty expected_merges.txt: a run submitted with scenario `-`),
    no merge record at all. Everything else is kept, with the reason. The evidence is the merge
    record (written only after a merge's clone and comparison), not a read of the main, which
    would need a JVM and the main closed."""
    area_dir = area_root(area, cfg, root)
    main_db = area_dir / COPY_KINDS["main"] / require_name(main) / "db" / STEM
    if not Path(f"{main_db}.properties").exists():
        raise Refused(f"no results main {main!r} in {area_dir / COPY_KINDS['main']}")
    main_rel = f"{COPY_KINDS['main']}/{main}/db/{STEM}"
    by_job: dict[str, list[dict]] = {}
    for record in merge_records(cfg, area_dir):
        job_rel = within_area(record.get("job_dir", ""), cfg.areas[area])
        if job_rel:
            by_job.setdefault(job_rel, []).append(record)
    plan: dict[str, list] = {"delete": [], "keep": []}
    jobs = area_dir / COPY_KINDS["job"]
    for job in sorted(p for p in jobs.iterdir() if p.is_dir()) if jobs.is_dir() else []:
        why = _deletable(cfg, job, include_outputs)
        if why:
            plan["keep"].append((job, why))
            continue
        records = by_job.get(f"{COPY_KINDS['job']}/{job.name}", [])
        good = [r for r in records if within_area(r.get("into_db", ""), cfg.areas[area]) == main_rel
                and r.get("compare", {}).get("ok")]
        bad = [r["_path"] for r in records if r not in good]
        expected_file = job / EXPECTED_MERGES
        expected = set(expected_file.read_text().split()) if expected_file.exists() else set()
        missing = sorted(expected - {r["scenario"] for r in good})
        if not records and expected_file.exists() and not expected:
            plan["delete"].append((job, []))  # a run meant to merge nothing (a read job)
        elif not records:
            plan["keep"].append((job, f"no merge record into {main}"))
        elif bad:
            plan["keep"].append((job, f"a merge went elsewhere or its comparison failed: {bad}"))
        elif missing:
            plan["keep"].append((job, f"no merge record yet for {missing}"))
        else:
            plan["delete"].append((job, [r["_path"] for r in good]))
    return plan


def discard_check(cfg: Config, area: str, name: str, root: Path | None = None,
                  include_outputs: bool = False) -> Path:
    """The job copy <area>/jobs/<name>, refusing unless it may be deleted whatever its merges:
    closed by its job, its database closed, its records collected, and no output a run wrote
    (unless `include_outputs`). For a copy the user decided is not needed (a failed run, a
    merge that will never be made)."""
    job = area_root(area, cfg, root) / COPY_KINDS["job"] / require_name(name)
    if not job.is_dir():
        raise Refused(f"no job copy {job}")
    why = _deletable(cfg, job, include_outputs)
    if why:
        raise Refused(f"{job}: {why}")
    return job


def cleanup_apply(plan: dict[str, list]) -> list[str]:
    """Delete the job copies the plan marks for deletion; returns their paths."""
    deleted = []
    for job, _ in plan["delete"]:
        shutil.rmtree(job)
        deleted.append(str(job))
    return deleted
