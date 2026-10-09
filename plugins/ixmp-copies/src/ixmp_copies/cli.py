"""ixmp-copies: run MESSAGE-ix/ixmp work on a cluster against copies of a local HyperSQL database.

The chain is one-way: live database -> backup -> seed -> job copies -> merge into a main.

Setup and checks:
    init --venv V [--cluster-user U] [--platform P] [--model M] [--name N]
                                        write ixmp_copies.toml here (never overwrites); V is the
                                        project's venv on the cluster, U your cluster account
    platform-add [--name P] [--dir D] [--apply]
                                        register a local HyperSQL platform with CACHED tables
                                        (a new or an existing database, never on the H drive)
    doctor [--local]                    check every prerequisite, local and on the cluster
    where --area A [KIND] | --backups   print <area> or <area>/KIND (jobs, seeds, mains, runs,
                                        code, backups), or the backups of databases outside
                                        every area, as this machine sees the share
On the workstation:
    backup [--platform P] [--apply]     byte copy of P's database (shut down cleanly, open in no
                                        process) to the backups folder; a copy inside an area
                                        backs up to <area>/backups/<label>/. Prints the
                                        backup's path as the cluster spells it (for BACKUP=)
    restore --from BACKUP --dest DIR [--apply]
                                        a working database in the new local folder DIR; prints
                                        the line that registers it with ixmp
    stage --area A [--extra F ...]      a committed snapshot of the project, plus this tool, to
                                        <area>/code/<sha>/ on the cluster's side (the jobs' CODE)
    collect --area A                    copy the records jobs and merges wrote into the
                                        project's records folder (write-once; submission
                                        records, which only grow, are updated)
    cleanup --area A --main NAME [--include-outputs] [--apply]
                                        delete the job copies whose every expected scenario a
                                        merge record shows merged into <area>/mains/NAME and
                                        whose records are collected; keep the rest
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
    run-mark --job-dir DIR --scenario S (--before | --after) [--model M]
                                        record S's versions before the run's command, and the
                                        version it left as default after it (refused when that
                                        is no new version: the command forgot set_as_default())
    merge --job-dir DIR --scenario S [--version N | --version default] [--allow-unsolved]
          [--model M] [--into P] [--apply]
                                        the version is the one the run recorded (run-mark);
                                        a job without that record needs --version

P defaults to [project] platform, M to [project] model. Without --apply, backup, restore,
seed, merge and transfer only run their checks.

Exit codes: 0 done; 3 refused, nothing changed (also: no H drive, an unknown platform, no
config); 2 a copy does not match its source, verify found a difference, a comparison after a
merge or transfer failed, or collect met a record it will not overwrite; 4 a merge or transfer
failed after its backup, or left its target not shut down cleanly (the message names the backup
to restore from); 1 a failed doctor check, or Python's own code for an uncaught error (a bug or
a failure no guard anticipated, never a refusal).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from ixmp_copies import config as config_mod
from ixmp_copies import copies, doctor, stage
from ixmp_copies.config import ConfigError
from ixmp_copies.copies import STEM, CopyMismatch, Refused
from ixmp_copies.platforms import AlreadyMerged, PlatformError


class OperationFailed(RuntimeError):
    """A merge or transfer failed after its backup was taken, or left its target not closed."""


def _version(text: str):
    """--version: a number, or `default` for the copy's default version."""
    return text if text == "default" else int(text)


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
    name = args.name or re.sub(r"[^a-z0-9_]+", "_", Path.cwd().name.lower()).strip("_")
    copies.require_name(name)
    platform = args.platform or f"{name.replace('_', '-')}-local"
    user = args.cluster_user or stage.cluster_whoami("unicc")
    if not user:
        raise Refused("no --cluster-user given and `ssh unicc whoami` did not answer: pass your cluster "
                      "account (at IIASA, your IIASA user)")
    path.write_text(config_mod.template(name, platform, args.model or "", args.venv, user))
    print(f"wrote {path} (project {name}, platform {platform}, cluster user {user}). Next:\n"
          f"  1. check it: [cluster] modules and gams_module, [storage] roots if your share is mounted elsewhere\n"
          f"  2. ixmp-copies platform-add --apply      (if {platform} is not registered with ixmp yet)\n"
          f"  3. ixmp-copies doctor                    (until nothing fails)\n"
          f"  4. git add {path.name} && git commit")
    return 0


def cmd_platform_add(args) -> int:
    """Register a local HyperSQL platform with CACHED tables in this process's ixmp config."""
    from ixmp_copies.platforms import ixmp_config

    cfg = config_mod.load()
    name = args.name or cfg.platform
    dest = Path(args.dir).expanduser().resolve() if args.dir else Path.home() / "ixmp_local" / name
    ixc = ixmp_config()
    if name in ixc.get("platform"):
        raise Refused(f"platform {name!r} is already in {ixc.path}: {ixc.get('platform')[name]}")
    for base in copies.hdrive_candidates(cfg):
        if copies.reachable(base) and base.resolve() in (dest / STEM).parents:
            raise Refused(f"{dest} is on the H drive: a working database belongs on a local disk")
    db = dest / STEM
    if Path(f"{db}.script").exists():
        try:
            copies.require_cached_tables(db)
        except ValueError as err:
            raise Refused(f"{db} exists and cannot be used: {err}") from err
        state = "an existing database"
    elif dest.exists() and any(dest.iterdir()):
        raise Refused(f"{dest} exists and holds files but no {STEM}.script")
    else:
        state = "a new database, created on first open"
    url = copies.hsqldb_url(db)
    print(f"{name} -> {url} ({state}) in {ixc.path}")
    if not args.apply:
        print("dry run: ixmp config unchanged; pass --apply")
        return 0
    dest.mkdir(parents=True, exist_ok=True)
    ixc.add_platform(name, "jdbc", "hsqldb", url=url)
    ixc.save()
    print(f"registered {name}")
    return 0


def cmd_doctor(args) -> int:
    return doctor.report(doctor.run(remote=not args.local))


def cmd_where(args) -> int:
    cfg = config_mod.load()
    if args.backups:
        print(copies.hdrive_root(cfg) / cfg.backups)
        return 0
    if not args.area:
        raise Refused("pass --area A (and a kind), or --backups")
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
    on_cluster = copies.cluster_spelling(dest, cfg)
    if on_cluster:
        print(f"on the cluster (BACKUP=): {on_cluster}")
    return 0


def cmd_seed(args) -> int:
    cfg = config_mod.load()
    folder = Path(args.source or args.from_job)
    if not args.apply:
        if args.source:
            source = copies.require_verified(folder, "backup")
            db = folder / Path(source["source"]).name
        else:
            copies.require_job_result(folder)
            db = folder / "db" / STEM
        try:
            copies.require_cached_tables(db)
        except ValueError as err:
            raise Refused(f"{folder} cannot seed jobs: {err}") from err
        dest = copies.area_root(args.area, cfg) / "seeds" / copies.require_name(args.name)
        if dest.exists() or dest.with_name(dest.name + copies.PARTIAL).exists():
            raise Refused(f"{dest} already exists")
        print(f"{folder} is as recorded; would seed {dest}")
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
        copies.require_restore_dest(dest, cfg)
        print(f"{folder} matches its manifest; would restore it to {dest}")
        return 0
    manifest = copies.restore(folder, dest, cfg)
    record = _record(cfg, f"restore_{dest.name}_{time.strftime('%Y%m%d_%H%M%S')}", {**manifest, "copy": str(dest)})
    print(f"restored {folder} to {dest}; record {record}\nregister it under a new platform name:\n"
          f"  ixmp-copies platform-add --name <name> --dir {dest} --apply")
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
    print(f"{len(out['copied'])} copied, {len(out['updated'])} updated, {len(out['present'])} already present, "
          f"into {cfg.records_dir}")
    for c in out["conflicts"]:
        print(f"CONFLICT (not overwritten): {c}", file=sys.stderr)
    return 2 if out["conflicts"] else 0


def cmd_cleanup(args) -> int:
    cfg = config_mod.load()
    plan = copies.cleanup_plan(cfg, args.area, args.main, include_outputs=args.include_outputs)
    for job, reason in plan["keep"]:
        print(f"keep    {job.name}: {reason}")
    for job, records in plan["delete"]:
        print(f"delete  {job.name}: merged ({len(records)} record{'s' if len(records) > 1 else ''})")
    if not args.apply:
        print(f"dry run: {len(plan['delete'])} job copies would be deleted; pass --apply")
        return 0
    deleted = copies.cleanup_apply(plan)
    record = _record(cfg, f"cleanup_{args.area}_{args.main}_{time.strftime('%Y%m%d_%H%M%S')}",
                     {"deleted": {str(j): r for j, r in plan["delete"]},
                      "kept": {str(j): why for j, why in plan["keep"]}})
    print(f"deleted {len(deleted)} job copies; record {record}")
    return 0


def cmd_job_copy(args) -> int:
    if os.environ.get("IXMP_DATA"):
        raise Refused(f"IXMP_DATA is already set ({os.environ['IXMP_DATA']}): job-copy reads the "
                      "shared ixmp config and must run before the job switches to its own")
    import message_ix

    from ixmp_copies.platforms import ixmp_config

    cfg = config_mod.load()
    ixc = ixmp_config()
    if ixc.path is None or not Path(ixc.path).is_file():
        raise Refused("this account has no ixmp config file (ixmp config show names none): run "
                      "`ixmp platform add` once, or copy a config without passwords, on this machine")
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


def cmd_run_mark(args) -> int:
    """Inside a run job: record the scenario's versions before the command, and after it the
    version the command left as default; refuse (exit 3) when that is not a new version."""
    import ixmp

    from ixmp_copies.platforms import scenario_state

    cfg = config_mod.load()
    model = _model(cfg, args)
    job_dir = Path(args.job_dir).resolve()
    if Path(os.environ.get("IXMP_DATA", "")).resolve() != job_dir / "ixmp":
        raise Refused(f"IXMP_DATA={os.environ.get('IXMP_DATA')!r}, not {job_dir / 'ixmp'}: run job-check first")
    mp = ixmp.Platform()
    try:
        state = scenario_state(mp, model, args.scenario)
    finally:
        mp.close_db()
    if args.before:
        copies.write_new(job_dir / copies.RUN_BEFORE, json.dumps(state, indent=2))
        print(f"before the run: {model}/{args.scenario} versions {state['versions']}, default {state['default']}")
        return 0
    before_file = job_dir / copies.RUN_BEFORE
    if not before_file.exists():
        raise Refused(f"{before_file} does not exist: run-mark --before was not run")
    before = json.loads(before_file.read_text())
    if before["scenario"] != args.scenario or before["model"] != model:
        raise Refused(f"{before_file} is about {before['model']}/{before['scenario']}")
    result = {**state, "before": before["versions"],
              "new": state["default"] is not None and state["default"] not in before["versions"]}
    copies.write_new(job_dir / copies.RUN_RESULT, json.dumps(result, indent=2))
    if not result["new"]:
        raise Refused(f"the run left no new version of {model}/{args.scenario} as default (default "
                      f"{state['default']}, versions before the run {before['versions']}): did its command "
                      "call set_as_default()? Its merge will refuse")
    print(f"the run left v{state['default']} as default (solved: {state['solved']})")
    return 0


def _merge_version(job_dir: Path, scenario: str, requested, allow_unsolved: bool):
    """The version a merge brings back, and the run's own record of it when there is one."""
    result_file = job_dir / copies.RUN_RESULT
    if result_file.exists():
        run = json.loads(result_file.read_text())
        if run["scenario"] != scenario:
            raise Refused(f"{job_dir}'s run was about {run['scenario']}, not {scenario}")
        if not run["new"]:
            raise Refused(f"the run left no new version of {scenario} as default (default {run['default']}, "
                          f"versions before it {run['before']}): did its command call set_as_default()? "
                          "Nothing merged")
        if requested not in (None, "default") and requested != run["default"]:
            raise Refused(f"--version {requested} is not the version the run left as default ({run['default']})")
        if not run["solved"] and not allow_unsolved:
            raise Refused(f"v{run['default']} of {scenario} has no solution; pass --allow-unsolved to merge it anyway")
        return run["default"], run
    if requested is None:
        raise Refused(f"{job_dir} holds no run record ({copies.RUN_RESULT}): pass --version N, or "
                      "--version default for the copy's default version")
    return requested, None


def cmd_merge(args) -> int:
    import ixmp

    from ixmp_copies.platforms import default_version, merge_scenario, platform_db

    cfg = config_mod.load()
    model, into = _model(cfg, args), args.into or cfg.platform
    job_dir = Path(args.job_dir).resolve()
    copies.require_job_result(job_dir)
    version, run = _merge_version(job_dir, args.scenario, args.version, args.allow_unsolved)
    seed_file = job_dir / copies.SEED_MERGE
    # A seed merge is marked by its seed, which is the same in every resubmission; a run's
    # merge by the job that ran it.
    source = Path(seed_file.read_text().strip()).resolve() if seed_file.exists() else job_dir
    dst = platform_db(into)
    copies.require_closed(dst)
    label = copies.copy_label(dst, into, cfg)
    backups = copies.backup_root_for(dst, cfg)
    if version != "default":
        earlier = copies.find_merge_record(cfg, dst, f"merged from {source} v{version}")
        if earlier:
            raise Refused(f"{model}/{args.scenario} v{version} from {source} was merged into {into} before: {earlier}")
    print(f"{model}/{args.scenario} v{version} from {job_dir} -> {into} ({dst}); "
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
        if version == "default":
            version = _default(default_version, src_mp, model, args.scenario)
        marker = f"merged from {source} v{version}"
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
    model_source = job_dir / "model_source.json"
    out.update({"job_dir": str(job_dir), "into": into, "into_db": str(dst), "label": label,
                "pre_merge_backup": str(backup_dir), "merge_source_copy": str(src_dir), "run": run,
                "model_source": json.loads(model_source.read_text()) if model_source.exists() else None})
    record = _record(cfg, f"merge_{label}_{args.scenario}_v{version}_{stamp}", out)
    print(json.dumps(out, indent=2, default=str))
    print(f"record {record}")
    try:
        copies.require_closed(dst)
    except Refused as err:
        raise OperationFailed(f"merged and recorded ({record}), but {err}; check before the next merge, "
                              f"or restore from {backup_dir}") from err
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
        version = args.version if isinstance(args.version, int) else _default(
            default_version, src_mp, model, args.scenario)
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

    def add(command, func, *flags, **opts):
        s = sub.add_parser(command)
        for flag in flags:
            s.add_argument(flag, required=True)
        for flag, kw in opts.items():
            s.add_argument(f"--{flag.replace('_', '-')}", **kw)
        s.set_defaults(func=func)
        return s

    apply = {"action": "store_true"}
    add("init", cmd_init, "--venv", platform={}, model={}, name={"help": "project name ([a-z0-9_])"},
        cluster_user={"help": "your cluster account (default: asks `ssh unicc whoami`)"})
    add("platform-add", cmd_platform_add, name={"help": "default: [project] platform"},
        dir={"help": "default: ~/ixmp_local/<name>"}, apply=apply)
    add("doctor", cmd_doctor, local={"action": "store_true", "help": "skip the cluster checks"})
    w = add("where", cmd_where, area={}, backups={"action": "store_true",
                                                 "help": "the backups of databases outside every area"})
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
    add("cleanup", cmd_cleanup, "--area", "--main", apply=apply,
        include_outputs={"action": "store_true", "help": "also delete jobs holding files a run wrote"})
    add("job-copy", cmd_job_copy, "--seed", "--job-dir", "--area", platform={},
        kind={"choices": ("job", "main"), "default": "job"})
    add("job-check", cmd_job_check, "--job-dir", platform={})
    add("job-close", cmd_job_close, "--job-dir")
    rm = add("run-mark", cmd_run_mark, "--job-dir", "--scenario", model={})
    when = rm.add_mutually_exclusive_group(required=True)
    when.add_argument("--before", action="store_true")
    when.add_argument("--after", action="store_true")
    add("merge", cmd_merge, "--job-dir", "--scenario", version={"type": _version}, model={}, into={}, apply=apply,
        allow_unsolved={"action": "store_true"})
    t = add("transfer", cmd_transfer, "--to", "--scenario", version={"type": int}, model={}, apply=apply)
    t.add_argument("--from", dest="source", required=True, help="source platform name")
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.func(args)
    except (Refused, ConfigError, PlatformError) as err:
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
