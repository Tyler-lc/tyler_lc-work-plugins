from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from ixmp_copies import config

SRC = Path(__file__).resolve().parents[1] / "src"

TOML = """\
[project]
platform = "proj-local"
model = "m"
records = "records"

[storage]
roots = ["{hd}"]
backups = "ixmp_backup"

[storage.areas]
test = "ixmp_test"
live = "ixmp_live"

[cluster]
ssh_host = "nohost"
remote_hdrive = "{remote}"
lmod_init = "{lmod}"
modules = ["Python/3", "Java"]
gams_module = ""
venv = "{venv}"

[stage]
paths = ["."]
"""


def fake_db(folder: Path, modified: str = "no") -> Path:
    folder.mkdir(parents=True)
    db = folder / "db"
    Path(f"{db}.properties").write_text(f"#HSQL Database Engine 2.5.1\nmodified={modified}\nversion=2.5.1\n")
    Path(f"{db}.script").write_text("CREATE CACHED TABLE A\nCREATE CACHED TABLE B\n")
    Path(f"{db}.data").write_bytes(bytes(range(256)) * 4096)
    Path(f"{db}.lobs").write_bytes(b"\x01" * 100_000)
    Path(f"{db}.tmp").mkdir()
    return db


@pytest.fixture
def project(tmp_path: Path):
    """A project folder with an ixmp_copies.toml whose H drive is tmp/hd, a fake lmod and a
    fake venv whose activate puts this interpreter first on PATH."""
    hd = tmp_path / "hd"
    hd.mkdir()
    (hd / ".keep").write_text("")  # an empty folder does not count as a reachable share
    lmod = tmp_path / "lmod_init.sh"
    lmod.write_text("module() { echo \"module $*\" >&2; }\n")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "activate").write_text(f'export PATH="{Path(sys.executable).parent}:$PATH"\n'
                                           f'python() {{ "{sys.executable}" "$@"; }}\n')
    proj = tmp_path / "proj"
    proj.mkdir()
    path = proj / config.FILENAME
    path.write_text(TOML.format(hd=hd, remote=hd, lmod=lmod, venv=venv))
    return config.parse(path), hd


def run_cli(args: list[str], cwd: Path, **env: str) -> subprocess.CompletedProcess:
    full = {k: v for k, v in os.environ.items() if k not in ("IXMP_DATA", config.ENV)}
    full["PYTHONPATH"] = str(SRC)
    full.update(env)
    return subprocess.run([sys.executable, "-m", "ixmp_copies", *args], cwd=cwd, env=full,
                          capture_output=True, text=True)


def has_ixmp() -> bool:
    try:
        import ixmp  # noqa: F401
    except ImportError:
        return False
    return True
