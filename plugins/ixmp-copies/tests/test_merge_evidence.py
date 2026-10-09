"""What a merge needs before it touches the target: the run's own record of the version it left as
default, or an explicit --version; and no earlier merge of the same version from the same source.
All dry runs: these refusals come before any backup or JVM."""

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

    def merge(*extra):
        return run_cli(["merge", "--job-dir", str(job), "--scenario", "sc", *extra], cfg.project_root,
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
    assert merge("--version", "x").returncode == 2  # argparse: neither a number nor `default`


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
    run_result(scenario="other")
    assert "not sc" in merge().stderr


def _earlier(cfg, main: Path, marker: str) -> None:
    cluster_main = "/home/cluster/hdrive/ixmp_test/mains/results/db/db"  # the spelling a merge job writes
    cfg.records_dir.mkdir(parents=True, exist_ok=True)
    (cfg.records_dir / "merge_results_sc_v2_1.json").write_text(json.dumps(
        {"marker": marker, "into_db": cluster_main, "compare": {"ok": True}, "scenario": "sc"}))


def test_an_earlier_merge_is_refused_before_any_backup(setup):
    cfg, area, _, job, main, merge, run_result = setup
    run_result()
    _earlier(cfg, main, f"merged from {job.resolve()} v2")
    again = merge("--apply")
    assert again.returncode == 3 and "was merged into" in again.stderr, again.stderr
    assert not (area / "backups").exists() and not list(job.glob("merge_src_*"))


def test_seed_merges_are_marked_by_the_seed(setup):
    """Resubmitting a seed merge makes a new job folder; the marker must not change with it."""
    cfg, area, seed, job, main, merge, _ = setup
    (job / dbc.SEED_MERGE).write_text(str(seed) + "\n")
    _earlier(cfg, main, f"merged from {seed.resolve()} v4")
    again = merge("--version", "4")
    assert again.returncode == 3 and "was merged into" in again.stderr, again.stderr
    assert merge("--version", "5").returncode == 0
