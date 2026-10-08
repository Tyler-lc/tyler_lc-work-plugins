"""Config, CLI dry runs (no JVM), staging through a local stand-in for ssh, job.env, collect."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import fake_db, has_ixmp, run_cli
from test_copies import SHARED, model_src

from ixmp_copies import config, stage
from ixmp_copies import copies as dbc
from ixmp_copies.copies import Refused

needs_ixmp = pytest.mark.skipif(not has_ixmp(), reason="ixmp not importable")


def test_config_parse_and_find(project, tmp_path, monkeypatch):
    cfg, hd = project
    assert cfg.platform == "proj-local" and cfg.areas == {"test": "ixmp_test", "live": "ixmp_live"}
    assert cfg.records_dir == cfg.project_root / "records"
    sub = cfg.project_root / "a" / "b"
    sub.mkdir(parents=True)
    monkeypatch.delenv(config.ENV, raising=False)
    assert config.find(sub) == cfg.path
    with pytest.raises(config.ConfigError, match="ixmp-copies init"):
        config.find(tmp_path / "hd")
    monkeypatch.setenv(config.ENV, str(tmp_path / "missing.toml"))
    with pytest.raises(config.ConfigError, match="is not a file"):
        config.find(sub)


@pytest.mark.parametrize("text,match", [
    ("[project]\n", "lacks 'platform'"),
    ('[project]\nplatform="p"\n[storage]\nroots=["/x"]\nbackups="b"\nareas={}\n', "at least one area"),
    ('[project]\nplatform="p"\n[storage]\nroots=["/x"]\nbackups="b"\nareas={t="t"}\n', "lacks 'venv'"),
    ("[project\n", "Expected"),
], ids=["no_platform", "no_areas", "no_venv", "bad_toml"])
def test_config_errors(tmp_path, text, match):
    path = tmp_path / config.FILENAME
    path.write_text(text)
    with pytest.raises(config.ConfigError, match=match):
        config.parse(path)


def test_init_template_parses_and_refuses_overwrite(tmp_path):
    proj = tmp_path / "My-Project"
    proj.mkdir()
    out = run_cli(["init", "--model", "M", "--venv", "~/repos/.efc"], proj)
    assert out.returncode == 0, out.stderr
    cfg = config.parse(proj / config.FILENAME)
    assert cfg.platform == "my_project-local" and cfg.model == "M" and cfg.venv == "~/repos/.efc"
    assert cfg.areas["live"] == "ixmp_copies/my_project/live"
    again = run_cli(["init"], proj)
    assert again.returncode == 3 and "exists" in again.stderr


def test_where_and_refusal_exit_codes(project):
    cfg, hd = project
    out = run_cli(["where", "--area", "live", "jobs"], cfg.project_root)
    assert out.returncode == 0 and out.stdout.strip() == str(hd / "ixmp_live" / "jobs")
    bad = run_cli(["where", "--area", "nope"], cfg.project_root)
    assert bad.returncode == 3 and "unknown area" in bad.stderr
    noconf = run_cli(["where", "--area", "live"], hd)
    assert noconf.returncode == 3 and "ixmp-copies init" in noconf.stderr


def _job(cfg, hd, tmp_path, name="t1"):
    db = fake_db(tmp_path / f"live_{name}")
    backup_dir, _ = dbc.backup(db, tmp_path / "bk", name)
    seed_dir, _, _ = dbc.seed(backup_dir, "test", f"s_{name}", cfg)
    model = tmp_path / "model_src"
    if not model.exists():
        model_src(tmp_path)
    job = hd / "ixmp_test" / "jobs" / name
    dbc.job_copy(seed_dir, job, cfg.platform, {**SHARED, "message_model_dir": str(model)}, model, "test", cfg)
    return job, seed_dir


@needs_ixmp
def test_job_check(project, tmp_path):
    cfg, hd = project
    job, _ = _job(cfg, hd, tmp_path)
    ok = run_cli(["job-check", "--job-dir", str(job)], cfg.project_root, IXMP_DATA=str(job / "ixmp"))
    assert ok.returncode == 0, ok.stderr
    unset = run_cli(["job-check", "--job-dir", str(job)], cfg.project_root)
    assert unset.returncode == 3 and "IXMP_DATA" in unset.stderr
    path = job / "ixmp" / "config.json"
    conf = json.loads(path.read_text())
    conf["platform"]["stray"] = {"class": "jdbc", "driver": "hsqldb", "path": "/live/db"}
    path.write_text(json.dumps(conf))
    stray = run_cli(["job-check", "--job-dir", str(job)], cfg.project_root, IXMP_DATA=str(job / "ixmp"))
    assert stray.returncode == 3 and "platforms" in stray.stderr
    conf["platform"].pop("stray")
    conf["platform"][cfg.platform]["url"] = "jdbc:hsqldb:file:/elsewhere/db"
    path.write_text(json.dumps(conf))
    other = run_cli(["job-check", "--job-dir", str(job)], cfg.project_root, IXMP_DATA=str(job / "ixmp"))
    assert other.returncode == 3 and "database" in other.stderr


@needs_ixmp
def test_merge_and_backup_dry_runs(project, tmp_path):
    cfg, hd = project
    job, seed_dir = _job(cfg, hd, tmp_path, "src")
    dbc.job_close(job)
    main = hd / "ixmp_live" / "mains" / "results_1"
    dbc.job_copy(seed_dir, main, cfg.platform, SHARED, tmp_path / "model_src", "live", cfg, kind="main")
    into_main = run_cli(["merge", "--job-dir", str(job), "--scenario", "s"], cfg.project_root,
                        IXMP_DATA=str(main / "ixmp"))
    want = f"pre-merge backup to {(hd / 'ixmp_live' / 'backups').resolve() / 'results_1'}/; record merge_results_1_*"
    assert into_main.returncode == 0 and want in into_main.stdout, into_main.stdout + into_main.stderr
    backup = run_cli(["backup"], cfg.project_root, IXMP_DATA=str(main / "ixmp"))
    assert backup.returncode == 0 and "record backup_results_1_*" in backup.stdout, backup.stderr
    assert not (hd / "ixmp_live" / "backups").exists()
    nomodel = cfg.path.read_text().replace('model = "m"\n', "")
    cfg.path.write_text(nomodel)
    refused = run_cli(["merge", "--job-dir", str(job), "--scenario", "s"], cfg.project_root,
                      IXMP_DATA=str(main / "ixmp"))
    assert refused.returncode == 3 and "no model" in refused.stderr
    open_job = hd / "ixmp_test" / "jobs" / "unclosed"
    dbc.job_copy(seed_dir, open_job, cfg.platform, SHARED, tmp_path / "model_src", "test", cfg)
    unclosed = run_cli(["merge", "--job-dir", str(open_job), "--scenario", "s", "--model", "m"],
                       cfg.project_root, IXMP_DATA=str(main / "ixmp"))
    assert unclosed.returncode == 3 and "did not close" in unclosed.stderr


def test_job_env_sources_in_bash(project):
    cfg, _ = project
    text = stage.job_env(cfg.__class__(**{**cfg.__dict__, "venv": "~/repos/.efc",
                                          "modules": ("Py/3 x", "Java")}))
    out = subprocess.run(["bash", "-c", f"{text}\necho \"$IXC_VENV|$IXC_MODULES|$IXC_PLATFORM\""],
                         capture_output=True, text=True, env={"HOME": "/home/u", "PATH": "/usr/bin:/bin"})
    assert out.stdout.strip() == "/home/u/repos/.efc|Py/3 x Java|proj-local", out.stderr


def local_remote(command: str, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    """Stand-in for ssh: the 'cluster' is this machine."""
    return subprocess.run(["bash", "-c", command], input=stdin, capture_output=True)


def _git_project(cfg) -> None:
    root = cfg.project_root
    (root / "run.py").write_text("print('hi')\n")
    for args in (["init", "-q"], ["add", "."], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"]):
        subprocess.run(["git", "-C", str(root), *args], check=True)


def test_stage_snapshot(project, tmp_path):
    cfg, hd = project
    _git_project(cfg)
    (cfg.project_root / "untracked.csv").write_text("a,b\n")
    dest = Path(stage.stage(cfg, "test", ["untracked.csv"], local_remote))
    assert dest.parent == hd / "ixmp_test" / "code"
    for rel in ("run.py", "ixmp_copies.toml", "untracked.csv", "CODE_MANIFEST", ".ixmp_copies/job.env",
                ".ixmp_copies/src/ixmp_copies/cli.py", ".ixmp_copies/slurm/job_run.do"):
        assert (dest / rel).is_file(), rel
    assert (dest / ".ixmp_copies/slurm/job_run.do").stat().st_mode & 0o111
    assert "extra untracked.csv" in (dest / "CODE_MANIFEST").read_text()
    with pytest.raises(Refused, match="already exists"):
        stage.stage(cfg, "test", [], local_remote)
    with pytest.raises(Refused, match="unknown area"):
        stage.stage(cfg, "nope", [], local_remote)
    with pytest.raises(Refused, match="does not exist"):
        stage.stage(cfg, "test", ["nothing_here.csv"], local_remote)
    (cfg.project_root / "run.py").write_text("print('changed')\n")
    with pytest.raises(Refused, match="commit first"):
        stage.stage(cfg, "test", [], local_remote)


def test_stage_refuses_config_below_root(project):
    cfg, _ = project
    _git_project(cfg)
    sub = cfg.project_root / "sub"
    sub.mkdir()
    shutil.copy(cfg.path, sub / config.FILENAME)
    with pytest.raises(Refused, match="repository root"):
        stage.stage(config.parse(sub / config.FILENAME), "test", [], local_remote)


def test_stage_ssh_failure_is_refusal(project):
    cfg, _ = project
    _git_project(cfg)

    def down(command, stdin=None):
        return subprocess.CompletedProcess(["ssh"], 255, b"", b"Permission denied (publickey,password)")

    with pytest.raises(Refused, match="SSH connection"):
        stage.stage(cfg, "test", [], down)


def test_job_scripts_run_against_a_snapshot(project, tmp_path):
    """common.sh and job_run.do as a job would run them, with fake lmod/venv and sbatch's
    SLURM_JOB_ID, CMD writing a record inside the job's code copy; then collect."""
    cfg, hd = project
    _git_project(cfg)
    code = Path(stage.stage(cfg, "test", [], local_remote))
    job, seed_dir = _job(cfg, hd, tmp_path, "pre")  # a seed to run against
    dbc.job_close(job)
    env = {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin", "CODE": str(code), "AREA": "test",
           "SEED": str(seed_dir), "NAME": "probe", "SLURM_JOB_ID": "42",
           "CMD": "mkdir -p records && echo '{\"ok\": true}' > records/run_probe.json && "
                  "test -n \"$IXMP_DATA\" && test \"$IXC_JOB_DIR\" = \"$(dirname \"$IXMP_DATA\")\""}
    if not has_ixmp():
        pytest.skip("job-copy reads the ixmp config")
    env["IXMP_DATA"] = "/nonexistent"  # common.sh must unset it, or job-copy refuses
    out = subprocess.run(["bash", str(code / ".ixmp_copies/slurm/job_run.do")], capture_output=True,
                         text=True, env=env, cwd=tmp_path)
    assert "Exit: run 0 close 0 seed 0" in out.stdout, out.stdout[-2000:] + out.stderr[-2000:]
    assert out.returncode == 0
    job_dir = hd / "ixmp_test" / "jobs" / "probe_42"
    assert (job_dir / "result.json").is_file()
    assert (job_dir / "code" / "records" / "run_probe.json").is_file()
    got = stage.collect(cfg, hd / "ixmp_test")
    assert "run_probe.json" in got["copied"] and not got["conflicts"]
    again = stage.collect(cfg, hd / "ixmp_test")
    assert "run_probe.json" in again["present"] and not again["copied"]
    (cfg.records_dir / "run_probe.json").write_text("{}")
    clash = stage.collect(cfg, hd / "ixmp_test")
    assert clash["conflicts"] and (cfg.records_dir / "run_probe.json").read_text() == "{}"
    failing = {**env, "NAME": "fails", "SLURM_JOB_ID": "43", "CMD": "exit 7"}
    bad = subprocess.run(["bash", str(code / ".ixmp_copies/slurm/job_run.do")], capture_output=True,
                         text=True, env=failing, cwd=tmp_path)
    assert bad.returncode != 0 and "Exit: run 7 close 0 seed 0" in bad.stdout, bad.stdout[-1500:]

