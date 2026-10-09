"""doctor, local and cluster checks. The cluster is this machine, reached through a fake ssh that
runs the command here, so the real check commands run, the GAMS-source report over stdin included."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import fake_db, has_ixmp, run_cli
from test_cli_stage import _git_project

from ixmp_copies import config

pytestmark = pytest.mark.skipif(not has_ixmp(), reason="doctor imports ixmp")

FAKE_SSH = """#!/bin/bash
while [ $# -gt 0 ]; do case "$1" in -o) shift 2 ;; -*) shift ;; *) break ;; esac; done
shift  # the host
exec bash -c "$*"
"""


def _doctor(project, tmp_path, edit=lambda text: text, venv=None):
    cfg, hd = project
    text = edit(cfg.path.read_text())
    if venv:
        text = text.replace(f'venv = "{cfg.venv}"', f'venv = "{venv}"')
    cfg.path.write_text(text)
    _git_project(config.parse(cfg.path))
    db = fake_db(tmp_path / "localdb")
    home = tmp_path / "ixmp_home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"platform": {"default": "proj-local", "proj-local": {
        "class": "jdbc", "driver": "hsqldb", "url": f"jdbc:hsqldb:file:{db};hsqldb.default_table_type=cached"}}}))
    fake = tmp_path / "fakebin"
    fake.mkdir(exist_ok=True)
    for name, body in (("ssh", FAKE_SSH), ("sbatch", "#!/bin/sh\nexit 0\n")):
        (fake / name).write_text(body)
        (fake / name).chmod(0o755)
    out = run_cli(["doctor"], cfg.project_root, IXMP_DATA=str(home),
                  PATH=f"{fake}:{Path(__import__('sys').executable).parent}:/usr/bin:/bin")
    lines = {ln[6:].split(":")[0]: ln[:4].strip() for ln in out.stdout.splitlines() if ln[:4].strip() in
             ("ok", "warn", "FAIL", "skip")}
    return out, lines


def test_doctor_all_clear(project, tmp_path):
    out, lines = _doctor(project, tmp_path)
    assert out.returncode == 0, out.stdout
    for check in ("H drive reachable here", "platform 'proj-local' is a HyperSQL file database",
                  "database has CACHED tables only", "ssh nohost without a prompt", "H drive on the cluster",
                  "[storage] roots reach the share on the cluster", "cluster venv imports ixmp and message_ix",
                  "cluster venv is Python 3.11 or newer", "an ixmp config file on the cluster",
                  "GAMS source on the cluster", "sbatch on the login node"):
        assert lines.get(check) == "ok", (check, out.stdout)
    assert lines.get("[cluster] user set") == "warn"


def test_doctor_finds_what_breaks_jobs(project, tmp_path):
    """A wrong cluster user, roots the cluster cannot see, an old Python without an ixmp config."""
    old = tmp_path / "oldvenv"
    (old / "bin").mkdir(parents=True)
    (old / "bin" / "activate").write_text(f'export PATH="{old / "bin"}:$PATH"\n')
    (old / "bin" / "python").write_text('#!/bin/bash\n[ "$1" = -c ] && { echo "False 3.10.4 3.11 3.11 None"; exit 0; }\nexit 1\n')
    (old / "bin" / "python").chmod(0o755)
    cfg, hd = project

    def edit(text):
        return (text.replace('ssh_host = "nohost"', 'ssh_host = "nohost"\nuser = "someone_else"')
                .replace(f'roots = ["{hd}"]', f'roots = ["{hd}", "/nonexistent/share"]'))

    out, lines = _doctor(project, tmp_path, edit, venv=str(old))
    assert out.returncode == 1
    assert lines["[cluster] user is the account ssh logs in as"] == "FAIL"
    assert lines["cluster venv is Python 3.11 or newer"] == "FAIL"
    assert lines["an ixmp config file on the cluster"] == "FAIL"
    assert lines["GAMS source on the cluster"] == "FAIL"
    cfg.path.write_text(cfg.path.read_text().replace(f'roots = ["{hd}", "/nonexistent/share"]',
                                                     'roots = ["/nonexistent/share"]'))
    out2 = run_cli(["doctor", "--local"], cfg.project_root)
    assert out2.returncode == 1 and "FAIL  H drive reachable here" in out2.stdout and "sudo mount" in out2.stdout
