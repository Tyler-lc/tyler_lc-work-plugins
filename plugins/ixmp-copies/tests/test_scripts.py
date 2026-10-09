"""The submit scripts against a fake sbatch: what they submit, with which partition, variables and
dependencies, from relative paths; and the records they write."""

from __future__ import annotations

import subprocess
from pathlib import Path

from conftest import fake_db
from test_cli_stage import _git_project, local_remote
from test_copies import SHARED, model_src

from ixmp_copies import copies as dbc
from ixmp_copies import stage

FAKE_SBATCH = """#!/bin/bash
n=$(( $(cat "$FAKE/next") + 1 )); echo $n > "$FAKE/next"
vars="NAME=${NAME:-}|SCENARIO=${SCENARIO:-}|MERGE_SCENARIO=${MERGE_SCENARIO:-}|MODEL=${MODEL:-}"
vars="$vars|SEED=${SEED:-}|BACKUP=${BACKUP:-}|CMD=${CMD:-}"
echo "$n|$PWD|$vars|$*" >> "$FAKE/sbatch.log"
echo $n
"""


def _setup(project, tmp_path):
    cfg, hd = project
    cfg.path.write_text(cfg.path.read_text().replace('gams_module = ""', 'gams_module = ""\npartition = "short"'))
    _git_project(cfg)
    from ixmp_copies import config
    cfg = config.parse(cfg.path)
    code = Path(stage.stage(cfg, "test", [], local_remote))
    backup_dir, _ = dbc.backup(fake_db(tmp_path / "live"), tmp_path / "bk", "l")
    seed, _, _ = dbc.seed(backup_dir, "test", "s", cfg)
    dbc.job_copy(seed, hd / "ixmp_test" / "mains" / "results", "p", SHARED, model_src(tmp_path), "test", cfg,
                 kind="main")
    fake = tmp_path / "fake"
    (fake / "bin").mkdir(parents=True)
    (fake / "next").write_text("100")
    (fake / "bin" / "sbatch").write_text(FAKE_SBATCH)
    (fake / "bin" / "sbatch").chmod(0o755)
    env = {"HOME": str(Path.home()), "PATH": f"{fake / 'bin'}:/usr/bin:/bin", "FAKE": str(fake), "CODE": str(code)}
    return cfg, hd, code, seed, fake, env


def test_submit_runs_from_relative_paths(project, tmp_path):
    cfg, hd, code, seed, fake, env = _setup(project, tmp_path)
    caller = tmp_path / "caller"
    caller.mkdir()
    (caller / "batch1.txt").write_text("# NAME SCENARIO COMMAND\n"
                                       "a_run sc_a python solve.py --out=x,y scenario=no\n"
                                       "b_run - python read.py\n")
    (caller / "seedlink").symlink_to(seed)
    out = subprocess.run(["bash", str(code / ".ixmp_copies/slurm/submit_runs.sh"), "seedlink",
                          str(hd / "ixmp_test" / "mains" / "results"), "batch1.txt"],
                         cwd=caller, capture_output=True, text=True, env={**env, "AREA": "test", "MODEL": "M"})
    assert out.returncode == 0, out.stdout + out.stderr
    log = (fake / "sbatch.log").read_text().splitlines()
    assert len(log) == 3, log  # two runs, one merge
    run_a, merge_a, run_b = (line.split("|") for line in log)
    assert "MERGE_SCENARIO=sc_a" in run_a and "MODEL=M" in run_a and "CMD=python solve.py --out=x,y scenario=no" in run_a
    assert f"SEED={seed.resolve()}" in run_a and "--partition=short" in run_a[-1]
    assert "SCENARIO=sc_a" in merge_a and "afterok:101,singleton" in merge_a[-1] and "--partition=short" in merge_a[-1]
    assert "MERGE_SCENARIO=" in run_b and "MERGE_SCENARIO=sc" not in "|".join(run_b)
    rec = next((hd / "ixmp_test" / "runs").glob("submitted_batch1_into_results_*.txt")).read_text().splitlines()
    assert rec[1].startswith("run a_run job=101 dir=") and " scenario=sc_a merge=102 cmd=python solve.py" in rec[1]
    assert rec[2].startswith("run b_run job=103 ") and "scenario=" not in rec[2].split(" cmd=")[0]
    again = subprocess.run(["bash", str(code / ".ixmp_copies/slurm/submit_runs.sh"), str(seed),
                            str(hd / "ixmp_test" / "mains" / "results"), "batch1.txt"],
                           cwd=caller, capture_output=True, text=True, env={**env, "AREA": "test"})
    assert again.returncode == 1 and "already submitted" in again.stderr


def test_submit_sh(project, tmp_path):
    cfg, hd, code, seed, fake, env = _setup(project, tmp_path)
    caller = tmp_path / "caller"
    caller.mkdir()
    (caller / "bk").symlink_to(tmp_path / "bk")
    out = subprocess.run(["bash", str(code / ".ixmp_copies/slurm/submit.sh"), "job_seed.do", "AREA=test",
                          "NAME=seed2", "BACKUP=bk", "--", "--mem=2G"],
                         cwd=caller, capture_output=True, text=True, env=env)
    assert out.returncode == 0 and out.stdout.strip() == "101", out.stdout + out.stderr
    line = (fake / "sbatch.log").read_text().splitlines()[0].split("|")
    assert line[1] == str(hd / "ixmp_test" / "runs")  # logs in the area's runs folder
    assert "NAME=seed2" in line and f"BACKUP={(tmp_path / 'bk').resolve()}" in line
    assert line[-1].startswith("--parsable --export=ALL --partition=short --mem=2G ") and line[-1].endswith("job_seed.do")
    nolog = subprocess.run(["bash", str(code / ".ixmp_copies/slurm/submit.sh"), "job_make_main.do", "SEED=x"],
                           cwd=caller, capture_output=True, text=True, env=env)
    assert nolog.returncode == 0 and (fake / "sbatch.log").read_text().splitlines()[1].split("|")[1] == str(caller)
    bad = subprocess.run(["bash", str(code / ".ixmp_copies/slurm/submit.sh"), "nope.do"], cwd=caller,
                         capture_output=True, text=True, env=env)
    assert bad.returncode == 1 and "no template" in bad.stderr
    notvar = subprocess.run(["bash", str(code / ".ixmp_copies/slurm/submit.sh"), "job_seed.do", "NAME"], cwd=caller,
                            capture_output=True, text=True, env=env)
    assert notvar.returncode == 1 and "not VAR=value" in notvar.stderr


def test_templates_carry_no_partition():
    slurm = Path(stage.SLURM)
    for template in slurm.glob("*.do"):
        assert "--partition" not in template.read_text(), template.name
