"""What a merge needs before it touches the target: the run's own record of the version it left as
default, about the model and scenario merged, or an explicit --version; and no earlier merge of the
same version of the same scenario from the same source. Dry runs: these refusals come before any
backup or JVM; and what a merge or transfer failing after its backup says, with the platforms faked."""

from __future__ import annotations

import json
import time
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


def test_a_run_that_did_not_complete_is_not_merged(setup):
    """run-mark --before ran, run-mark --after did not (the command failed): no version of the copy is
    known to be the run's result. --version default would bring back the seed's version; refused, as is
    any version, unless named with --despite-failed-run (a merge by hand after checking it)."""
    _, area, _, job, _, merge, _ = setup
    (job / dbc.RUN_BEFORE).write_text(json.dumps(
        {"model": "m", "scenario": "sc", "versions": [1], "default": 1, "solved": False}))
    for extra in ((), ("--version", "default"), ("--version", "1"), ("--version", "default", "--despite-failed-run"),
                  ("--version", "default", "--apply")):
        out = merge(*extra)
        assert out.returncode == 3 and "did not complete" in out.stderr, (extra, out.stdout + out.stderr)
        assert "--version default" not in out.stderr.split("REFUSED:", 1)[1], out.stderr  # no longer invited
    assert not (area / "backups").exists() and not list(job.glob("merge_src_*"))
    by_hand = merge("--version", "1", "--despite-failed-run")
    assert by_hand.returncode == 0 and "dry run" in by_hand.stdout, by_hand.stdout + by_hand.stderr


def test_despite_failed_run_only_where_a_run_did_not_complete(setup):
    _, _, _, job, _, merge, run_result = setup
    none = merge("--version", "1", "--despite-failed-run")  # no run records at all
    assert none.returncode == 3 and "no run records" in none.stderr, none.stderr
    run_result()
    done = merge("--version", "2", "--despite-failed-run")
    assert done.returncode == 3 and "its run's record" in done.stderr, done.stderr


def test_a_seed_merge_refusal_says_how_to_bring_a_changed_version(setup, tmp_path):
    """The marker names a version number, not its content: a version solved or edited in place on the
    workstation after its merge keeps its number, and its merge through a newer seed is refused. The
    refusal says to clone it to a new version and merge that; a run job's refusal does not."""
    cfg, area, seed, job, main, merge, run_result = setup
    live = tmp_path / "live" / "db"
    _earlier(cfg, main, dbc.merge_marker(str(live), "m", "sc", 4))
    Path(f"{live}.data").write_bytes(b"\x07" * 5000)  # the live database changed since
    later, _ = dbc.backup(live, tmp_path / "bk2", "l2")
    seed2, _, _ = dbc.seed(later, "test", "s2", cfg)
    via_seed2 = merge("--version", "4", job_dir=_seed_job(cfg, seed2, area, "merge_seed_9", model_src(tmp_path / "m2")))
    assert via_seed2.returncode == 3 and "was merged into" in via_seed2.stderr, via_seed2.stderr
    assert "clone it to a new version there and merge that" in via_seed2.stderr, via_seed2.stderr
    run_result()
    _earlier(cfg, main, dbc.merge_marker(str(job.resolve()), "m", "sc", 2), n=2)
    again = merge()
    assert again.returncode == 3 and "was merged into" in again.stderr and "clone it" not in again.stderr


def test_a_run_that_never_started_is_not_merged(setup):
    """job_run.do wrote expected_merges.txt naming the scenario, then run-mark --before failed (a JVM or
    platform error): the command never ran, job-close did. Every version in the copy may be the seed's:
    refused like a run that did not complete, --version default included."""
    _, area, _, job, _, merge, _ = setup
    (job / dbc.EXPECTED_MERGES).write_text("sc\n")
    for extra in ((), ("--version", "default"), ("--version", "1"), ("--version", "default", "--despite-failed-run"),
                  ("--version", "default", "--apply")):
        out = merge(*extra)
        assert out.returncode == 3 and "never started" in out.stderr, (extra, out.stdout + out.stderr)
        assert "--version default" not in out.stderr.split("REFUSED:", 1)[1], out.stderr
    assert not (area / "backups").exists() and not list(job.glob("merge_src_*"))
    other = merge(scenario="other")  # any scenario of that copy
    assert other.returncode == 3 and "never started" in other.stderr, other.stderr
    by_hand = merge("--version", "1", "--despite-failed-run")
    assert by_hand.returncode == 0 and "dry run" in by_hand.stdout, by_hand.stdout + by_hand.stderr


def test_what_does_not_count_as_a_run_that_never_started(setup):
    """A read job (an empty expected_merges.txt, or only whitespace) and a seed merge (expected_merges.txt
    beside seed_merge.txt) hold no run records by design: a named version merges as before."""
    _, _, seed, job, _, merge, _ = setup
    (job / dbc.EXPECTED_MERGES).write_text("\n")
    bare = merge()
    assert bare.returncode == 3 and "no run record" in bare.stderr, bare.stderr
    assert merge("--version", "default").returncode == 0
    (job / dbc.EXPECTED_MERGES).write_text("sc\n")
    (job / dbc.SEED_MERGE).write_text(str(seed) + "\n")
    told = merge("--version", "default")
    assert told.returncode == 0 and "dry run" in told.stdout, told.stdout + told.stderr


def test_a_refused_run_merges_only_by_hand(setup):
    """run-mark --after refused the run (here: the base solved in place beside a forgotten clone). The
    merge refuses it, says why and how to merge by hand; --version N --despite-failed-run merges."""
    _, _, _, job, _, merge, run_result = setup
    (job / dbc.RUN_BEFORE).write_text(json.dumps(
        {"model": "m", "scenario": "sc", "versions": [1], "default": 1, "solved": False}))
    run_result(versions=[1, 2], default=1, new=False, in_place=False, accepted=False,
               reason="a version the run made is not default")
    for extra in ((), ("--version", "2"), ("--version", "default", "--despite-failed-run")):
        out = merge(*extra)
        assert out.returncode == 3 and "a version the run made is not default" in out.stderr, (extra, out.stderr)
        assert "--version N --despite-failed-run" in out.stderr, out.stderr
    by_hand = merge("--version", "2", "--despite-failed-run")
    assert by_hand.returncode == 0 and "sc v2 from" in by_hand.stdout, by_hand.stdout + by_hand.stderr
    run_result(versions=[1], default=1, before=[1], new=False)  # a record of 0.3.0: `new` false, no verdict
    legacy = merge("--version", "1", "--despite-failed-run")
    assert legacy.returncode == 0, legacy.stderr
    run_result(model="OTHER_MODEL", accepted=False, reason="x")  # another model: refused for that first
    assert "OTHER_MODEL/sc, not m/sc" in merge("--version", "2", "--despite-failed-run").stderr


def test_a_record_of_an_earlier_release_is_judged_again(setup):
    """0.4.0 accepted the base solved in place beside a new, non-default version. A pending record of it
    is judged by this release's rule too: refused, and mergeable only by hand. A record this release
    accepts as well (a new version; a solve in place with no other version) merges."""
    _, _, _, job, _, merge, run_result = setup
    (job / dbc.RUN_BEFORE).write_text(json.dumps(
        {"model": "m", "scenario": "sc", "versions": [1], "default": 1, "solved": False}))
    run_result(versions=[1, 2], default=1, before=[1], before_default=1, before_solved=False,
               new=False, in_place=True, accepted=True, reason=None)
    out = merge()
    assert out.returncode == 3 and "a version the run made is not default" in out.stderr, out.stdout + out.stderr
    assert "earlier release" in out.stderr, out.stderr
    assert merge("--version", "2", "--despite-failed-run").returncode == 0
    run_result(versions=[1], default=1, before=[1], before_default=1, before_solved=False,
               new=False, in_place=True, accepted=True, reason=None)
    in_place = merge()
    assert in_place.returncode == 0 and "sc v1 from" in in_place.stdout, in_place.stdout + in_place.stderr
    run_result(before_default=1, before_solved=True, accepted=True, reason=None)  # v2 new and default
    assert merge().returncode == 0
    done = merge("--version", "2", "--despite-failed-run")
    assert done.returncode == 3 and "which accepts the run" in done.stderr, done.stderr


class _FakePlatform:
    def __init__(self, *args, **kwargs):
        pass

    def close_db(self):
        pass


def _merged(model, scenario, version, marker):
    return {"model": model, "scenario": scenario, "source_version": version, "merged_version": 7,
            "was_default_in_job": True, "set_default": True, "marker": marker, "compare": {"ok": True}}


def test_exit_4_says_what_to_check(setup, monkeypatch, capsys):
    """A merge failing after its backup (a JVM error in the clone), or leaving its target not shut
    down cleanly, exits 4 with what to check and never advises restoring the main from its backup.
    In process, with the platforms faked: no JVM. A refused run merged by hand is recorded so."""
    import ixmp

    from ixmp_copies import cli, platforms
    from ixmp_copies import config as config_mod

    cfg, area, _, job, main, _, run_result = setup
    main_db = main / "db" / dbc.STEM
    monkeypatch.chdir(cfg.project_root)
    monkeypatch.delenv(config_mod.ENV, raising=False)
    monkeypatch.setattr(platforms, "platform_db", lambda name: main_db)
    monkeypatch.setattr(ixmp, "Platform", _FakePlatform)

    def oom(*args):
        raise RuntimeError("java.lang.OutOfMemoryError: Java heap space")

    monkeypatch.setattr(platforms, "merge_scenario", oom)
    run_result()
    assert cli.main(["merge", "--job-dir", str(job), "--scenario", "sc", "--apply"]) == 4
    err = capsys.readouterr().err
    assert "FAILED: MERGE: RuntimeError: java.lang.OutOfMemoryError" in err, err
    assert "restore from" not in err and "Never swap database files by hand" in err, err
    assert f"'{dbc.merge_marker(str(job), 'm', 'sc', 2)}'" in err and "FORCE_RUNS" in err, err
    assert str(area / "backups") in err and not list(cfg.records_dir.glob("merge_*.json"))

    def lands_and_holds(src_mp, dst_mp, model, scenario, version, marker, key, legacy):
        Path(f"{main_db}.lck").write_text("held")  # the main left not shut down cleanly
        return _merged(model, scenario, version, marker)

    monkeypatch.setattr(platforms, "merge_scenario", lands_and_holds)
    time.sleep(1.1)  # backups and merge sources are named by the second
    assert cli.main(["merge", "--job-dir", str(job), "--scenario", "sc", "--apply"]) == 4
    err = capsys.readouterr().err
    record = next(cfg.records_dir.glob("merge_*.json"))
    assert f"merged and recorded ({record})" in err and "Never swap database files by hand" in err, err
    assert "restore from" not in err and "never delete it" in err, err
    Path(f"{main_db}.lck").unlink()
    record.unlink()

    (job / dbc.RUN_BEFORE).write_text(json.dumps(
        {"model": "m", "scenario": "sc", "versions": [1], "default": 1, "solved": False}))
    run_result(versions=[1, 2], default=1, new=False, accepted=False, reason="a version the run made is not default")
    monkeypatch.setattr(platforms, "merge_scenario", lambda *a: _merged(*a[2:6]))
    time.sleep(1.1)
    assert cli.main(["merge", "--job-dir", str(job), "--scenario", "sc", "--version", "2", "--despite-failed-run",
                     "--apply"]) == 0
    by_hand = json.loads(next(cfg.records_dir.glob("merge_*.json")).read_text())
    assert by_hand["despite_failed_run"] is True and by_hand["run"]["accepted"] is False, by_hand
    assert by_hand["source_version"] == 2


def test_a_failed_transfer_names_its_backup(setup, monkeypatch, capsys, tmp_path):
    """A transfer failing after its backup exits 4 naming the backup to restore to a new folder; into
    a new database (no backup) it says only that the target may hold a partial version."""
    import ixmp
    import message_ix

    from ixmp_copies import cli, platforms
    from ixmp_copies import config as config_mod

    cfg, _, _, _, _, _, _ = setup
    target = fake_db(tmp_path / "local")
    monkeypatch.chdir(cfg.project_root)
    monkeypatch.delenv(config_mod.ENV, raising=False)
    monkeypatch.setattr(platforms, "is_hsqldb", lambda name: True)
    monkeypatch.setattr(platforms, "platform_db", lambda name: target)
    monkeypatch.setattr(ixmp, "Platform", _FakePlatform)
    monkeypatch.setattr(message_ix, "Scenario", lambda *a, **k: object())

    def broken(mp):
        raise RuntimeError("java.lang.OutOfMemoryError")

    monkeypatch.setattr(platforms, "read_registry", broken)
    argv = ["transfer", "--from", "ixmp-dev", "--to", "loc", "--scenario", "s", "--version", "1", "--apply"]
    assert cli.main(argv) == 4
    err = capsys.readouterr().err
    backup = next((tmp_path / "hd" / cfg.backups / "loc").iterdir())  # outside every area: the backups folder
    assert f"The backup taken before it: {backup}" in err and "restore it to a new folder" in err, err
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    monkeypatch.setattr(platforms, "platform_db", lambda name: fresh / "db")
    assert cli.main(argv) == 4
    err = capsys.readouterr().err
    assert err.rstrip().endswith("may hold a partial version") and "backup" not in err, err
