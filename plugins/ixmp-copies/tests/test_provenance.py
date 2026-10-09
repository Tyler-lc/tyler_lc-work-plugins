"""Which GAMS source a job copied, and the consistency checks doctor reports."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import fake_db
from test_copies import SHARED, model_src

from ixmp_copies import copies as dbc
from ixmp_copies import provenance as pv


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def test_fingerprint_ignores_run_output_only(tmp_path):
    model = model_src(tmp_path)
    base = pv.fingerprint(model)
    for junk in ("cplex.opt", "cplex.op2", "data/MsgData_m_s.gdx", "output/MsgOutput_m_s.gdx",
                 "output/README.md", "225b/x", "MESSAGE_run.lst",
                 "MESSAGE/.ipynb_checkpoints/model_core-checkpoint.gms"):
        (model / junk).parent.mkdir(parents=True, exist_ok=True)
        (model / junk).write_text("run output")
    assert pv.fingerprint(model) == base
    (model / "MESSAGE" / "model_core.gms").write_text("* core, changed")
    assert pv.fingerprint(model) != base


def test_model_source_in_and_outside_git(tmp_path):
    model = model_src(tmp_path / "pkg")
    outside = pv.model_source(model, "3.11.1")
    assert outside["git_commit"] is None and outside["git_changed"] == []
    git(tmp_path / "pkg", "init", "-q")
    git(tmp_path / "pkg", "add", ".")
    git(tmp_path / "pkg", "commit", "-qm", "x")
    head = git(tmp_path / "pkg", "rev-parse", "HEAD")
    inside = pv.model_source(model, f"3.11.2.dev86+g{head[:9]}")
    assert inside["git_commit"] == head and inside["git_changed"] == []
    (model / "MESSAGE_run.gms").write_text("* edited")
    (model / "data" / "README.md").write_text("x")
    git(tmp_path / "pkg", "add", "model_src/data/README.md")
    git(tmp_path / "pkg", "commit", "-qm", "readme")
    (model / "data" / "README.md").unlink()
    assert pv.model_source(model)["git_changed"] == [" M model_src/MESSAGE_run.gms"]


@pytest.mark.parametrize("version,want", [
    ("3.11.2.dev86+g79e880969", "79e880969"),
    ("3.11.2.dev147+gb0ded639d.d20260529", "b0ded639d"),
    ("3.11.1", None), (None, None)])
def test_label_commit(version, want):
    assert pv.label_commit(version) == want


def _src(version="3.11.2.dev86+g79e880969", commit="b0ded639d0000", changed=()):
    return {"message_ix_version": version, "git_commit": commit, "git_changed": list(changed)}


def test_assess(tmp_path):
    a, b = str(tmp_path / "a"), str(tmp_path / "b")
    # an editable install whose checkout moved on: same folder, label from an older commit
    got = pv.assess(a, a, _src())
    assert got[0][0] == "ok" and any(s == "warn" and "misreports" in m for s, m in got)
    # an editable install installed at its checkout's commit: same folder, label matches
    assert [s for s, _ in pv.assess(a, a, _src("3.11.2.dev147+gb0ded639d.d20260529"))] == ["ok"]
    # a released package with GAMS from a checkout elsewhere; the label check does not apply
    mixed = pv.assess(a, b, _src("3.11.1"))
    assert [s for s, _ in mixed] == ["warn"] and "two releases" in mixed[0][1]
    # a different folder whose label disagrees: only the mismatch is reported
    assert [s for s, _ in pv.assess(a, b, _src())] == ["warn"]
    assert any("uncommitted" in m for _, m in pv.assess(a, a, _src(commit=None, changed=[" M x.gms"])))


def test_job_copy_records_and_checks_the_model_source(project, tmp_path, monkeypatch):
    cfg, hd = project
    backup_dir, _ = dbc.backup(fake_db(tmp_path / "live"), tmp_path / "bk", "l")
    seed, _, _ = dbc.seed(backup_dir, "test", "s", cfg)
    model = model_src(tmp_path)
    jobs = hd / "ixmp_test" / "jobs"
    out = dbc.job_copy(seed, jobs / "j1", "p", SHARED, model, "test", cfg, message_ix_version="3.11.1")
    record = json.loads((jobs / "j1" / "model_source.json").read_text())
    assert record == out["model_source"] and record["message_ix_version"] == "3.11.1"
    assert record["fingerprint"] == pv.fingerprint(jobs / "j1" / "model") == pv.fingerprint(model)

    real = shutil.copytree

    def copy_while_source_changes(src, dst, *args, **kw):
        # A pull between recording the source and copying it; copytree also calls itself for
        # subfolders, so only the top-level call changes the source.
        if Path(src) == model:
            (model / "MESSAGE" / "model_core.gms").write_text("* pulled mid-copy")
        return real(src, dst, *args, **kw)

    monkeypatch.setattr(dbc.shutil, "copytree", copy_while_source_changes)
    with pytest.raises(dbc.CopyMismatch, match="did the source change"):
        dbc.job_copy(seed, jobs / "j2", "p", SHARED, model, "test", cfg)
    assert not (jobs / "j2" / "model_source.json").exists()


def test_commit_without_git(tmp_path, monkeypatch):
    """Cluster compute nodes have no git: the commit comes from the .git files, loose or packed,
    in a checkout or a worktree, and uncommitted changes are unknown (None), not none."""
    repo = tmp_path / "pkg"
    model = model_src(repo)
    git(repo, "init", "-q")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "x")
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "other")
    wt_model = tmp_path / "wt" / "model_src"
    (wt_model / "MESSAGE_run.gms").write_text("* edited")
    git(tmp_path / "wt", "commit", "-qam", "wt")
    wt_head = git(tmp_path / "wt", "rev-parse", "HEAD")
    monkeypatch.setattr(pv.shutil, "which", lambda name: None)
    loose = pv.model_source(model)
    assert loose["git_commit"] == head and loose["git_changed"] is None
    assert pv.model_source(wt_model)["git_commit"] == wt_head
    git(repo, "pack-refs", "--all")
    assert not (repo / ".git" / "refs" / "heads" / "other").exists()
    assert pv.head_from_files(model) == head and pv.head_from_files(wt_model) == wt_head
    git(repo, "checkout", "-q", "--detach")
    assert pv.head_from_files(model) == head
    assert pv.head_from_files(tmp_path / "elsewhere") is None
