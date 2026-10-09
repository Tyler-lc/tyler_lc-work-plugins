"""After the runs: cleanup of merged job copies, and resubmission of merges that did not happen."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from conftest import fake_db, run_cli
from test_cli_stage import _git_project, local_remote
from test_copies import SHARED, model_src

from ixmp_copies import copies as dbc
from ixmp_copies import stage
from ixmp_copies.copies import Refused


@pytest.mark.parametrize("path,area,want", [
    ("/home/cluster/hdrive/ixmp_test/jobs/a_1", "ixmp_test", "jobs/a_1"),
    ("/hdrive/u1/me/ixmp_copies/p/test/mains/m/db/db", "ixmp_copies/p/test", "mains/m/db/db"),
    ("/elsewhere/jobs/a_1", "ixmp_test", None),
    ("", "ixmp_test", None),
])
def test_within_area(path, area, want):
    assert dbc.within_area(path, area) == want


def _area(cfg, hd, tmp_path):
    backup_dir, _ = dbc.backup(fake_db(tmp_path / "live"), tmp_path / "bk", "l")
    seed, _, _ = dbc.seed(backup_dir, "test", "s", cfg)
    model = model_src(tmp_path)
    area = hd / "ixmp_test"
    for main in ("results", "other"):
        dbc.job_copy(seed, area / "mains" / main, "p", SHARED, model, "test", cfg, kind="main")

    def job(name, close=True, expected=None):
        d = area / "jobs" / name
        dbc.job_copy(seed, d, "p", SHARED, model, "test", cfg)
        if expected:
            (d / dbc.EXPECTED_MERGES).write_text("\n".join(expected) + "\n")
        if close:
            dbc.job_close(d)
        return d

    return area, job


def _record(cfg, job: Path, scenario: str, main: str = "results", ok: bool = True) -> None:
    # The cluster's spelling of the share, as a merge job writes it.
    cluster = Path("/home/cluster/hdrive/ixmp_test")
    cfg.records_dir.mkdir(parents=True, exist_ok=True)
    (cfg.records_dir / f"merge_{main}_{scenario}_{job.name}.json").write_text(json.dumps({
        "scenario": scenario, "job_dir": str(cluster / "jobs" / job.name),
        "into_db": str(cluster / "mains" / main / "db" / "db"), "compare": {"ok": ok}}))


def test_cleanup_deletes_only_proven_merges(project, tmp_path):
    cfg, hd = project
    area, job = _area(cfg, hd, tmp_path)
    merged = job("merged_1", expected=["s"])
    _record(cfg, merged, "s")
    failed = job("failed_2")
    _record(cfg, failed, "s", ok=False)
    job("nomerge_3")
    job("running_4", close=False)
    elsewhere = job("elsewhere_5")
    _record(cfg, elsewhere, "s", main="other")
    partial = job("seedmerge_6", expected=["a", "b"])
    _record(cfg, partial, "a")
    reopened = job("reopened_7")
    _record(cfg, reopened, "s")
    Path(f"{reopened / 'db' / 'db'}.properties").write_text("modified=yes\n")

    snapshot_records = area / "code" / "abc" / cfg.records
    snapshot_records.mkdir(parents=True)
    for rec in cfg.records_dir.glob("merge_results_s_merged_1.json"):
        (snapshot_records / rec.name).write_text(rec.read_text())  # as collect leaves it: in both places

    plan = dbc.cleanup_plan(cfg, "test", "results")
    assert [(j.name, len(r)) for j, r in plan["delete"]] == [("merged_1", 1)]
    kept = {j.name: why for j, why in plan["keep"]}
    assert "comparison failed" in kept["failed_2"]
    assert "no merge record" in kept["nomerge_3"]
    assert "not closed" in kept["running_4"]
    assert "elsewhere" in kept["elsewhere_5"]
    assert "['b']" in kept["seedmerge_6"]
    assert "database not closed" in kept["reopened_7"]

    _record(cfg, partial, "b")
    dry = run_cli(["cleanup", "--area", "test", "--main", "results"], cfg.project_root)
    assert dry.returncode == 0 and "delete  seedmerge_6" in dry.stdout and "dry run: 2" in dry.stdout, dry.stderr
    assert merged.exists() and partial.exists()
    done = run_cli(["cleanup", "--area", "test", "--main", "results", "--apply"], cfg.project_root)
    assert done.returncode == 0, done.stderr
    assert not merged.exists() and not partial.exists()
    assert all((area / "jobs" / n).exists() for n in ("failed_2", "nomerge_3", "running_4", "elsewhere_5", "reopened_7"))
    assert (area / "seeds" / "s").exists() and (area / "mains" / "results").exists()
    record = json.loads(next(cfg.records_dir.glob("cleanup_test_results_*.json")).read_text())
    assert len(record["deleted"]) == 2 and len(record["kept"]) == 5
    with pytest.raises(Refused, match="no results main"):
        dbc.cleanup_plan(cfg, "test", "nope")
    nomain = run_cli(["cleanup", "--area", "test", "--main", "nope"], cfg.project_root)
    assert nomain.returncode == 3


FAKE_SBATCH = """#!/bin/bash
n=$(( $(cat "$FAKE/next") + 1 )); echo $n > "$FAKE/next"
echo "$n|${SRC_JOB:-}|${SCENARIO:-}|$*" >> "$FAKE/sbatch.log"
echo $n
"""
FAKE_SACCT = """#!/bin/bash
while [ $# -gt 0 ]; do [ "$1" = -j ] && id=$2; shift; done
awk -v id="$id" '$1 == id {print $2}' "$FAKE/states"
"""


def test_submit_merges_resubmits_only_what_is_missing(project, tmp_path):
    cfg, hd = project
    _git_project(cfg)
    code = Path(stage.stage(cfg, "test", [], local_remote))
    area, job = _area(cfg, hd, tmp_path)
    jobs = {n: job(n) for n in ("a_1", "b_3", "c_5", "e_9", "f_11")}
    (jobs["e_9"] / "result.json").unlink()
    runs = area / "runs"
    runs.mkdir()
    rec = runs / "submitted_batch_into_results_1.txt"
    lines = [f"batch batch seed {area}/seeds/s main {area}/mains/results code {code} runs_file x",
             f"run a_1 job=1 dir={jobs['a_1']} scenario=sa merge=2 cmd=x",   # merged
             f"run b_3 job=3 dir={jobs['b_3']} scenario=sb merge=4 cmd=x",   # merge failed
             f"run c_5 job=5 dir={area}/jobs/c_5 scenario=sc merge=6 cmd=x",  # run still running
             f"run d_7 job=7 dir={area}/jobs/d_7 scenario=sd merge=8 cmd=x",  # run failed
             f"run e_9 job=9 dir={jobs['e_9']} scenario=se merge=10 cmd=x",  # completed, not closed
             f"run f_11 job=11 dir={jobs['f_11']} scenario=sf merge=12 cmd=x",  # merge cancelled...
             "run g_13 job=13 dir=/x/g_13 cmd=x",                            # nothing to merge
             f"run h_15 job=15 dir={area}/jobs/h_15 scenario=sh merge=16 cmd=x",  # merge still queued
             "remerge f_11 run=11 merge=20"]                                # ...and redone
    rec.write_text("\n".join(lines) + "\n")
    fake = tmp_path / "fake"
    (fake / "bin").mkdir(parents=True)
    (fake / "next").write_text("100")
    (fake / "states").write_text("1 COMPLETED\n2 COMPLETED\n3 COMPLETED\n4 FAILED\n5 RUNNING\n6 PENDING\n"
                                 "7 FAILED\n8 CANCELLED\n9 COMPLETED\n10 CANCELLED\n11 COMPLETED\n"
                                 "12 CANCELLED\n20 COMPLETED\n15 RUNNING\n16 PENDING\n")
    for name, text in (("sbatch", FAKE_SBATCH), ("sacct", FAKE_SACCT)):
        (fake / "bin" / name).write_text(text)
        (fake / "bin" / name).chmod(0o755)
    env = {"HOME": str(Path.home()), "PATH": f"{fake / 'bin'}:/usr/bin:/bin", "FAKE": str(fake), "CODE": str(code)}
    out = subprocess.run(["bash", str(code / ".ixmp_copies/slurm/submit_merges.sh"), str(rec)],
                         capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stdout + out.stderr
    log = (fake / "sbatch.log").read_text().splitlines()
    submitted = {line.split("|")[2]: line for line in log}
    assert sorted(submitted) == ["sb"], log  # c's merge 6 is still pending: not duplicated
    assert "--dependency=singleton" in submitted["sb"] and str(jobs["b_3"]) in submitted["sb"]
    for name, why in (("a_1", "merge 2 is COMPLETED"), ("c_5", "merge 6 is PENDING"), ("d_7", "run 7 is FAILED"),
                      ("e_9", "no result.json"), ("f_11", "merge 20 is COMPLETED"), ("h_15", "merge 16 is PENDING")):
        assert f"skip {name}: " in out.stdout and why in out.stdout, (name, out.stdout)
    assert "remerge b_3 run=3 merge=101" in rec.read_text()

    # The pending merge of c_5 lost: its run still running, so the new merge waits for it.
    (fake / "states").write_text((fake / "states").read_text().replace("\n6 PENDING\n", "\n6 CANCELLED\n")
                                 + "101 COMPLETED\n")
    again = subprocess.run(["bash", str(code / ".ixmp_copies/slurm/submit_merges.sh"), str(rec)],
                           capture_output=True, text=True, env=env)
    assert again.returncode == 0, again.stdout + again.stderr
    new = (fake / "sbatch.log").read_text().splitlines()[len(log):]
    assert len(new) == 1 and "|sc|" in new[0] and "afterok:5,singleton" in new[0], new
    assert "skip b_3: merge 101 is COMPLETED" in again.stdout
    assert os.access(code / ".ixmp_copies/slurm/job_merge_from_seed.do", os.X_OK)
