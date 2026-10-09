"""The whole chain on real HyperSQL databases, one JVM at a time: create a platform, back it up,
seed, a results main and a job copy, change a scenario on the job copy, close it, merge it
into the main (and refuse the repeat); merge two scenarios at one version from a seed, and refuse
the same version through a newer seed; then transfer a scenario into a platform never opened
before. With GAMS on PATH, also a solve in place on a job copy, merged, and a re-solve in place
refused. Skipped without ixmp, message_ix or java."""

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
s = make_dantzig(mp)
s.set_as_default()
s.clone(scenario="standard_x", keep_solution=False).set_as_default()  # v1 of another name
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
    marker = dbc.merge_marker(str(job.resolve()), MODEL, SCEN, job_version)
    assert f"DEFAULT {record['merged_version']} True {marker}" in after.stdout
    repeat = ["merge", "--job-dir", str(job), "--scenario", SCEN, "--model", MODEL, "--allow-unsolved", "--apply"]
    again = run_cli(repeat, root, **menv)
    assert again.returncode == 3 and "was merged into" in again.stderr, again.stderr[-1000:]  # by its record
    record_path = next(cfg.records_dir.glob(f"merge_results_{SCEN}_v*.json"))
    record_path.rename(record_path.with_suffix(".aside"))
    again = run_cli(repeat, root, **menv)
    assert again.returncode == 3 and "already merged" in again.stderr, again.stderr[-1000:]  # by the main itself
    assert "clone it" not in again.stderr  # a run job's copy cannot have changed since: no such advice
    record_path.with_suffix(".aside").rename(record_path)
    assert dbc.verify(seed) == []

    # Seed merges, SCENARIOS="standard:1 standard_x:1": both merge (a marker naming only the
    # source and the version refused the second). Then standard_x v1 again through a newer seed
    # of the same database: refused by its record, and, the record set aside, by the main.
    def seed_job(seed_dir, name):
        out = run_cli(["job-copy", "--seed", str(seed_dir), "--area", "live", "--job-dir", str(hd / "ixmp_live" / "jobs" / name)], root)
        assert out.returncode == 0, out.stderr
        assert run_cli(["job-close", "--job-dir", str(hd / "ixmp_live" / "jobs" / name)], root).returncode == 0
        (hd / "ixmp_live" / "jobs" / name / dbc.SEED_MERGE).write_text(f"{seed_dir}\n")
        return hd / "ixmp_live" / "jobs" / name

    from_seed = seed_job(seed, "merge_seed_1")
    for scen in (SCEN, "standard_x"):
        out = run_cli(["merge", "--job-dir", str(from_seed), "--scenario", scen, "--model", MODEL, "--version", "1",
                       "--allow-unsolved", "--apply"], root, **menv)
        assert out.returncode == 0, (scen, out.stdout[-1500:] + out.stderr[-1500:])
    later = run_cli(["backup", "--apply"], root, **env)
    assert later.returncode == 0, later.stderr
    backup2 = sorted((hd / "ixmp_backup" / "proj-local").iterdir())[-1]
    assert run_cli(["seed", "--from", str(backup2), "--area", "live", "--name", "s2", "--apply"], root).returncode == 0
    via_s2 = seed_job(hd / "ixmp_live" / "seeds" / "s2", "merge_seed_2")
    again_x = ["merge", "--job-dir", str(via_s2), "--scenario", "standard_x", "--model", MODEL, "--version", "1",
               "--allow-unsolved", "--apply"]
    refused = run_cli(again_x, root, **menv)
    assert refused.returncode == 3 and "was merged into" in refused.stderr, refused.stderr[-1000:]
    x_record = next(cfg.records_dir.glob("merge_results_standard_x_v1_*.json"))
    assert json.loads(x_record.read_text())["marker"] == dbc.merge_marker(str(live_db), MODEL, "standard_x", 1)
    x_record.rename(x_record.with_suffix(".aside"))
    by_main = run_cli(again_x, root, **menv)
    assert by_main.returncode == 3 and "already merged" in by_main.stderr, by_main.stderr[-1000:]
    assert "clone it to a new version there and merge that" in by_main.stderr, by_main.stderr[-1000:]
    x_record.with_suffix(".aside").rename(x_record)

    # transfer: the original database to one registered but never opened (no files yet: nothing
    # to back up); registry added, copy compared. A second transfer finds it and backs it up.
    fresh_db = tmp_path / "fresh" / "db"
    fresh_db.parent.mkdir()
    both = ixmp_home(tmp_path / "ixmp_both", {"proj-local": live_db, "fresh": fresh_db})
    tenv = {"IXMP_DATA": str(both), **HEAP}
    moved = run_cli(["transfer", "--from", "proj-local", "--to", "fresh", "--scenario", SCEN, "--model", MODEL, "--apply"], root, **tenv)
    assert moved.returncode == 0, moved.stdout[-2000:] + moved.stderr[-2000:]
    assert "a new database, nothing to back up" in moved.stdout
    trec = json.loads(next(cfg.records_dir.glob(f"transfer_fresh_{SCEN}_v1_*.json")).read_text())
    assert trec["compare"]["ok"] and trec["registry_added"]["regions"] >= 1 and trec["pre_transfer_backup"] is None
    assert dbc.require_cached_tables(fresh_db) > 0
    second = run_cli(["transfer", "--from", "proj-local", "--to", "fresh", "--scenario", "standard_x", "--model", MODEL,
                      "--apply"], root, **tenv)
    assert second.returncode == 0, second.stdout[-2000:] + second.stderr[-2000:]
    trec2 = json.loads(next(cfg.records_dir.glob("transfer_fresh_standard_x_v1_*.json")).read_text())
    assert Path(trec2["pre_transfer_backup"]).parent == hd / "ixmp_backup" / "fresh"
    missing = run_cli(["transfer", "--from", "proj-local", "--to", "fresh", "--scenario", "nope", "--model", MODEL, "--apply"],
                      root, **tenv)
    assert missing.returncode == 3 and "no default version" in missing.stderr, missing.stderr[-1000:]


SOLVE_IN_PLACE = """
import ixmp, message_ix
mp = ixmp.Platform()
s = message_ix.Scenario(mp, "{model}", "{scen}")
if s.has_solution():
    s.remove_solution()  # ixmp solves no scenario that has a solution
s.solve(quiet=True)
s.set_as_default()
print("SOLVED", s.version, s.has_solution())
mp.close_db()
"""


@pytest.mark.skipif(not shutil.which("gams"), reason="a solve needs gams on PATH")
def test_solve_in_place(project, tmp_path):
    """A run that solves the seed's default version in place (no clone) leaves the same default
    version, now solved: accepted and merged. Its GDX files in model/ keep the copy from cleanup.
    A run that re-solves an already solved default in place is refused: nothing tells it from a
    command that did nothing."""
    cfg, hd = project
    root = cfg.project_root
    live_db = tmp_path / "live" / "db"
    live_home = ixmp_home(tmp_path / "ixmp_live", {"proj-local": live_db})
    py(CREATE.format(name="proj-local"), IXMP_DATA=str(live_home))
    assert run_cli(["backup", "--apply"], root, IXMP_DATA=str(live_home)).returncode == 0
    backup = next((hd / "ixmp_backup" / "proj-local").iterdir())
    assert run_cli(["seed", "--from", str(backup), "--area", "live", "--name", "s1", "--apply"], root).returncode == 0
    jobs, main = hd / "ixmp_live" / "jobs", hd / "ixmp_live" / "mains" / "results"
    assert run_cli(["job-copy", "--seed", str(hd / "ixmp_live" / "seeds" / "s1"), "--area", "live", "--kind", "main",
                    "--job-dir", str(main)], root).returncode == 0

    def run_job(seed_dir, job_dir):
        out = run_cli(["job-copy", "--seed", str(seed_dir), "--area", "live", "--job-dir", str(job_dir)], root)
        assert out.returncode == 0, out.stderr
        jenv = {"IXMP_DATA": str(job_dir / "ixmp"), **HEAP}
        assert run_cli(["job-check", "--job-dir", str(job_dir)], root, **jenv).returncode == 0
        mark = ["run-mark", "--job-dir", str(job_dir), "--scenario", SCEN, "--model", MODEL]
        assert run_cli([*mark, "--before"], root, **jenv).returncode == 0
        solved = py(SOLVE_IN_PLACE.format(model=MODEL, scen=SCEN), IXMP_DATA=str(job_dir / "ixmp"))
        assert "SOLVED 1 True" in solved.stdout, solved.stdout
        after = run_cli([*mark, "--after"], root, **jenv)
        assert run_cli(["job-close", "--job-dir", str(job_dir)], root).returncode == 0
        return after

    first = run_job(hd / "ixmp_live" / "seeds" / "s1", jobs / "inplace_1")
    assert first.returncode == 0 and "solved in place" in first.stdout, first.stdout + first.stderr[-1500:]
    result = json.loads((jobs / "inplace_1" / dbc.RUN_RESULT).read_text())
    assert result["accepted"] and result["in_place"] and not result["new"] and result["default"] == 1
    outputs = dbc.job_outputs(cfg, jobs / "inplace_1")
    assert any(o.startswith("model/output/") and o.endswith(".gdx") for o in outputs), outputs

    menv = {"IXMP_DATA": str(main / "ixmp"), **HEAP}
    merged = run_cli(["merge", "--job-dir", str(jobs / "inplace_1"), "--scenario", SCEN, "--model", MODEL, "--apply"],
                     root, **menv)
    assert merged.returncode == 0, merged.stdout[-2000:] + merged.stderr[-2000:]
    record = json.loads(next(cfg.records_dir.glob(f"merge_results_{SCEN}_v1_*.json")).read_text())
    assert record["compare"]["ok"] and record["compare"]["solved"] == [True, True] and record["set_default"]
    assert record["run"]["in_place"]

    # From a seed of the solved copy, the same in-place solve again: solved before and after.
    assert run_cli(["seed", "--from-job", str(jobs / "inplace_1"), "--area", "live", "--name", "solved",
                    "--apply"], root).returncode == 0
    again = run_job(hd / "ixmp_live" / "seeds" / "solved", jobs / "inplace_2")
    assert again.returncode == 3 and "Clone to a new version" in again.stderr, again.stdout + again.stderr[-1500:]
    refused = run_cli(["merge", "--job-dir", str(jobs / "inplace_2"), "--scenario", SCEN, "--model", MODEL, "--apply"],
                      root, **menv)
    assert refused.returncode == 3 and "re-solve in place" in refused.stderr, refused.stderr[-1000:]
