"""ixmp-copies: run MESSAGE-ix/ixmp work on a cluster against copies of a local HyperSQL database.

The chain is one-way: live database -> backup -> seed -> job copies -> merge into a main.

Setup and checks:
    init [--platform P] [--model M] [--venv V]   write ixmp_copies.toml here (never overwrites)
    doctor [--local]                    check every prerequisite, local and on the cluster
    where --area A [KIND]               print <area> or <area>/KIND (jobs, seeds, mains, runs,
                                        code, backups) as this machine sees it
On the workstation:
    backup [--platform P] [--apply]     byte copy of P's database (shut down cleanly, open in no
                                        process) to the backups folder; a copy inside an area
                                        backs up to <area>/backups/<label>/
    restore --from BACKUP --dest DIR [--apply]
                                        a working database in the new local folder DIR; prints
                                        the line that registers it with ixmp
    stage --area A [--extra F ...]      a committed snapshot of the project, plus this tool, to
                                        <area>/code/<sha>/ on the cluster's side (the jobs' CODE)
    collect --area A                    copy the records jobs and merges wrote into the
                                        project's records folder (write-once)
    transfer --from P1 --to P2 --scenario S [--model M] [--version V] [--apply]
                                        copy one scenario across platforms (e.g. ixmp-dev to a
                                        local database, and back), adding the units, regions
                                        and time slices the target lacks; backs up a HyperSQL
                                        target first
Anywhere:
    seed (--from BACKUP | --from-job DIR) --area A --name N [--apply]
                                        read-only seed <area>/seeds/N/ (make it on the cluster:
                                        chmod does not take over CIFS)
    verify FOLDER                       re-read a copy against its manifest
Inside a job (see the SLURM templates):
    job-copy --seed S --job-dir DIR --area A [--platform P] [--kind job|main]
    job-check --job-dir DIR [--platform P]
    job-close --job-dir DIR
    merge --job-dir DIR --scenario S [--version V] [--model M] [--into P] [--apply]

P defaults to [project] platform, M to [project] model. Without --apply, backup, restore,
seed, merge and transfer only run their checks.

Exit codes: 0 done; 3 refused, nothing changed; 2 a copy does not match its source, verify
found a difference, or a comparison after a merge or transfer failed; 4 a merge or transfer
failed after its backup (the target may hold a partial version: the message names the backup
to restore from); 1 a failed doctor check, or Python's own code for an uncaught error (a bug or
a failure no guard anticipated, never a refusal).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from ixmp_copies import config as config_mod
from ixmp_copies import copies, doctor, stage
from ixmp_copies.config import ConfigError
from ixmp_copies.copies import STEM, CopyMismatch, Refused
from ixmp_copies.platforms import AlreadyMerged


class OperationFailed(RuntimeError):
    """A merge or transfer failed after its backup was taken."""


def _record(cfg, name: str, payload: dict) -> Path:
    cfg.records_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.records_dir / f"{name}.json"
    copies.write_new(path, json.dumps(payload, indent=2, default=str))
    return path


def _model(cfg, args) -> str:
    model = args.model or cfg.model
    if not model:
        raise Refused("no model: pass --model or set [project] model")
    return model


def cmd_init(args) -> int:
    path = Path.cwd() / config_mod.FILENAME
    if path.exists():
        raise Refused(f"{path} exists")
    name = copies.require_name(Path.cwd().name.lower().replace("-", "_"))
    text = config_mod.template(name, args.platform or f"{name}-local", args.model or "",
                               args.venv or "~/repos/.venv")
    path.write_text(text)
    print(f"wrote {path}; edit it, then run `ixmp-copies doctor`")
    return 0


def cmd_doctor(args) -> int:
    return doctor.report(doctor.run(remote=not args.local))


def cmd_where(args) -> int:
    cfg = config_mod.load()
    base = copies.area_root(args.area, cfg)
    print(base / args.kind if args.kind else base)
    return 0


def cmd_backup(args) -> int:
    from ixmp_copies.platforms import platform_db

    cfg = config_mod.load()
    platform = args.platform or cfg.platform
    db = platform_db(platform)
    dest_root = copies.backup_root_for(db, cfg)
    label = copies.copy_label(db, platform, cfg)
    if not args.apply:
        copies.require_closed(db)
        files = copies.snapshot(copies.database_files(db))
        print(f"{db} is closed; would copy {sorted(files)} "
              f"({sum(f['size'] for f in files.values()) / 1e6:.0f} MB) to {dest_root / label}/; "
              f"record backup_{label}_*")
        return 0
    dest, manifest = copies.backup(db, dest_root, label)
    record = _record(cfg, f"backup_{label}_{dest.name}", {**manifest, "copy": str(dest)})
    print(f"backup of {db} at {dest}; {len(manifest['files'])} files, sha256 checked; record {record}")
    return 0


def cmd_seed(args) -> int:
    cfg = config_mod.load()
    folder = Path(args.source or args.from_job)
    if not args.apply:
        (copies.require_verified(folder, "backup") if args.source else copies.require_job_result(folder))
        copies.area_root(args.area, cfg)
        copies.require_name(args.name)
        print(f"{folder} is as recorded; would seed {args.area}/seeds/{args.name}")
        return 0
    dest, manifest, read_only = (copies.seed(folder, args.area, args.name, cfg) if args.source
                                 else copies.seed_from_job(folder, args.area, args.name, cfg))
    record = _record(cfg, f"seed_{args.area}_{args.name}", {**manifest, "copy": str(dest), "read_only": read_only})
    print(f"seed {dest} from {folder}; read-only: {read_only}; record {record}")
    if not read_only:
        print("warning: read-only did not take (a CIFS mount?): make seeds on the cluster", file=sys.stderr)
    return 0


def cmd_restore(args) -> int:
    cfg = config_mod.load()
    folder, dest = Path(args.source), Path(args.dest).resolve()
    if not args.apply:
        copies.require_verified(folder, "backup")
        print(f"{folder} matches its manifest; would restore it to {dest}")
        return 0
    manifest = copies.restore(folder, dest, cfg)
    record = _record(cfg, f"restore_{dest.name}_{time.strftime('%Y%m%d_%H%M%S')}", {**manifest, "copy": str(dest)})
    print(f"restored {folder} to {dest}; record {record}\nregister it under a new platform name:\n"
          f"  ixmp platform add <name> jdbc hsqldb \"url={copies.hsqldb_url(dest / STEM)}\"")
    return 0


def cmd_verify(args) -> int:
    problems = copies.verify(Path(args.folder))
    for p in problems:
        print(p, file=sys.stderr)
    print(f"{args.folder}: {'MISMATCH' if problems else 'matches its manifest'}")
    return 2 if problems else 0


def cmd_stage(args) -> int:
    cfg = config_mod.load()
    print(stage.stage(cfg, args.area, args.extra, stage.ssh_remote(cfg.ssh_host)))
    return 0


def cmd_collect(args) -> int:
    cfg = config_mod.load()
    out = stage.collect(cfg, copies.area_root(args.area, cfg))
    print(f"{len(out['copied'])} copied, {len(out['present'])} already present, into {cfg.records_dir}")
    for c in out["conflicts"]:
        print(f"CONFLICT (not overwritten): {c}", file=sys.stderr)
    return 2 if out["conflicts"] else 0


def cmd_job_copy(args) -> int:
    if os.environ.get("IXMP_DATA"):
        raise Refused(f"IXMP_DATA is already set ({os.environ['IXMP_DATA']}): job-copy reads the "
                      "shared ixmp config and must run before the job switches to its own")
    import message_ix

    from ixmp_copies.platforms import ixmp_config

    cfg = config_mod.load()
    ixc = ixmp_config()
    shared = json.loads(Path(ixc.path).read_text())
    model_src = Path(ixc.get("message model dir"))
    out = copies.job_copy(Path(args.seed), Path(args.job_dir), args.platform or cfg.platform, shared,
                          model_src, args.area, cfg, kind=args.kind, message_ix_version=message_ix.__version__)
    print(json.dumps(out, indent=2))
    return 0


def cmd_job_check(args) -> int:
    from ixmp_copies.platforms import hsqldb_file

    job_dir = Path(args.job_dir).resolve()
    platform = args.platform or config_mod.load().platform
    if Path(os.environ.get("IXMP_DATA", "")).resolve() != job_dir / "ixmp":
        raise Refused(f"IXMP_DATA={os.environ.get('IXMP_DATA')!r}, not {job_dir / 'ixmp'}")
    from ixmp_copies.platforms import ixmp_config

    ixc = ixmp_config()
    found = {
        "config": Path(ixc.path).resolve(),
        "database": hsqldb_file(ixc.get_platform_info(platform)[1]).resolve(),
        "model dir": Path(ixc.get("message model dir")).resolve(),
        "platforms": sorted(ixc.get("platform")),
    }
    wanted = {"config": job_dir / "ixmp" / "config.json", "database": job_dir / "db" / STEM,
              "model dir": job_dir / "model", "platforms": sorted({"default", platform})}
    wrong = {k: f"{found[k]} (wanted {wanted[k]})" for k in wanted if found[k] != wanted[k]}
    if wrong:
        raise Refused(f"this process's ixmp does not point at {job_dir} alone: {wrong}")
    print(f"{platform} -> {found['database']}; model dir {found['model dir']}; no other platform")
    return 0


def cmd_job_close(args) -> int:
    result = copies.job_close(Path(args.job_dir))
    print(f"{args.job_dir}: closed; {len(result['files'])} files recorded in result.json")
    return 0


def _default(default_version, mp, model: str, scenario: str) -> int:
    try:
        return default_version(mp, model, scenario)
    except LookupError as err:
        raise Refused(f"{err}; pass --version") from err


def _guarded(what: str, backup_dir: Path | None, target: str, fn):
    """Run fn(); any failure after the backup becomes OperationFailed naming the backup."""
    try:
        return fn()
    except (Refused, AlreadyMerged):
        raise
    except Exception as err:  # noqa: BLE001 -- JVM errors (e.g. OutOfMemoryError) are not
        # Python exception classes one can name here; whatever it is, the target may now hold
        # a partial version, so the backup to restore from must be in the message.
        where = f": restore from {backup_dir}" if backup_dir else ""
        raise OperationFailed(f"{what}: {type(err).__name__}: {str(err)[:300]}; {target} may hold a "
                              f"partial version{where}") from err


def cmd_merge(args) -> int:
    import ixmp

    from ixmp_copies.platforms import default_version, merge_scenario, platform_db

    cfg = config_mod.load()
    model, into = _model(cfg, args), args.into or cfg.platform
    job_dir = Path(args.job_dir).resolve()
    copies.require_job_result(job_dir)
    dst = platform_db(into)
    copies.require_closed(dst)
    label = copies.copy_label(dst, into, cfg)
    backups = copies.backup_root_for(dst, cfg)
    print(f"{model}/{args.scenario} v{args.version or 'default'} from {job_dir} -> {into} ({dst}); "
          f"pre-merge backup to {backups / label}/; record merge_{label}_*")
    if not args.apply:
        print("dry run: nothing copied or opened; pass --apply")
        return 0
    backup_dir, _ = copies.backup(dst, backups, label)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    # The job's own copy is never opened: a merge reads a throwaway checked copy of it.
    src_dir = job_dir / f"merge_src_{stamp}"
    copies.checked_copy(job_dir / "db" / STEM, src_dir, {"kind": "merge-source", "job": str(job_dir)})
    src_mp = ixmp.Platform(backend="jdbc", driver="hsqldb", url=copies.hsqldb_url(src_dir / STEM))
    try:
        version = args.version or _default(default_version, src_mp, model, args.scenario)
        marker = f"merged from {job_dir} v{version}"
        dst_mp = ixmp.Platform(into)
        try:
            out = _guarded("MERGE", backup_dir, f"{into} ({dst})", lambda: merge_scenario(
                src_mp, dst_mp, model, args.scenario, version, marker, cfg.marker_key))
        except AlreadyMerged as err:
            raise Refused(str(err)) from err
        finally:
            dst_mp.close_db()
    finally:
        src_mp.close_db()
    copies.require_closed(dst)
    source = job_dir / "model_source.json"
    out.update({"job_dir": str(job_dir), "into": into, "into_db": str(dst), "label": label,
                "pre_merge_backup": str(backup_dir), "merge_source_copy": str(src_dir),
                "model_source": json.loads(source.read_text()) if source.exists() else None})
    record = _record(cfg, f"merge_{label}_{args.scenario}_v{version}_{stamp}", out)
    print(json.dumps(out, indent=2, default=str))
    print(f"record {record}")
    return 0 if out["compare"]["ok"] else 2


def cmd_transfer(args) -> int:
    import ixmp
    import message_ix

    from ixmp_copies.platforms import (
        clone_across,
        compare_copy,
        default_version,
        is_hsqldb,
        platform_db,
        read_registry,
        seed_registry,
    )

    cfg = config_mod.load()
    model = _model(cfg, args)
    dst_db = platform_db(args.to) if is_hsqldb(args.to) else None
    if dst_db is not None:
        copies.require_closed(dst_db)
    print(f"{model}/{args.scenario} v{args.version or 'default'}: {args.source} -> {args.to}"
          + (f" ({dst_db}; backed up first)" if dst_db else " (not HyperSQL: no backup possible)"))
    if not args.apply:
        print("dry run: nothing opened; pass --apply")
        return 0
    backup_dir = None
    if dst_db is not None:
        backup_dir, _ = copies.backup(dst_db, copies.backup_root_for(dst_db, cfg),
                                      copies.copy_label(dst_db, args.to, cfg))
    src_mp = ixmp.Platform(args.source)
    try:
        version = args.version or _default(default_version, src_mp, model, args.scenario)
        src = message_ix.Scenario(src_mp, model, args.scenario, version=version)
        dst_mp = ixmp.Platform(args.to)
        try:
            def move():
                added = seed_registry(dst_mp, read_registry(src_mp))
                copy = clone_across(src, dst_mp)
                check = compare_copy(src, copy)
                if check["ok"]:
                    copy.set_as_default()
                return {"registry_added": added, "version": int(copy.version), "compare": check}
            out = _guarded("TRANSFER", backup_dir, args.to, move)
        finally:
            dst_mp.close_db()
    finally:
        src_mp.close_db()
    out.update({"model": model, "scenario": args.scenario, "source": args.source,
                "source_version": version, "to": args.to, "pre_transfer_backup": str(backup_dir)})
    record = _record(cfg, f"transfer_{args.to}_{args.scenario}_v{version}_{time.strftime('%Y%m%d_%H%M%S')}", out)
    print(json.dumps(out, indent=2, default=str))
    print(f"record {record}")
    return 0 if out["compare"]["ok"] else 2


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ixmp-copies", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, func, *flags, **opts):
        s = sub.add_parser(name)
        for flag in flags:
            s.add_argument(flag, required=True)
        for flag, kw in opts.items():
            s.add_argument(f"--{flag.replace('_', '-')}", **kw)
        s.set_defaults(func=func)
        return s

    apply = {"action": "store_true"}
    add("init", cmd_init, platform={}, model={}, venv={})
    add("doctor", cmd_doctor, local={"action": "store_true", "help": "skip the cluster checks"})
    w = add("where", cmd_where, "--area")
    w.add_argument("kind", nargs="?", choices=("jobs", "seeds", "mains", "runs", "code", "backups"))
    add("backup", cmd_backup, platform={}, apply=apply)
    s = add("seed", cmd_seed, "--area", "--name", apply=apply)
    src = s.add_mutually_exclusive_group(required=True)
    src.add_argument("--from", dest="source", help="a backup folder")
    src.add_argument("--from-job", help="a job folder its job closed")
    r = add("restore", cmd_restore, "--dest", apply=apply)
    r.add_argument("--from", dest="source", required=True, help="a backup folder")
    v = add("verify", cmd_verify)
    v.add_argument("folder")
    add("stage", cmd_stage, "--area", extra={"action": "append", "default": [],
                                             "help": "PATH, or CHECKOUT:PATH for an untracked file"})
    add("collect", cmd_collect, "--area")
    add("job-copy", cmd_job_copy, "--seed", "--job-dir", "--area", platform={},
        kind={"choices": ("job", "main"), "default": "job"})
    add("job-check", cmd_job_check, "--job-dir", platform={})
    add("job-close", cmd_job_close, "--job-dir")
    add("merge", cmd_merge, "--job-dir", "--scenario", version={"type": int}, model={}, into={}, apply=apply)
    t = add("transfer", cmd_transfer, "--to", "--scenario", version={"type": int}, model={}, apply=apply)
    t.add_argument("--from", dest="source", required=True, help="source platform name")
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.func(args)
    except (Refused, ConfigError) as err:
        print(f"REFUSED: {err}", file=sys.stderr)
        return 3
    except CopyMismatch as err:
        print(f"MISMATCH: {err}", file=sys.stderr)
        return 2
    except OperationFailed as err:
        print(f"FAILED: {err}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
