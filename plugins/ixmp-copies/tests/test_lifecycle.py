"""After the runs: cleanup of merged job copies (and of read jobs, and one job copy on request), and
resubmission of merges that did not happen."""

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

    def job(name, close=True, expected=None, code=False):
        """expected: the scenarios to merge ([] for a read job); code: a code copy as job_run.do makes it."""
        d = area / "jobs" / name
        dbc.job_copy(seed, d, "p", SHARED, model, "test", cfg)
        if expected is not None:
            (d / dbc.EXPECTED_MERGES).write_text("".join(f"{s}\n" for s in expected))
        if code:
            (d / "code" / "pkg").mkdir(parents=True)
            (d / "code" / "pkg" / "run.py").write_text("print('staged')\n")
            (d / "code" / cfg.records).mkdir()
            (d / "code" / cfg.records / "staged.json").write_text("{}")
            dbc.record_code_files(d)
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
echo "$n|${SRC_JOB:-}|${SCENARIO:-}|${MODEL:-}|$*" >> "$FAKE/sbatch.log"
echo $n
"""
# states: "id State [ExitCode]" per line; ExitCode defaults to 0:0.
FAKE_SACCT = """#!/bin/bash
while [ $# -gt 0 ]; do case "$1" in -j) id=$2 ;; -o) field=$2 ;; esac; shift; done
awk -v id="$id" -v f="$field" '$1 == id {print (f == "ExitCode" ? ($3 == "" ? "0:0" : $3) : $2)}' "$FAKE/states"
"""


def test_submit_merges_resubmits_only_what_is_missing(project, tmp_path):
    cfg, hd = project
    _git_project(cfg)
    code = Path(stage.stage(cfg, "test", [], local_remote))
    area, job = _area(cfg, hd, tmp_path)
    jobs = {n: job(n) for n in ("a_1", "b_3", "c_5", "e_9", "f_11", "i_17")}
    (jobs["e_9"] / "result.json").unlink()
    runs = area / "runs"
    runs.mkdir()
    rec = runs / "submitted_batch_into_results_1.txt"
    lines = [f"batch batch seed {area}/seeds/s main {area}/mains/results code {code} runs_file x",
             f"run a_1 job=1 dir={jobs['a_1']} scenario=sa merge=2 cmd=x",   # merged
             f"run b_3 job=3 dir={jobs['b_3']} scenario=sb merge=4 model=Canning problem (MESSAGE scheme) "
             "cmd=python run.py --scenario=bad --out-dir=results job=9 merge=2 model=evil",  # merge failed;
             # its model holds spaces, its command mimics every field
             f"run c_5 job=5 dir={area}/jobs/c_5 scenario=sc merge=6 cmd=x",  # run still running
             f"run d_7 job=7 dir={area}/jobs/d_7 scenario=sd merge=8 cmd=x",  # run failed
             f"run e_9 job=9 dir={jobs['e_9']} scenario=se merge=10 cmd=x",  # completed, not closed
             f"run f_11 job=11 dir={jobs['f_11']} scenario=sf merge=12 cmd=x",  # merge cancelled...
             "run g_13 job=13 dir=/x/g_13 cmd=x",                            # nothing to merge
             f"run h_15 job=15 dir={area}/jobs/h_15 scenario=sh merge=16 cmd=x",  # merge still queued
             f"run i_17 job=17 dir={jobs['i_17']} scenario=si merge=18 model=m cmd=x",  # merge failed after
             "remerge f_11 run=11 merge=20"]                                # ...and redone  its backup
    rec.write_text("\n".join(lines) + "\n")
    fake = tmp_path / "fake"
    (fake / "bin").mkdir(parents=True)
    (fake / "next").write_text("100")
    (fake / "states").write_text("1 COMPLETED\n2 COMPLETED\n3 COMPLETED\n4 FAILED\n5 RUNNING\n6 PENDING\n"
                                 "7 FAILED\n8 CANCELLED\n9 COMPLETED\n10 CANCELLED\n11 COMPLETED\n"
                                 "12 CANCELLED\n20 COMPLETED\n15 RUNNING\n16 PENDING\n17 COMPLETED\n"
                                 "18 FAILED 4:0\n")
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
    assert submitted["sb"].split("|")[3] == "Canning problem (MESSAGE scheme)"  # the record's model
    for name, why in (("a_1", "merge 2 is COMPLETED"), ("c_5", "merge 6 is PENDING"), ("d_7", "run 7 is FAILED"),
                      ("e_9", "no result.json"), ("f_11", "merge 20 is COMPLETED"), ("h_15", "merge 16 is PENDING"),
                      ("i_17", "merge 18 exited 4 (failed after its backup): check the main")):
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
    assert new[0].split("|")[3] == ""  # a record line of 0.3.0 names no model: [project] model
    assert "skip b_3: merge 101 is COMPLETED" in again.stdout
    assert os.access(code / ".ixmp_copies/slurm/job_merge_from_seed.do", os.X_OK)
    # c_5's new merge is cancelled too: resubmitted with MODEL, which fills in for lines naming none.
    (fake / "states").write_text((fake / "states").read_text() + "102 CANCELLED\n103 COMPLETED\n")
    with_model = subprocess.run(["bash", str(code / ".ixmp_copies/slurm/submit_merges.sh"), str(rec)],
                                capture_output=True, text=True, env={**env, "MODEL": "M_env"})
    third = (fake / "sbatch.log").read_text().splitlines()[len(log) + 1:]
    assert len(third) == 1 and third[0].split("|")[2:4] == ["sc", "M_env"], (third, with_model.stdout)


def test_submit_merges_reads_the_main_of_a_batch_named_main(project, tmp_path):
    """A runs file called main.txt: the batch line holds the word main twice."""
    cfg, hd = project
    _git_project(cfg)
    code = Path(stage.stage(cfg, "test", [], local_remote))
    area, _ = _area(cfg, hd, tmp_path)
    (area / "runs").mkdir()
    rec = area / "runs" / "submitted_main_into_results_1.txt"
    rec.write_text(f"batch main seed {area}/seeds/s main {area}/mains/results code {code} runs_file /r/main.txt\n")
    env = {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin", "CODE": str(code)}
    out = subprocess.run(["bash", str(code / ".ixmp_copies/slurm/submit_merges.sh"), str(rec)],
                         capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "merges resubmitted" in rec.read_text()


def test_cleanup_keeps_what_a_run_wrote_into_code_and_model(project, tmp_path):
    """Outputs outside the job folder's top level: new or changed files in the code copy (json
    records excepted: collect brings those), GDX files (equation duals in model/output) and
    listings in the model copy; a code copy with no code_files.json at all."""
    cfg, hd = project
    area, job = _area(cfg, hd, tmp_path)
    plain = job("plain_1", code=True)
    (plain / "code" / "pkg" / "__pycache__").mkdir()
    (plain / "code" / "pkg" / "__pycache__" / "run.cpython-311.pyc").write_bytes(b"x")  # every import writes one
    (plain / "code" / cfg.records / "run_plain.json").write_text("{}")
    (cfg.records_dir).mkdir(parents=True, exist_ok=True)
    (cfg.records_dir / "run_plain.json").write_text("{}")  # collected
    (cfg.records_dir / "staged.json").write_text("{}")
    report = job("report_2", code=True)
    (report / "code" / "results" / "v6.6").mkdir(parents=True)
    (report / "code" / "results" / "v6.6" / "report.xlsx").write_text("only copy")
    (report / "code" / cfg.records / "run_summary.csv").write_text("only copy")
    changed = job("changed_3", code=True)
    (changed / "code" / "pkg" / "run.py").write_text("print('edited by the run')\n")
    duals = job("duals_4", code=True)
    (duals / "model" / "output" / "MsgOutput_m_sc.gdx").write_text("duals")
    (duals / "model" / "MESSAGE_run.lst").write_text("listing")
    old = job("old_5")
    (old / "code").mkdir()
    (old / "code" / "run.py").write_text("x")
    for j in (plain, report, changed, duals, old):
        _record(cfg, j, "s")
    assert dbc.job_outputs(cfg, plain) == []
    assert dbc.job_outputs(cfg, report) == ["code/records/run_summary.csv", "code/results/v6.6/report.xlsx"]
    assert dbc.job_outputs(cfg, changed) == ["code/pkg/run.py"]
    assert dbc.job_outputs(cfg, duals) == ["model/MESSAGE_run.lst", "model/output/MsgOutput_m_sc.gdx"]
    assert "no code_files.json" in dbc.job_outputs(cfg, old)[0]
    plan = dbc.cleanup_plan(cfg, "test", "results")
    assert [j.name for j, _ in plan["delete"]] == ["plain_1"]
    kept = {j.name: why for j, why in plan["keep"]}
    assert "report.xlsx" in kept["report_2"] and "run_summary.csv" in kept["report_2"]
    assert "MsgOutput_m_sc.gdx" in kept["duals_4"] and "--include-outputs" in kept["duals_4"]
    assert "code/pkg/run.py" in kept["changed_3"] and "no code_files.json" in kept["old_5"]
    every = dbc.cleanup_plan(cfg, "test", "results", include_outputs=True)
    assert sorted(j.name for j, _ in every["delete"]) == ["changed_3", "duals_4", "old_5", "plain_1", "report_2"]
    with pytest.raises(Refused, match="copy the code"):
        dbc.record_code_files(old.parent / "nonexistent")


def test_cleanup_of_read_jobs_and_discard(project, tmp_path):
    """A job declared to merge nothing (scenario -: an empty expected_merges.txt) goes once its records
    are collected and it holds no output; a job submitted by hand (no expected_merges.txt) stays.
    --discard deletes one closed copy whatever its merges, with a reason, never an open one."""
    cfg, hd = project
    area, job = _area(cfg, hd, tmp_path)
    cfg.records_dir.mkdir(parents=True)
    (cfg.records_dir / "staged.json").write_text("{}")  # the code copies' staged record, collected
    reader = job("read_1", expected=[], code=True)
    writer = job("read_2", expected=[], code=True)
    (writer / "code" / "out.csv").write_text("x")
    by_hand = job("hand_3")
    failed = job("failed_4", expected=["s"])
    running = job("running_5", close=False)
    plan = dbc.cleanup_plan(cfg, "test", "results")
    assert [(j.name, r) for j, r in plan["delete"]] == [("read_1", [])]
    kept = {j.name: why for j, why in plan["keep"]}
    assert "out.csv" in kept["read_2"] and "no merge record" in kept["hand_3"] and "no merge record" in kept["failed_4"]
    dry = run_cli(["cleanup", "--area", "test", "--main", "results"], cfg.project_root)
    assert dry.returncode == 0 and "delete  read_1: meant to merge nothing" in dry.stdout, dry.stdout + dry.stderr

    def discard(*args):
        return run_cli(["cleanup", "--area", "test", *args], cfg.project_root)

    assert discard().returncode == 3  # neither --main nor --discard
    noreason = discard("--discard", "failed_4")
    assert noreason.returncode == 3 and "--reason" in noreason.stderr
    both = discard("--discard", "failed_4", "--reason", "x", "--main", "results")
    assert both.returncode == 3 and "without --main" in both.stderr
    assert discard("--discard", "nope_9", "--reason", "x").returncode == 3
    still_running = discard("--discard", running.name, "--reason", "x", "--apply")
    assert still_running.returncode == 3 and "not closed" in still_running.stderr and running.exists()
    with_output = discard("--discard", writer.name, "--reason", "x", "--apply")
    assert with_output.returncode == 3 and "out.csv" in with_output.stderr and writer.exists()
    dry = discard("--discard", failed.name, "--reason", "the run failed; not needed")
    assert dry.returncode == 0 and "dry run" in dry.stdout and failed.exists()
    done = discard("--discard", failed.name, "--reason", "the run failed; not needed", "--apply")
    assert done.returncode == 0 and not failed.exists(), done.stderr
    record = json.loads(next(cfg.records_dir.glob("cleanup_test_discard_failed_4_*.json")).read_text())
    assert record["reason"] == "the run failed; not needed" and record["discarded"].endswith("failed_4")
    forced = discard("--discard", writer.name, "--reason", "copied out", "--include-outputs", "--apply")
    assert forced.returncode == 0 and not writer.exists()
    assert by_hand.exists() and reader.exists() and running.exists()


def test_cleanup_keeps_uncollected_records_and_run_outputs(project, tmp_path):
    cfg, hd = project
    area, job = _area(cfg, hd, tmp_path)
    a = job("a_1")
    _record(cfg, a, "s")
    (a / "code" / cfg.records).mkdir(parents=True)
    dbc.record_code_files(a)  # as job_run.do does right after copying the code
    (a / "code" / cfg.records / "run_a.json").write_text('{"solved": 1}')
    b = job("b_2")
    _record(cfg, b, "s")
    (b / "results.csv").write_text("x")  # a run's output in $IXC_JOB_DIR
    plan = dbc.cleanup_plan(cfg, "test", "results")
    kept = {j.name: why for j, why in plan["keep"]}
    assert "run_a.json" in kept["a_1"] and "collect" in kept["a_1"]
    assert "results.csv" in kept["b_2"] and not plan["delete"]
    (cfg.records_dir / "run_a.json").write_text('{"solved": 0}')  # collected, but it differs
    assert "run_a.json" in dict((j.name, w) for j, w in dbc.cleanup_plan(cfg, "test", "results")["keep"])["a_1"]
    (cfg.records_dir / "run_a.json").write_text('{"solved": 1}')
    plan = dbc.cleanup_plan(cfg, "test", "results", include_outputs=True)
    assert sorted(j.name for j, _ in plan["delete"]) == ["a_1", "b_2"]


def test_collect_updates_a_submission_record_that_grew(project, tmp_path):
    cfg, hd = project
    runs = hd / "ixmp_test" / "runs"
    runs.mkdir(parents=True)
    rec = runs / "submitted_batch_into_results_1.txt"
    rec.write_text("batch b\nrun a_1 job=1 merge=2\n")
    assert stage.collect(cfg, hd / "ixmp_test")["copied"] == [rec.name]
    rec.write_text(rec.read_text() + "remerge a_1 run=1 merge=5\n")  # submit_merges.sh appends
    out = stage.collect(cfg, hd / "ixmp_test")
    assert out["updated"] == [rec.name] and not out["conflicts"]
    assert (cfg.records_dir / rec.name).read_text().endswith("merge=5\n")
    rec.write_text("rewritten\n")  # not an extension of what was collected: never overwritten
    clash = stage.collect(cfg, hd / "ixmp_test")
    assert clash["conflicts"] and (cfg.records_dir / rec.name).read_text().endswith("merge=5\n")


def test_a_merge_source_not_closed_keeps_the_job(project, tmp_path):
    """A merge copies the job's database to merge_src_<time>/ inside the job folder and opens that copy:
    while it is open (a merge running on another host) or left open (a merge that died), neither
    cleanup nor --discard deletes the folder. A closed merge source is no obstacle."""
    cfg, hd = project
    area, job = _area(cfg, hd, tmp_path)
    merging = job("merging_1", expected=["s"])
    _record(cfg, merging, "s")
    src = merging / "merge_src_20261009_120000"
    fake_db(src, modified="yes")
    Path(f"{src / 'db'}.lck").write_text("x")
    done = job("done_2", expected=["s"])
    _record(cfg, done, "s")
    fake_db(done / "merge_src_20261009_110000")  # an earlier merge, finished: closed
    plan = dbc.cleanup_plan(cfg, "test", "results")
    assert [j.name for j, _ in plan["delete"]] == ["done_2"]
    kept = dict((j.name, why) for j, why in plan["keep"])["merging_1"]
    assert "merge_src_20261009_120000 not closed" in kept and ".lck" in kept, kept
    with pytest.raises(Refused, match="a merge from it is running or did not finish"):
        dbc.discard_check(cfg, "test", merging.name, include_outputs=True)
    out = run_cli(["cleanup", "--area", "test", "--discard", merging.name, "--reason", "x", "--apply"], cfg.project_root)
    assert out.returncode == 3 and merging.exists(), out.stdout + out.stderr
    half = done / "merge_src_20261009_130000.partial"  # a copy being made: no properties yet
    half.mkdir()
    Path(f"{half / 'db'}.data").write_bytes(b"x")
    assert "not closed" in dict((j.name, w) for j, w in dbc.cleanup_plan(cfg, "test", "results")["keep"])["done_2"]


def test_cleanup_reads_only_what_it_must_of_a_code_copy(project, tmp_path, monkeypatch):
    """New files and files whose size changed are output without being hashed (a run's outputs can be
    large and the share slow); a file of the recorded size is hashed, so a change keeping the size is
    still found."""
    cfg, hd = project
    area, job = _area(cfg, hd, tmp_path)
    j = job("j_1", code=True)
    (j / "code" / "pkg" / "big_output.csv").write_text("x" * 10_000)
    (j / "code" / "pkg" / "more.txt").write_text("new")
    hashed = []
    real = dbc.digest
    monkeypatch.setattr(dbc, "digest", lambda path: hashed.append(Path(path).name) or real(path))
    assert dbc.job_outputs(cfg, j) == ["code/pkg/big_output.csv", "code/pkg/more.txt"]
    assert hashed == ["run.py"], hashed  # the only file of its recorded size
    (j / "code" / "pkg" / "run.py").write_text("print('stAged')\n")  # same size, other bytes
    assert "code/pkg/run.py" in dbc.job_outputs(cfg, j)
    (j / "code" / "pkg" / "run.py").write_text("print('staged, longer')\n")
    hashed.clear()
    assert "code/pkg/run.py" in dbc.job_outputs(cfg, j) and hashed == [], hashed


def test_submit_merges_resubmits_a_merge_that_exited_4_only_when_told(project, tmp_path):
    """A merge that exited 4 is skipped (the main may hold what it made) until the user, having checked
    the main, names the run in FORCE_RUNS; naming a run whose merge did not exit 4 changes nothing.
    The record is in the form of 0.4.1: a run line, then its merge's line."""
    cfg, hd = project
    _git_project(cfg)
    code = Path(stage.stage(cfg, "test", [], local_remote))
    area, job = _area(cfg, hd, tmp_path)
    jobs = {n: job(n) for n in ("a_1", "b_3")}
    (area / "runs").mkdir()
    rec = area / "runs" / "submitted_batch_into_results_1.txt"
    rec.write_text("\n".join([
        f"batch batch seed {area}/seeds/s main {area}/mains/results code {code} runs_file x",
        f"run a_1 job=1 dir={jobs['a_1']} scenario=sa model=m cmd=python a.py merge=9",  # merge exited 4
        "merge a_1 merge=2",
        f"run b_3 job=3 dir={jobs['b_3']} scenario=sb model=m cmd=x",  # merged
        "merge b_3 merge=4"]) + "\n")
    fake = tmp_path / "fake"
    (fake / "bin").mkdir(parents=True)
    (fake / "next").write_text("100")
    (fake / "states").write_text("1 COMPLETED\n2 FAILED 4:0\n3 COMPLETED\n4 COMPLETED\n9 COMPLETED\n")
    for name, text in (("sbatch", FAKE_SBATCH), ("sacct", FAKE_SACCT)):
        (fake / "bin" / name).write_text(text)
        (fake / "bin" / name).chmod(0o755)
    env = {"HOME": str(Path.home()), "PATH": f"{fake / 'bin'}:/usr/bin:/bin", "FAKE": str(fake), "CODE": str(code)}

    def submit(**extra):
        return subprocess.run(["bash", str(code / ".ixmp_copies/slurm/submit_merges.sh"), str(rec)],
                              capture_output=True, text=True, env={**env, **extra})

    plain = submit()
    assert plain.returncode == 0 and "skip a_1: merge 2 exited 4" in plain.stdout, plain.stdout + plain.stderr
    assert "FORCE_RUNS=a_1" in plain.stdout and "skip b_3: merge 4 is COMPLETED" in plain.stdout, plain.stdout
    assert not (fake / "sbatch.log").exists()
    other = submit(FORCE_RUNS="b_3 x_9")
    assert "skip a_1: merge 2 exited 4" in other.stdout and "skip b_3: merge 4 is COMPLETED" in other.stdout
    assert not (fake / "sbatch.log").exists()
    forced = submit(FORCE_RUNS="z_0 a_1")
    assert forced.returncode == 0, forced.stdout + forced.stderr
    log = (fake / "sbatch.log").read_text().splitlines()
    assert len(log) == 1 and log[0].split("|")[1:3] == [str(jobs["a_1"]), "sa"], log
    assert "remerge a_1 run=1 merge=101 forced" in rec.read_text()
    (fake / "states").write_text((fake / "states").read_text() + "101 COMPLETED\n")
    after = submit(FORCE_RUNS="a_1")  # the forced merge is now the latest: completed, skipped
    assert "skip a_1: merge 101 is COMPLETED" in after.stdout and len((fake / "sbatch.log").read_text().splitlines()) == 1
