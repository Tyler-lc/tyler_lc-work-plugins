"""File-level copies on fake HyperSQL file sets: no JVM."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import fake_db

from ixmp_copies import copies as dbc
from ixmp_copies.copies import CopyMismatch, Refused


def test_backup_and_verify(tmp_path):
    db = fake_db(tmp_path / "ok")
    dest, manifest = dbc.backup(db, tmp_path / "bk", "p")
    assert dest.is_dir() and not list(dest.parent.glob("*.partial"))
    assert sorted(manifest["files"]) == ["db.data", "db.lobs", "db.properties", "db.script"]
    assert all((dest / n).read_bytes() == (db.parent / n).read_bytes() for n in manifest["files"])
    assert manifest["tables"] == {"cached": 2, "memory": 0}
    assert dbc.verify(dest) == []


@pytest.mark.parametrize("spoil", [
    lambda d: Path(f"{d}.properties").write_text("modified=yes\n"),
    lambda d: Path(f"{d}.lck").write_bytes(b"x"),
    lambda d: Path(f"{d}.log").write_text("INSERT ..."),
    lambda d: (Path(f"{d}.tmp") / "x").write_text("x"),
    lambda d: Path(f"{d}.foo").write_text("x"),
    lambda d: Path(f"{d}.properties").unlink(),
], ids=["modified", "lck", "log", "tmp", "unknown_file", "no_properties"])
def test_backup_refusals_write_nothing(tmp_path, spoil):
    d = fake_db(tmp_path / "db")
    spoil(d)
    with pytest.raises(Refused):
        dbc.backup(d, tmp_path / "bk", "x")
    assert not (tmp_path / "bk").exists()


def test_empty_log_is_closed(tmp_path):
    d = fake_db(tmp_path / "db")
    Path(f"{d}.log").write_text("")
    dbc.require_closed(d)


def _hold(path: str, mode: str) -> subprocess.Popen:
    holder = subprocess.Popen([sys.executable, "-c", f"f = open({path!r}, {mode!r}); import time; time.sleep(30)"])
    time.sleep(1.0)
    return holder


def test_refused_while_held_open_for_writing(tmp_path):
    held = fake_db(tmp_path / "held")
    alias = tmp_path / "alias"
    alias.symlink_to(held.parent)  # on UniCC ~/hdrive is such a symlink to /hdrive/...
    holder = _hold(str(held) + ".data", "r+b")
    try:
        with pytest.raises(Refused, match=str(holder.pid)):
            dbc.backup(held, tmp_path / "bk", "held")
        with pytest.raises(Refused, match=str(holder.pid)):
            dbc.require_closed(alias / "db")
    finally:
        holder.kill()
        holder.wait()
    dbc.require_closed(held)


def test_readers_do_not_block_a_copy(tmp_path):
    """Two jobs on one node copying the same read-only seed are each other's readers."""
    held = fake_db(tmp_path / "read")
    holder = _hold(str(held) + ".data", "rb")
    try:
        dbc.require_closed(held)
        dbc.backup(held, tmp_path / "bk", "read")
    finally:
        holder.kill()
        holder.wait()


def test_refused_partial_exists(tmp_path):
    d = fake_db(tmp_path / "db")
    (tmp_path / "bk" / "x" / f"{time.strftime('%Y%m%d_%H%M%S')}.partial").mkdir(parents=True)
    with pytest.raises(Refused, match="already exists"):
        dbc.backup(d, tmp_path / "bk", "x")


def test_mismatch_when_source_changes(tmp_path, monkeypatch):
    d = fake_db(tmp_path / "db")
    original = dbc._copy_file

    def copy_then_touch(src, dst):
        original(src, dst)
        if src.suffix == ".data":
            with src.open("ab") as f:
                f.write(b"late write")

    monkeypatch.setattr(dbc, "_copy_file", copy_then_touch)
    with pytest.raises(CopyMismatch):
        dbc.backup(d, tmp_path / "bk", "changing")
    left = list((tmp_path / "bk" / "changing").iterdir())
    assert len(left) == 1 and left[0].name.endswith(".partial")
    assert dbc.verify(left[0]) != []


def _flip(out: Path) -> None:
    b = bytearray((out / "db.data").read_bytes())
    b[1000] ^= 0xFF
    (out / "db.data").write_bytes(bytes(b))


@pytest.mark.parametrize("spoil,expect", [
    (_flip, "db.data"),
    (lambda o: (o / "db.lobs").write_bytes(b"\x01" * 10), "db.lobs"),
    (lambda o: (o / "db.script").unlink(), "missing db.script"),
    (lambda o: (o / "db.lck").write_text("x"), "unexpected db.lck"),
    (lambda o: (o / dbc.MANIFEST).unlink(), "does not exist"),
], ids=["flipped", "truncated", "missing", "extra", "no_manifest"])
def test_verify_finds(tmp_path, spoil, expect):
    out, _ = dbc.backup(fake_db(tmp_path / "db"), tmp_path / "bk", "v")
    spoil(out)
    assert any(expect in p for p in dbc.verify(out))


def test_cached_tables(tmp_path):
    d = fake_db(tmp_path / "db")
    assert dbc.require_cached_tables(d) == 2
    Path(f"{d}.script").write_text("CREATE MEMORY TABLE A\n")
    with pytest.raises(ValueError, match="MEMORY"):
        dbc.require_cached_tables(d)
    Path(f"{d}.script").write_text("SET DATABASE\n")
    with pytest.raises(ValueError, match="no table definition"):
        dbc.require_cached_tables(d)
    Path(f"{d}.script").unlink()
    with pytest.raises(FileNotFoundError):
        dbc.require_cached_tables(d)


def test_hdrive_unreachable(project, tmp_path):
    cfg, hd = project
    for p in hd.iterdir():
        p.unlink()
    with pytest.raises(Refused, match="no H drive"):
        dbc.hdrive_root(cfg)


def test_a_hung_mount_is_not_waited_for(tmp_path, monkeypatch):
    real = subprocess.Popen
    monkeypatch.setattr(dbc, "PROBE_SECONDS", 0.5)
    monkeypatch.setattr(dbc.subprocess, "Popen", lambda *a, **k: real(["sleep", "30"]))
    start = time.monotonic()
    assert dbc.reachable(tmp_path) is False
    assert time.monotonic() - start < 5


def model_src(root: Path) -> Path:
    model = root / "model_src"
    (model / "MESSAGE").mkdir(parents=True)
    (model / "MESSAGE" / "model_core.gms").write_text("* core")
    (model / "MESSAGE_run.gms").write_text("* run")
    (model / "225a").mkdir()
    (model / "225a" / "scratch").write_text("x")
    for junk in ("MESSAGE_run.lst", "MESSAGE_run.log"):
        (model / junk).write_text("x")
    (model / "data").mkdir()
    (model / "data" / "MsgData_x.gdx").write_text("x")
    return model


SHARED = {"platform": {"default": "local", "ixmp-dev": {"class": "jdbc", "driver": "oracle", "password": "secret"},
                       "local": {"class": "jdbc", "driver": "hsqldb", "path": "/elsewhere/local"}},
          "message_local_data": "/data"}


def test_chain(project, tmp_path):
    cfg, hd = project
    db = fake_db(tmp_path / "live")
    backup_dir, _ = dbc.backup(db, dbc.backup_root_for(db, cfg), "live")
    assert backup_dir.parent.parent == hd / "ixmp_backup"

    seed_dir, manifest, read_only = dbc.seed(backup_dir, "test", "s1", cfg)
    assert seed_dir == hd / "ixmp_test" / "seeds" / "s1"
    assert manifest["kind"] == "seed" and manifest["from_backup"] == str(backup_dir)
    assert dbc.verify(seed_dir) == []
    assert read_only and not os.access(seed_dir / "db.data", os.W_OK)
    with pytest.raises(Refused, match="already exists"):
        dbc.seed(backup_dir, "test", "s1", cfg)
    with pytest.raises(Refused, match="not a 'backup'"):
        dbc.seed(seed_dir, "test", "s2", cfg)
    with pytest.raises(Refused, match="unknown area"):
        dbc.seed(backup_dir, "nope", "s3", cfg)
    with pytest.raises(Refused, match="must match"):
        dbc.seed(backup_dir, "test", "S 4", cfg)

    mem = fake_db(tmp_path / "memory")
    Path(f"{mem}.script").write_text("CREATE MEMORY TABLE A\n")
    mem_backup, _ = dbc.backup(mem, tmp_path / "bk", "memory")
    with pytest.raises(Refused, match="MEMORY"):
        dbc.seed(mem_backup, "test", "s5", cfg)

    model = model_src(tmp_path)
    jobs = hd / "ixmp_test" / "jobs"
    job = jobs / "t3_1"
    out = dbc.job_copy(seed_dir, job, "proj-local", {**SHARED, "message_model_dir": str(model)}, model, "test", cfg)
    conf = json.loads((job / "ixmp" / "config.json").read_text())
    assert dbc.verify(job / "db") == []
    assert sorted(conf["platform"]) == ["default", "proj-local"] and conf["platform"]["default"] == "proj-local"
    assert out["url"].startswith(f"jdbc:hsqldb:file:{job / 'db' / 'db'};")
    assert "secret" not in (job / "ixmp" / "config.json").read_text()
    assert conf["message_local_data"] == "/data" and conf["message_model_dir"] == str(job / "model")
    assert (job / "ixmp" / "config.json").stat().st_mode & 0o777 == 0o600
    copied = sorted(str(p.relative_to(job / "model")) for p in (job / "model").rglob("*"))
    assert copied == ["MESSAGE", "MESSAGE/model_core.gms", "MESSAGE_run.gms", "data", "output"]
    assert dbc.verify(seed_dir) == []
    with pytest.raises(Refused, match="already exists"):
        dbc.job_copy(seed_dir, job, "p", SHARED, model, "test", cfg)
    with pytest.raises(Refused, match="not directly below"):
        dbc.job_copy(seed_dir, hd / "elsewhere" / "t3_2", "p", SHARED, model, "test", cfg)
    with pytest.raises(Refused, match="not a 'seed'"):
        dbc.job_copy(backup_dir, jobs / "t3_3", "p", SHARED, model, "test", cfg)
    with pytest.raises(Refused, match="model dir"):
        dbc.job_copy(seed_dir, jobs / "t3_4", "p", SHARED, tmp_path / "no_model", "test", cfg)
    assert not (jobs / "t3_4").exists()

    Path(f"{job / 'db' / 'db'}.properties").write_text("modified=yes\n")
    with pytest.raises(Refused):
        dbc.job_close(job)
    assert not (job / "result.json").exists()
    Path(f"{job / 'db' / 'db'}.properties").write_text("modified=no\n")
    assert dbc.job_close(job)["kind"] == "job-result"
    with pytest.raises(Refused, match="already exists"):
        dbc.job_close(job)

    assert dbc.backup_root_for(job / "db" / "db", cfg) == (hd / "ixmp_test" / "backups").resolve()
    restored = tmp_path / "restored"
    dbc.restore(backup_dir, restored, cfg)
    assert all((restored / n).read_bytes() == (backup_dir / n).read_bytes()
               for n in ("db.data", "db.lobs", "db.script", "db.properties"))
    with pytest.raises(Refused, match="already exists"):
        dbc.restore(backup_dir, restored, cfg)
    with pytest.raises(Refused, match="restore to a local disk"):
        dbc.restore(backup_dir, hd / "somewhere", cfg)
    assert not (hd / "somewhere").exists()
    with pytest.raises(Refused, match="not a 'backup'"):
        dbc.restore(seed_dir, tmp_path / "r2", cfg)

    fresh = jobs / "t3_5"
    dbc.job_copy(seed_dir, fresh, "proj-local", SHARED, model, "test", cfg)
    with pytest.raises(Refused, match="did not close"):
        dbc.seed_from_job(fresh, "live", "s_ref", cfg)
    dbc.job_close(fresh)
    js_dir, js_manifest, js_ro = dbc.seed_from_job(fresh, "live", "s_ref", cfg)
    assert js_dir == hd / "ixmp_live" / "seeds" / "s_ref" and js_ro and dbc.verify(js_dir) == []

    main_dir = hd / "ixmp_live" / "mains" / "results_1"
    dbc.job_copy(js_dir, main_dir, "proj-local", SHARED, model, "live", cfg, kind="main")
    assert json.loads((main_dir / "db" / dbc.MANIFEST).read_text())["kind"] == "main"
    with pytest.raises(Refused, match="not directly below"):
        dbc.job_copy(js_dir, hd / "ixmp_live" / "jobs" / "m2", "p", SHARED, model, "live", cfg, kind="main")
    with pytest.raises(Refused, match="unknown copy kind"):
        dbc.job_copy(js_dir, hd / "ixmp_live" / "x" / "m3", "p", SHARED, model, "live", cfg, kind="other")

    main_db = main_dir / "db" / dbc.STEM
    assert dbc.copy_label(main_db, "proj-local", cfg) == "results_1"
    alias = tmp_path / "hd_alias"
    alias.symlink_to(hd)
    assert dbc.copy_label(alias / "ixmp_live" / "mains" / "results_1" / "db" / dbc.STEM, "proj-local", cfg) == "results_1"
    assert dbc.copy_label(fresh / "db" / dbc.STEM, "proj-local", cfg) == "proj-local"
    assert dbc.copy_label(db, "proj-local", cfg) == "proj-local"
    assert dbc.copy_label(hd / "ixmp_live" / "mains" / dbc.STEM, "proj-local", cfg) == "proj-local"
    assert dbc.backup_root_for(main_db, cfg) == (hd / "ixmp_live" / "backups").resolve()

    with pytest.raises(Refused, match="did not close"):
        dbc.require_job_result(jobs / "t3_1_missing")
    dbc.require_job_result(job)
    with Path(f"{job / 'db' / 'db'}.data").open("ab") as f:
        f.write(b"opened again")
    with pytest.raises(Refused, match="changed after job-close"):
        dbc.require_job_result(job)
