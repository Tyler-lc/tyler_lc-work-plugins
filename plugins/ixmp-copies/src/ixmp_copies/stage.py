"""Shipping a project's code to the cluster, and bringing the tool's records back.

A job never runs from a live checkout: `stage` puts a committed snapshot of the project on the
H drive (<area>/code/<sha>/) through the SSH connection, extracted on the cluster's side of the
share. Into the same snapshot go the tool itself (.ixmp_copies/src), the SLURM templates
(.ixmp_copies/slurm), the project config at the snapshot root, and job.env, the cluster
settings rendered as shell variables for the job scripts. The cluster then needs nothing of
this tool installed: only a venv that imports ixmp and message_ix.

`collect` copies the records that jobs and merges wrote inside snapshots and job copies into
the project's records folder. Every record is write-once: an existing file is never replaced.
"""

from __future__ import annotations

import filecmp
import io
import shlex
import shutil
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Callable

from ixmp_copies import __version__
from ixmp_copies.config import Config
from ixmp_copies.copies import Refused

PACKAGE = Path(__file__).resolve().parent
SLURM = PACKAGE / "slurm"
BUNDLE = ".ixmp_copies"

# run(command, stdin bytes) -> CompletedProcess, on the machine that sees `remote_hdrive`.
Remote = Callable[[str, bytes | None], subprocess.CompletedProcess]


def ssh_remote(host: str) -> Remote:
    def run(command: str, stdin: bytes | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["ssh", "-o", "BatchMode=yes", host, command], input=stdin,
                              capture_output=True, timeout=3600)
    return run


def _ok(result: subprocess.CompletedProcess, what: str) -> str:
    if result.returncode:
        raise Refused(f"{what} failed (exit {result.returncode}): {result.stderr.decode(errors='replace')[-500:]}")
    return result.stdout.decode(errors="replace").strip()


def cluster_whoami(host: str) -> str | None:
    """The account `ssh host` logs in as, or None when the connection does not answer."""
    try:
        out = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, "whoami"],
                             capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def remote_root(cfg: Config, remote: Remote) -> str:
    """The H-drive root as the cluster sees it, for the account the connection logs in as;
    refuses when that is not [cluster] user."""
    user = _ok(remote("whoami", None), "ssh whoami (is the SSH connection open?)")
    if cfg.cluster_user and cfg.cluster_user != user:
        raise Refused(f"the connection logs in as {user!r}, but [cluster] user is {cfg.cluster_user!r}")
    return cfg.remote_hdrive.format(cluster_user=user, remote_user=user, user=user)


def _shell_path(path: str) -> str:
    # ~ must expand on the cluster, not here.
    return '"$HOME"' + shlex.quote(path[1:]) if path.startswith("~/") else shlex.quote(path)


def job_env(cfg: Config) -> str:
    values = {
        "IXC_PLATFORM": shlex.quote(cfg.platform),
        "IXC_MODEL": shlex.quote(cfg.model or ""),
        "IXC_LMOD_INIT": _shell_path(cfg.lmod_init),
        "IXC_MODULES": shlex.quote(" ".join(cfg.modules)),
        "IXC_GAMS_MODULE": shlex.quote(cfg.gams_module),
        "IXC_VENV": _shell_path(cfg.venv),
        "IXC_PARTITION": shlex.quote(cfg.partition),
        "IXC_HEAP_RUN": shlex.quote(cfg.java_heap_run),
        "IXC_HEAP_MERGE": shlex.quote(cfg.java_heap_merge),
    }
    return "# written by ixmp-copies stage from ixmp_copies.toml\n" + "".join(
        f"{k}={v}\n" for k, v in values.items())


def _git(cfg: Config, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cfg.project_root), *args], capture_output=True,
                          text=True, check=True).stdout.strip()


def _add(tar: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = len(data), mode, int(time.time())
    tar.addfile(info, io.BytesIO(data))


def bundle(cfg: Config, manifest: str, extras: list[tuple[Path, str]]) -> bytes:
    """The tar stream laid over the snapshot: tool, templates, config, job.env, extras."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for path in sorted(PACKAGE.rglob("*.py")):
            _add(tar, f"{BUNDLE}/src/ixmp_copies/{path.relative_to(PACKAGE)}", path.read_bytes())
        for path in sorted(SLURM.iterdir()):
            _add(tar, f"{BUNDLE}/slurm/{path.name}", path.read_bytes(), 0o755)
        _add(tar, f"{BUNDLE}/job.env", job_env(cfg).encode())
        _add(tar, cfg.path.name, cfg.path.read_bytes())
        for src, rel in extras:
            _add(tar, rel, src.read_bytes())
        _add(tar, "CODE_MANIFEST", manifest.encode())
    return buf.getvalue()


def parse_extra(spec: str, cfg: Config) -> tuple[Path, str]:
    """EXTRA is a path relative to the project root, or SRC_DIR:REL for a file another
    checkout holds (shipped to REL)."""
    src_dir, rel = spec.split(":", 1) if ":" in spec else (str(cfg.project_root), spec)
    src = Path(src_dir).expanduser() / rel
    if not src.is_file():
        raise Refused(f"extra file {src} does not exist")
    return src, rel


def stage(cfg: Config, area: str, extras: list[str], remote: Remote) -> str:
    """Stage HEAD of the project's repository as <area>/code/<sha>/ on the cluster's side;
    refuses tracked changes under the staged paths and an existing snapshot. Returns the
    snapshot path as the cluster sees it (the jobs' CODE)."""
    if area not in cfg.areas:
        raise Refused(f"unknown area {area!r}; known: {sorted(cfg.areas)}")
    top = Path(_git(cfg, "rev-parse", "--show-toplevel")).resolve()
    if top != cfg.project_root.resolve():
        raise Refused(f"{cfg.path} is not at the repository root {top}: stage needs it there")
    dirty = subprocess.run(["git", "-C", str(top), "diff", "--quiet", "HEAD", "--", *cfg.stage_paths])
    if dirty.returncode:
        raise Refused(f"tracked changes under {list(cfg.stage_paths)}: commit first (a snapshot is a commit)")
    files = [parse_extra(e, cfg) for e in extras]
    sha = _git(cfg, "rev-parse", "--short=10", "HEAD")
    dest = f"{remote_root(cfg, remote)}/{cfg.areas[area]}/code/{sha}"
    q, qp = shlex.quote(dest), shlex.quote(dest + ".partial")
    if remote(f"test -e {q} || test -e {qp}", None).returncode == 0:
        raise Refused(f"{dest} (or its .partial) already exists")
    _ok(remote(f"mkdir -p {qp}", None), "mkdir")
    archive = subprocess.run(["git", "-C", str(top), "archive", "HEAD", "--", *cfg.stage_paths],
                             capture_output=True, check=True).stdout
    _ok(remote(f"tar -x -C {qp}", archive), "extracting the archive")
    manifest = "".join([
        f"commit {_git(cfg, 'rev-parse', 'HEAD')}\n",
        f"staged {time.strftime('%Y-%m-%dT%H:%M:%S%z')} from {top} by ixmp-copies {__version__}\n",
        *(f"extra {rel} from {src}\n" for src, rel in files),
    ])
    _ok(remote(f"tar -x -C {qp}", bundle(cfg, manifest, files)), "extracting the tool bundle")
    _ok(remote(f"mv {qp} {q}", None), "promoting the snapshot")
    return dest


def collect(cfg: Config, area_dir: Path) -> dict[str, list[str]]:
    """Copy records (json) written in the area's snapshots (<area>/code/*/) and job copies
    (<area>/jobs/*/code/), plus the submission records in <area>/runs/, into the project's
    records folder. A json record is write-once; a submission record only grows (submit_merges.sh
    appends to it), so a copy that is a prefix of the source is updated. Returns copied, updated,
    already present (identical) and conflicting names; a conflict is never overwritten."""
    out = {"copied": [], "updated": [], "present": [], "conflicts": []}
    dest = cfg.records_dir
    dest.mkdir(parents=True, exist_ok=True)
    sources = [*area_dir.glob(f"code/*/{cfg.records}/*.json"),
               *area_dir.glob(f"jobs/*/code/{cfg.records}/*.json"),
               *area_dir.glob("runs/submitted_*.txt")]
    for src in sorted(sources, key=lambda p: p.name):
        target = dest / src.name
        if not target.exists():
            shutil.copy2(src, target)
            out["copied"].append(src.name)
        elif filecmp.cmp(src, target, shallow=False):
            out["present"].append(src.name)
        elif src.suffix == ".txt" and src.read_bytes().startswith(target.read_bytes()):
            shutil.copy2(src, target)
            out["updated"].append(src.name)
        else:
            out["conflicts"].append(f"{src} differs from {target}")
    return out
