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
    out = run_cli(["init", "--model", "M", "--venv", "~/repos/.venv_b", "--cluster-user", "jdoe"], proj)
    assert out.returncode == 0, out.stderr
    cfg = config.parse(proj / config.FILENAME)
    assert cfg.platform == "my-project-local" and cfg.model == "M" and cfg.venv == "~/repos/.venv_b"
    assert cfg.areas["live"] == "ixmp_copies/my_project/live" and cfg.cluster_user == "jdoe"
    assert [str(r) for r in dbc.hdrive_candidates(cfg)][0] == "/hdrive/all_users/jdoe"
    assert cfg.remote_hdrive.format(cluster_user="jdoe") == "/hdrive/all_users/jdoe"
    again = run_cli(["init", "--venv", "v", "--cluster-user", "jdoe"], proj)
    assert again.returncode == 3 and "exists" in again.stderr
    novenv = run_cli(["init"], tmp_path)
    assert novenv.returncode == 3 and "--venv" in novenv.stderr  # a command line refused, like any refusal
    unknown = run_cli(["nonsense"], tmp_path)
    assert unknown.returncode == 3 and "REFUSED" in unknown.stderr


def test_init_asks_ssh_for_the_cluster_user(tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "ssh").write_text("#!/bin/sh\necho jdoe\n")
    (fake / "ssh").chmod(0o755)
    proj = tmp_path / "p1"
    proj.mkdir()
    out = run_cli(["init", "--venv", "v"], proj, PATH=f"{fake}:/usr/bin:/bin")
    assert out.returncode == 0 and config.parse(proj / config.FILENAME).cluster_user == "jdoe", out.stderr
    (fake / "ssh").write_text("#!/bin/sh\nexit 255\n")
    proj2 = tmp_path / "p2"
    proj2.mkdir()
    down = run_cli(["init", "--venv", "v"], proj2, PATH=f"{fake}:/usr/bin:/bin")
    assert down.returncode == 3 and "--cluster-user" in down.stderr and not (proj2 / config.FILENAME).exists()


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
    into_main = run_cli(["merge", "--job-dir", str(job), "--scenario", "s", "--version", "1"], cfg.project_root,
                        IXMP_DATA=str(main / "ixmp"))
    want = f"pre-merge backup to {(hd / 'ixmp_live' / 'backups').resolve() / 'results_1'}/; record merge_results_1_*"
    assert into_main.returncode == 0 and want in into_main.stdout, into_main.stdout + into_main.stderr
    backup = run_cli(["backup"], cfg.project_root, IXMP_DATA=str(main / "ixmp"))
    assert backup.returncode == 0 and "record backup_results_1_*" in backup.stdout, backup.stderr
    assert not (hd / "ixmp_live" / "backups").exists()
    nomodel = cfg.path.read_text().replace('model = "m"\n', "")
    cfg.path.write_text(nomodel)
    refused = run_cli(["merge", "--job-dir", str(job), "--scenario", "s", "--version", "1"], cfg.project_root,
                      IXMP_DATA=str(main / "ixmp"))
    assert refused.returncode == 3 and "no model" in refused.stderr
    open_job = hd / "ixmp_test" / "jobs" / "unclosed"
    dbc.job_copy(seed_dir, open_job, cfg.platform, SHARED, tmp_path / "model_src", "test", cfg)
    unclosed = run_cli(["merge", "--job-dir", str(open_job), "--scenario", "s", "--model", "m"],
                       cfg.project_root, IXMP_DATA=str(main / "ixmp"))
    assert unclosed.returncode == 3 and "did not close" in unclosed.stderr


def test_job_env_sources_in_bash(project):
    cfg, _ = project
    text = stage.job_env(cfg.__class__(**{**cfg.__dict__, "venv": "~/repos/.venv_b",
                                          "modules": ("Py/3 x", "Java")}))
    out = subprocess.run(["bash", "-c", f"{text}\necho \"$IXC_VENV|$IXC_MODULES|$IXC_PLATFORM\""],
                         capture_output=True, text=True, env={"HOME": "/home/u", "PATH": "/usr/bin:/bin"})
    assert out.stdout.strip() == "/home/u/repos/.venv_b|Py/3 x Java|proj-local", out.stderr


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
    assert "Exit: run 0 mark 0 close 0 seed 0" in out.stdout, out.stdout[-2000:] + out.stderr[-2000:]
    assert out.returncode == 0
    job_dir = hd / "ixmp_test" / "jobs" / "probe_42"
    assert (job_dir / "result.json").is_file()
    assert (job_dir / "code" / "records" / "run_probe.json").is_file()
    # The code copy as made is recorded; the job's own imports (bytecode) and its json record are
    # no output, so nothing would keep it from cleanup.
    assert (job_dir / dbc.CODE_FILES).is_file() and list((job_dir / "code").rglob("*.pyc"))
    assert dbc.job_outputs(cfg, job_dir) == []
    assert not (job_dir / dbc.EXPECTED_MERGES).exists()  # submitted by hand: not a read job
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
    assert bad.returncode != 0 and "Exit: run 7 mark 0 close 0 seed 0" in bad.stdout, bad.stdout[-1500:]
    reader = {**env, "NAME": "reads", "SLURM_JOB_ID": "44", "MERGE_SCENARIO": "-", "CMD": "echo x > out.txt"}
    read = subprocess.run(["bash", str(code / ".ixmp_copies/slurm/job_run.do")], capture_output=True,
                          text=True, env=reader, cwd=tmp_path)
    assert "Exit: run 0 mark 0 close 0 seed 0" in read.stdout, read.stdout[-1500:] + read.stderr[-1500:]
    read_dir = hd / "ixmp_test" / "jobs" / "reads_44"
    assert (read_dir / dbc.EXPECTED_MERGES).read_text() == "" and not (read_dir / dbc.RUN_BEFORE).exists()
    assert dbc.job_outputs(cfg, read_dir) == ["code/out.txt"]  # written by the command into its code copy



def test_init_sanitises_the_folder_name(tmp_path):
    proj = tmp_path / "4th-Gen.Paper"
    proj.mkdir()
    out = run_cli(["init", "--venv", "v", "--cluster-user", "jdoe"], proj)
    assert out.returncode == 0, out.stderr
    cfg = config.parse(proj / config.FILENAME)
    assert cfg.platform == "4th-gen-paper-local" and cfg.areas["test"] == "ixmp_copies/4th_gen_paper/test"
    assert "platform-add" in out.stdout


@needs_ixmp
def test_transfer_into_a_platform_not_opened_yet(project, tmp_path):
    """platform-add, then transfer (SETUP.md steps 6 and 7): the database files do not exist until the
    first open, so there is nothing to back up or to find open."""
    cfg, _ = project
    home = tmp_path / "ixmp_home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"platform": {"default": "ixmp-dev", "ixmp-dev": {
        "class": "jdbc", "driver": "oracle", "url": "x", "user": "u", "password": "p"}}}))
    env = {"IXMP_DATA": str(home)}
    added = run_cli(["platform-add", "--dir", str(tmp_path / "localdb"), "--apply"], cfg.project_root, **env)
    assert added.returncode == 0, added.stderr
    tr = run_cli(["transfer", "--from", "ixmp-dev", "--to", cfg.platform, "--scenario", "s"], cfg.project_root, **env)
    assert tr.returncode == 0 and "a new database, nothing to back up" in tr.stdout, tr.stdout + tr.stderr
    fake_db(tmp_path / "other")  # files beside the stem: an existing database, checked and backed up
    conf = json.loads((home / "config.json").read_text())
    conf["platform"]["proj-local"]["url"] = f"jdbc:hsqldb:file:{tmp_path / 'other' / 'db'}"
    (home / "config.json").write_text(json.dumps(conf))
    existing = run_cli(["transfer", "--from", "ixmp-dev", "--to", cfg.platform, "--scenario", "s"], cfg.project_root, **env)
    assert existing.returncode == 0 and "backed up first" in existing.stdout, existing.stdout + existing.stderr
    Path(f"{tmp_path / 'other' / 'db'}.lck").write_text("x")
    held = run_cli(["transfer", "--from", "ixmp-dev", "--to", cfg.platform, "--scenario", "s"], cfg.project_root, **env)
    assert held.returncode == 3 and "not closed" in held.stderr


@needs_ixmp
def test_transfer_target_folder(project, tmp_path):
    """A database yet to be created is one in an empty folder or a missing folder below an existing
    one; a url whose parent folder is missing too (a typo, an unmounted disk) is refused, as is a
    folder holding other files."""
    cfg, _ = project
    home = tmp_path / "ixmp_home"
    home.mkdir()

    def transfer_to(db: Path):
        (home / "config.json").write_text(json.dumps({"platform": {"default": "ixmp-dev", "ixmp-dev": {
            "class": "jdbc", "driver": "oracle", "url": "x", "user": "u", "password": "p"},
            cfg.platform: {"class": "jdbc", "driver": "hsqldb",
                           "url": f"jdbc:hsqldb:file:{db};hsqldb.default_table_type=cached"}}}))
        return run_cli(["transfer", "--from", "ixmp-dev", "--to", cfg.platform, "--scenario", "s"], cfg.project_root,
                       IXMP_DATA=str(home))

    unmounted = transfer_to(tmp_path / "not" / "mounted" / "db")
    assert unmounted.returncode == 3 and "nor its parent" in unmounted.stderr, unmounted.stdout + unmounted.stderr
    assert "a new database" not in unmounted.stdout
    below = transfer_to(tmp_path / "newdb" / "db")  # missing, below an existing folder
    assert below.returncode == 0 and "a new database, nothing to back up" in below.stdout, below.stdout + below.stderr
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy" / "notes.txt").write_text("x")
    busy = transfer_to(tmp_path / "busy" / "db")
    assert busy.returncode == 3 and "not an empty folder" in busy.stderr, busy.stdout + busy.stderr


@needs_ixmp
def test_platform_add(project, tmp_path):
    cfg, hd = project
    home = tmp_path / "ixmp_home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"platform": {"default": "local", "local": {
        "class": "jdbc", "driver": "hsqldb", "path": str(tmp_path / "x" / "local")}}}))
    env = {"IXMP_DATA": str(home)}
    dest = tmp_path / "dbs" / "proj"
    dry = run_cli(["platform-add", "--dir", str(dest)], cfg.project_root, **env)
    assert dry.returncode == 0 and "new database" in dry.stdout, dry.stderr
    assert "proj-local" not in json.loads((home / "config.json").read_text())["platform"]
    done = run_cli(["platform-add", "--dir", str(dest), "--apply"], cfg.project_root, **env)
    assert done.returncode == 0, done.stderr
    entry = json.loads((home / "config.json").read_text())["platform"]["proj-local"]
    assert entry["url"] == f"jdbc:hsqldb:file:{dest / 'db'};hsqldb.default_table_type=cached", entry
    again = run_cli(["platform-add", "--dir", str(dest), "--apply"], cfg.project_root, **env)
    assert again.returncode == 3 and "already" in again.stderr
    on_hd = run_cli(["platform-add", "--name", "p2", "--dir", str(hd / "dbs"), "--apply"], cfg.project_root, **env)
    assert on_hd.returncode == 3 and "H drive" in on_hd.stderr
    mem = fake_db(tmp_path / "memdb")
    Path(f"{mem}.script").write_text("CREATE MEMORY TABLE A\n")
    memory = run_cli(["platform-add", "--name", "p3", "--dir", str(mem.parent), "--apply"], cfg.project_root, **env)
    assert memory.returncode == 3 and "MEMORY" in memory.stderr
    existing = run_cli(["platform-add", "--name", "p4", "--dir", str(fake_db(tmp_path / "okdb").parent)],
                       cfg.project_root, **env)
    assert existing.returncode == 0 and "existing database" in existing.stdout, existing.stderr
