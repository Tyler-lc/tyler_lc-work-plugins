"""The whole chain on real HyperSQL databases, one JVM at a time: create a platform, back it up,
seed, a results main and a job copy, change a scenario on the job copy, close it, merge it
into the main (and refuse the repeat), then transfer a scenario between two platforms.
Skipped without ixmp, message_ix or java."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import SRC, has_ixmp, run_cli

from ixmp_copies import copies as dbc

pytestmark = pytest.mark.skipif(not (has_ixmp() and shutil.which("java")), reason="needs ixmp and java")
HEAP = {"JAVA_TOOL_OPTIONS": "-Xmx2g"}


def py(code: str, **env: str) -> subprocess.CompletedProcess:
    full = {k: v for k, v in os.environ.items() if k != "IXMP_DATA"}
    full.update(PYTHONPATH=str(SRC), **HEAP, **env)
    out = subprocess.run([sys.executable, "-c", code], env=full, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout[-1500:] + out.stderr[-3000:]
    return out


def ixmp_home(folder: Path, platforms: dict[str, Path]) -> Path:
    folder.mkdir(parents=True)
    conf = {"platform": {"default": next(iter(platforms)),
                         **{n: {"class": "jdbc", "driver": "hsqldb", "url": dbc.hsqldb_url(db)}
                            for n, db in platforms.items()}}}
    (folder / "config.json").write_text(json.dumps(conf))
    return folder


MODEL, SCEN = "Canning problem (MESSAGE scheme)", "standard"
CREATE = """
import ixmp
from message_ix.testing import make_dantzig
mp = ixmp.Platform("{name}")
make_dantzig(mp).set_as_default()
mp.close_db()
"""

CHANGE = """
import ixmp, message_ix
mp = ixmp.Platform()
s = message_ix.Scenario(mp, "{model}", "{scen}")
c = s.clone(keep_solution=False)
c.check_out(); c.add_set("technology", ["t_new"]); c.commit("job change")
{default}
print("VERSION", c.version)
mp.close_db()
"""


def test_full_chain(project, tmp_path):
    cfg, hd = project
    root = cfg.project_root
    live_db = tmp_path / "live" / "db"
    live_home = ixmp_home(tmp_path / "ixmp_live", {"proj-local": live_db})
    py(CREATE.format(name="proj-local"), IXMP_DATA=str(live_home))
    assert dbc.require_cached_tables(live_db) > 0

    env = {"IXMP_DATA": str(live_home), **HEAP}
    dry = run_cli(["backup"], root, **env)
    assert dry.returncode == 0 and "is closed" in dry.stdout, dry.stderr
    done = run_cli(["backup", "--apply"], root, **env)
    assert done.returncode == 0, done.stderr
    backup = next((hd / "ixmp_backup" / "proj-local").iterdir())
    seeded = run_cli(["seed", "--from", str(backup), "--area", "live", "--name", "s1", "--apply"], root)
    assert seeded.returncode == 0, seeded.stderr
    seed = hd / "ixmp_live" / "seeds" / "s1"

    main = hd / "ixmp_live" / "mains" / "results"
    job = hd / "ixmp_live" / "jobs" / "j1"
    forgot = hd / "ixmp_live" / "jobs" / "j2"
    for args in (["--kind", "main", "--job-dir", str(main)], ["--job-dir", str(job)], ["--job-dir", str(forgot)]):
        out = run_cli(["job-copy", "--seed", str(seed), "--area", "live", *args], root)
        assert out.returncode == 0, out.stderr

    def run_job(job_dir, default_line):
        """What job_run.do does around a command: job-check, run-mark before and after, job-close."""
        jenv = {"IXMP_DATA": str(job_dir / "ixmp"), **HEAP}
        check = run_cli(["job-check", "--job-dir", str(job_dir)], root, **jenv)
        assert check.returncode == 0, check.stderr
        mark = ["run-mark", "--job-dir", str(job_dir), "--scenario", SCEN, "--model", MODEL]
        assert run_cli([*mark, "--before"], root, **jenv).returncode == 0
        changed = py(CHANGE.format(model=MODEL, scen=SCEN, default=default_line), IXMP_DATA=str(job_dir / "ixmp"))
        after = run_cli([*mark, "--after"], root, **jenv)
        assert run_cli(["job-close", "--job-dir", str(job_dir)], root).returncode == 0
        return int(changed.stdout.split("VERSION")[1].split()[0]), after

    job_version, after = run_job(job, "c.set_as_default()")
    assert after.returncode == 0, after.stderr
    _, forgotten = run_job(forgot, "")
    assert forgotten.returncode == 3 and "set_as_default" in forgotten.stderr, forgotten.stderr[-500:]
    assert json.loads((job / dbc.RUN_RESULT).read_text())["default"] == job_version

    menv = {"IXMP_DATA": str(main / "ixmp"), **HEAP}
    stale = run_cli(["merge", "--job-dir", str(forgot), "--scenario", SCEN, "--model", MODEL, "--apply"], root, **menv)
    assert stale.returncode == 3 and "set_as_default" in stale.stderr, stale.stderr[-500:]
    unsolved = run_cli(["merge", "--job-dir", str(job), "--scenario", SCEN, "--model", MODEL, "--apply"], root, **menv)
    assert unsolved.returncode == 3 and "no solution" in unsolved.stderr, unsolved.stderr[-500:]
    assert not (hd / "ixmp_live" / "backups").exists()  # both refused before any backup
    merged = run_cli(["merge", "--job-dir", str(job), "--scenario", SCEN, "--model", MODEL, "--allow-unsolved",
                      "--apply"], root, **menv)
    assert merged.returncode == 0, merged.stdout[-2000:] + merged.stderr[-2000:]
    record = json.loads(next(cfg.records_dir.glob(f"merge_results_{SCEN}_v*.json")).read_text())
    assert record["source_version"] == job_version and record["set_default"] and record["compare"]["ok"]
    assert record["model_source"]["fingerprint"] == json.loads((job / "model_source.json").read_text())["fingerprint"]
    assert Path(record["pre_merge_backup"]).parent == (hd / "ixmp_live" / "backups" / "results").resolve()
    after = py(f"""
import ixmp, message_ix
mp = ixmp.Platform()
s = message_ix.Scenario(mp, "{MODEL}", "{SCEN}")
print("DEFAULT", s.version, "t_new" in s.set("technology").tolist(), s.get_meta().get("{cfg.marker_key}"))
mp.close_db()
""", IXMP_DATA=str(main / "ixmp"))
    assert f"DEFAULT {record['merged_version']} True merged from {job.resolve()} v{job_version}" in after.stdout
    repeat = ["merge", "--job-dir", str(job), "--scenario", SCEN, "--model", MODEL, "--allow-unsolved", "--apply"]
    again = run_cli(repeat, root, **menv)
    assert again.returncode == 3 and "was merged into" in again.stderr, again.stderr[-1000:]  # by its record
    record_path = next(cfg.records_dir.glob(f"merge_results_{SCEN}_v*.json"))
    record_path.rename(record_path.with_suffix(".aside"))
    again = run_cli(repeat, root, **menv)
    assert again.returncode == 3 and "already merged" in again.stderr, again.stderr[-1000:]  # by the main itself
    record_path.with_suffix(".aside").rename(record_path)
    assert dbc.verify(seed) == []

    # transfer: the original database to a fresh one; registry added, copy compared.
    fresh_db = tmp_path / "fresh" / "db"
    both = ixmp_home(tmp_path / "ixmp_both", {"proj-local": live_db, "fresh": fresh_db})
    py('import ixmp; ixmp.Platform("fresh").close_db()', IXMP_DATA=str(both))
    tenv = {"IXMP_DATA": str(both), **HEAP}
    moved = run_cli(["transfer", "--from", "proj-local", "--to", "fresh", "--scenario", SCEN, "--model", MODEL, "--apply"], root, **tenv)
    assert moved.returncode == 0, moved.stdout[-2000:] + moved.stderr[-2000:]
    trec = json.loads(next(cfg.records_dir.glob(f"transfer_fresh_{SCEN}_v1_*.json")).read_text())
    assert trec["compare"]["ok"] and trec["registry_added"]["regions"] >= 1
    assert Path(trec["pre_transfer_backup"]).parent == hd / "ixmp_backup" / "fresh"
    missing = run_cli(["transfer", "--from", "proj-local", "--to", "fresh", "--scenario", "nope", "--model", MODEL, "--apply"],
                      root, **tenv)
    assert missing.returncode == 3 and "no default version" in missing.stderr, missing.stderr[-1000:]
