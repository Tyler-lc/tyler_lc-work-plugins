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
vars="$vars|SEED=${SEED:-}|BACKUP=${BACKUP:-}|CMD=${CMD:-}|SRC_JOB=${SRC_JOB:-}|AREA=${AREA:-}"
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
    assert "MERGE_SCENARIO=-" in run_b  # declared to merge nothing: cleanup may delete it
    rec = next((hd / "ixmp_test" / "runs").glob("submitted_batch1_into_results_*.txt")).read_text().splitlines()
    assert rec[1].startswith("run a_run job=101 dir=") and " scenario=sc_a model=M cmd=python solve.py" in rec[1]
    assert rec[2] == "merge a_run merge=102", rec
    assert rec[3].startswith("run b_run job=103 ") and "scenario=" not in rec[3].split(" cmd=")[0]
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


def test_submit_runs_reads_a_last_line_without_newline(project, tmp_path):
    cfg, hd, code, seed, fake, env = _setup(project, tmp_path)
    caller = tmp_path / "caller"
    caller.mkdir()
    (caller / "batch.txt").write_text("a_run sc_a python a.py\nb_run sc_b python b.py")  # no final newline
    out = subprocess.run(["bash", str(code / ".ixmp_copies/slurm/submit_runs.sh"), str(seed),
                          str(hd / "ixmp_test" / "mains" / "results"), "batch.txt"],
                         cwd=caller, capture_output=True, text=True, env={**env, "AREA": "test"})
    assert out.returncode == 0, out.stdout + out.stderr
    log = (fake / "sbatch.log").read_text().splitlines()
    assert len(log) == 4 and "CMD=python b.py" in log[2], log  # both runs, both merges
    rec = next((hd / "ixmp_test" / "runs").glob("submitted_batch_into_results_*.txt")).read_text()
    assert " model=m cmd=python a.py" in rec and " model=m cmd=python b.py" in rec  # [project] model


def test_submit_sh_passes_only_what_it_is_given(project, tmp_path):
    """A template variable exported in the calling shell does not reach the job; only the path
    variables are made absolute, so an area named like a folder beside the caller stays a name."""
    cfg, hd, code, seed, fake, env = _setup(project, tmp_path)
    caller = tmp_path / "share_folder"
    (caller / "test").mkdir(parents=True)
    (caller / "seed3").mkdir()
    out = subprocess.run(["bash", str(code / ".ixmp_copies/slurm/submit.sh"), "job_seed.do", "AREA=test",
                          "NAME=seed3", "SRC_JOB=jobs/a_7"], cwd=caller, capture_output=True, text=True,
                         env={**env, "BACKUP": "/old/backup", "MODEL": "stale"})
    assert out.returncode == 0, out.stdout + out.stderr
    line = (fake / "sbatch.log").read_text().splitlines()[0].split("|")
    assert "BACKUP=" in line and "MODEL=" in line, line  # unset, not inherited
    assert "AREA=test" in line and "NAME=seed3" in line, line
    assert f"SRC_JOB={caller}/jobs/a_7" in line, line  # relative, not there yet: still made absolute


def test_job_seed_refuses_two_sources(project, tmp_path):
    cfg, hd, code, seed, fake, env = _setup(project, tmp_path)
    out = subprocess.run(["bash", str(code / ".ixmp_copies/slurm/job_seed.do")], capture_output=True, text=True,
                         env={**env, "AREA": "test", "NAME": "s9", "BACKUP": "/b", "SRC_JOB": "/j"}, cwd=tmp_path)
    assert out.returncode == 3 and "REFUSED: both BACKUP" in out.stdout, out.stdout + out.stderr
    assert not (hd / "ixmp_test" / "seeds" / "s9").exists()


def test_templates_carry_no_partition():
    slurm = Path(stage.SLURM)
    for template in slurm.glob("*.do"):
        assert "--partition" not in template.read_text(), template.name


def test_a_run_whose_merge_was_not_submitted_stays_recorded(project, tmp_path):
    """The run is submitted and running; its merge's sbatch fails (a bad MERGE_OPTS, the controller
    gone). The record holds the run, so submit_merges.sh can merge it; the batch stops there."""
    cfg, hd, code, seed, fake, env = _setup(project, tmp_path)
    (fake / "bin" / "sbatch").write_text(FAKE_SBATCH.replace(
        "#!/bin/bash\n", '#!/bin/bash\n[ -n "${SCENARIO:-}" ] && { echo "sbatch: error: invalid option" >&2; exit 1; }\n'))
    caller = tmp_path / "caller"
    caller.mkdir()
    (caller / "batch.txt").write_text("a_run sc_a python a.py\nb_run sc_b python b.py\n")
    main = hd / "ixmp_test" / "mains" / "results"
    out = subprocess.run(["bash", str(code / ".ixmp_copies/slurm/submit_runs.sh"), str(seed), str(main), "batch.txt"],
                         cwd=caller, capture_output=True, text=True, env={**env, "AREA": "test"})
    assert out.returncode == 1 and "the merge of a_run could not be submitted" in out.stderr, out.stdout + out.stderr
    rec_path = next((hd / "ixmp_test" / "runs").glob("submitted_batch_into_results_*.txt"))
    rec = rec_path.read_text().splitlines()
    assert len(rec) == 2 and rec[1].startswith("run a_run job=101 dir=") and " scenario=sc_a model=m cmd=python a.py" \
        in rec[1], rec
    # submit_merges.sh reads the run line and submits its merge after the run, still running.
    (fake / "bin" / "sbatch").write_text(FAKE_SBATCH)
    (fake / "bin" / "sacct").write_text('#!/bin/bash\necho RUNNING\n')
    (fake / "bin" / "sacct").chmod(0o755)
    again = subprocess.run(["bash", str(code / ".ixmp_copies/slurm/submit_merges.sh"), str(rec_path)],
                           capture_output=True, text=True, env=env)
    assert again.returncode == 0, again.stdout + again.stderr
    merge = (fake / "sbatch.log").read_text().splitlines()[-1].split("|")
    assert "SCENARIO=sc_a" in merge and "afterok:101,singleton" in merge[-1], merge
    assert "remerge a_run run=101 merge=102" in rec_path.read_text()
