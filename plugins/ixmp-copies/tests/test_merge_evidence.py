"""What a merge needs before it touches the target: the run's own record of the version it left as
default, about the model and scenario merged, or an explicit --version; and no earlier merge of the
same version of the same scenario from the same source. All dry runs: these refusals come before any
backup or JVM."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import fake_db, has_ixmp, run_cli
from test_copies import SHARED, model_src

from ixmp_copies import copies as dbc

pytestmark = pytest.mark.skipif(not has_ixmp(), reason="merge resolves the target through ixmp's config")


@pytest.fixture
def setup(project, tmp_path):
    cfg, hd = project
    backup_dir, _ = dbc.backup(fake_db(tmp_path / "live"), tmp_path / "bk", "l")
    seed, _, _ = dbc.seed(backup_dir, "test", "s", cfg)
    model = model_src(tmp_path)
    area = hd / "ixmp_test"
    main = area / "mains" / "results"
    dbc.job_copy(seed, main, cfg.platform, SHARED, model, "test", cfg, kind="main")
    job = area / "jobs" / "run_1"
    dbc.job_copy(seed, job, cfg.platform, SHARED, model, "test", cfg)
    dbc.job_close(job)

    def merge(*extra, scenario="sc", job_dir=job):
        return run_cli(["merge", "--job-dir", str(job_dir), "--scenario", scenario, *extra], cfg.project_root,
                       IXMP_DATA=str(main / "ixmp"))

    def run_result(**fields):
        record = {"model": "m", "scenario": "sc", "versions": [1, 2], "default": 2, "solved": True,
                  "before": [1], "new": True, **fields}
        (job / dbc.RUN_RESULT).write_text(json.dumps(record))

    return cfg, area, seed, job, main, merge, run_result


def test_without_run_record_a_version_must_be_named(setup):
    _, _, _, _, _, merge, _ = setup
    bare = merge()
    assert bare.returncode == 3 and "no run record" in bare.stderr, bare.stderr
    assert merge("--version", "default").returncode == 0
    assert merge("--version", "3").returncode == 0
    bad = merge("--version", "x")  # neither a number nor `default`: a command line refused
    assert bad.returncode == 3 and "REFUSED" in bad.stderr, bad.stderr


def test_the_run_record_decides(setup):
    _, _, _, _, _, merge, run_result = setup
    run_result()
    ok = merge()
    assert ok.returncode == 0 and "sc v2 from" in ok.stdout, ok.stderr
    assert merge("--version", "default").returncode == 0
    wrong = merge("--version", "1")
    assert wrong.returncode == 3 and "not the version the run left" in wrong.stderr
    run_result(default=1, new=False)
    stale = merge()
    assert stale.returncode == 3 and "set_as_default" in stale.stderr, stale.stderr
    run_result(solved=False)
    unsolved = merge()
    assert unsolved.returncode == 3 and "no solution" in unsolved.stderr
    assert merge("--allow-unsolved").returncode == 0
    run_result(accepted=False, new=False, reason="v1 of m/sc was the default, and solved, before the run")
    told = merge()
    assert told.returncode == 3 and "solved, before the run" in told.stderr, told.stderr
    run_result(new=False, accepted=True, in_place=True)  # solved in place: accepted
    assert merge().returncode == 0
    run_result(scenario="other")
    assert "not m/sc" in merge().stderr


def test_a_run_record_about_another_model_is_refused(setup):
    """The record names the model the run marked; a merge of another model's scenario of the same
    name would clone a version the run never made."""
    _, _, _, _, _, merge, run_result = setup
    run_result(model="OTHER_MODEL")
    out = merge()  # model from [project] model, "m"
    assert out.returncode == 3 and "OTHER_MODEL/sc, not m/sc" in out.stderr, out.stdout + out.stderr
    ok = merge("--model", "OTHER_MODEL")
    assert ok.returncode == 0 and "OTHER_MODEL/sc v2" in ok.stdout, ok.stderr


def _earlier(cfg, main: Path, marker: str, scenario: str = "sc", n: int = 1) -> None:
    cluster_main = "/home/cluster/hdrive/ixmp_test/mains/results/db/db"  # the spelling a merge job writes
    cfg.records_dir.mkdir(parents=True, exist_ok=True)
    (cfg.records_dir / f"merge_results_{scenario}_v2_{n}.json").write_text(json.dumps(
        {"marker": marker, "into_db": cluster_main, "compare": {"ok": True}, "scenario": scenario, "model": "m"}))


def test_an_earlier_merge_is_refused_before_any_backup(setup):
    cfg, area, _, job, main, merge, run_result = setup
    run_result()
    _earlier(cfg, main, dbc.merge_marker(str(job.resolve()), "m", "sc", 2))
    again = merge("--apply")
    assert again.returncode == 3 and "was merged into" in again.stderr, again.stderr
    assert not (area / "backups").exists() and not list(job.glob("merge_src_*"))


def test_a_record_of_0_3_0_still_refuses_its_own_repeat(setup):
    """0.3.0 markers named no scenario: read together with the record's scenario and model."""
    cfg, _, _, job, main, merge, run_result = setup
    run_result()
    _earlier(cfg, main, f"merged from {job.resolve()} v2", scenario="other")
    assert merge().returncode == 0  # about another scenario: not this merge
    _earlier(cfg, main, f"merged from {job.resolve()} v2", n=2)
    again = merge()
    assert again.returncode == 3 and "was merged into" in again.stderr, again.stderr


def _seed_job(cfg, seed: Path, area: Path, name: str, model: Path) -> Path:
    job = area / "jobs" / name
    dbc.job_copy(seed, job, cfg.platform, SHARED, model, "test", cfg)
    dbc.job_close(job)
    (job / dbc.SEED_MERGE).write_text(str(seed) + "\n")
    return job


def test_seed_merges_are_marked_by_where_the_versions_came_from(setup, tmp_path):
    """A resubmitted seed merge makes a new job folder, and a newer seed of the same database
    another seed folder: the marker is the database the versions came from, in every one."""
    cfg, area, seed, job, main, merge, _ = setup
    live = (tmp_path / "live" / "db")  # what the seed's backup copied
    assert dbc.seed_origin(seed) == str(live)
    (job / dbc.SEED_MERGE).write_text(str(seed) + "\n")
    _earlier(cfg, main, dbc.merge_marker(str(live), "m", "sc", 4))
    again = merge("--version", "4")
    assert again.returncode == 3 and "was merged into" in again.stderr, again.stderr
    assert merge("--version", "5").returncode == 0
    later_backup, _ = dbc.backup(live, tmp_path / "bk", "l2")
    seed2, _, _ = dbc.seed(later_backup, "test", "s2", cfg)
    via_seed2 = merge("--version", "4", job_dir=_seed_job(cfg, seed2, area, "merge_seed_2", tmp_path / "model_src"))
    assert via_seed2.returncode == 3 and "was merged into" in via_seed2.stderr, via_seed2.stderr


def test_seed_merges_of_several_scenarios_at_one_version(setup):
    """SCENARIOS="a:1 b:1": the merge of a v1 does not refuse b v1 (0.3.0's marker named only the
    seed and the version); a record of 0.3.0 about a v1 still refuses a v1."""
    cfg, _, seed, job, main, merge, _ = setup
    (job / dbc.SEED_MERGE).write_text(str(seed) + "\n")
    _earlier(cfg, main, f"merged from {seed.resolve()} v1", scenario="a")
    b = merge("--version", "1", scenario="b")
    assert b.returncode == 0 and "m/b v1" in b.stdout, b.stderr
    a = merge("--version", "1", scenario="a")
    assert a.returncode == 3 and "was merged into" in a.stderr, a.stderr
    _earlier(cfg, main, dbc.merge_marker(dbc.seed_origin(seed), "m", "b", 1), scenario="b")
    assert merge("--version", "1", scenario="b").returncode == 3
