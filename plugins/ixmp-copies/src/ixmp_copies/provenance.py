"""Which GAMS source a solve uses, and whether it is the one the Python side expects.

A solve runs the GAMS files in ixmp's `message_model_dir`, which the user-wide ixmp config can
point anywhere, while the Python `message_ix` comes from whatever the venv imports. Several
venvs may import one editable checkout, so moving that checkout changes every project at once,
and an editable install keeps the version label it had when it was installed. None of this is
visible from inside a run, so each job records the source it copied (model_source) and
`doctor` compares config, package and label (assess).

Standard library only: `doctor` sends this file to the cluster's venv over SSH and runs it there.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
import subprocess
from pathlib import Path

# Run input/output and per-solve files: data/ and output/ hold the GDX of a run, cplex.opt and
# cplex.op2 are rewritten from each solve's options, the rest is GAMS scratch and listings.
# Hidden paths (e.g. Jupyter's .ipynb_checkpoints/ copies of .gms files) are never included.
NOT_SOURCE = ("225*", "*.lst", "*.log", "*.gdx", "*.~*", "cplex.opt", "cplex.op2", ".*")
RUN_FOLDERS = ("data", "output")
LABEL = re.compile(r"\+g([0-9a-f]{7,40})")


def _ignored(rel: Path) -> bool:
    return rel.parts[0] in RUN_FOLDERS or any(fnmatch.fnmatch(p, pat) for p in rel.parts for pat in NOT_SOURCE)


def fingerprint(model_dir: Path) -> str:
    """SHA-256 over the relative path and bytes of every GAMS source file in `model_dir`; equal
    for two folders exactly when their sources are byte-identical."""
    h = hashlib.sha256()
    for path in sorted(p for p in model_dir.rglob("*") if p.is_file()):
        rel = path.relative_to(model_dir)
        if _ignored(rel):
            continue
        h.update(str(rel).encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return h.hexdigest()


def _git(model_dir: Path, *args: str) -> str | None:
    try:
        # rstrip only: porcelain status lines start with a meaningful space (" D file").
        return subprocess.run(["git", "-C", str(model_dir), *args], capture_output=True, text=True,
                              check=True).stdout.rstrip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None


def model_source(model_dir: Path, message_ix_version: str | None = None) -> dict:
    """Where `model_dir`'s GAMS source comes from: its path, the git commit of the checkout it
    lies in (None outside git), the tracked files changed there, the content fingerprint, and
    the version label of the message_ix the caller imported."""
    model_dir = Path(model_dir).resolve()
    commit = _git(model_dir, "rev-parse", "HEAD")
    prefix = _git(model_dir, "rev-parse", "--show-prefix") or ""
    status = _git(model_dir, "status", "--porcelain", "--untracked-files=no", "--", ".") or ""
    # Lines are "XY path" with the path from the repository root; run folders are not source.
    changed = [ln for ln in status.splitlines()
               if not _ignored(Path(ln[3:].removeprefix(prefix.strip())))]
    return {"path": str(model_dir), "git_commit": commit, "git_changed": changed,
            "fingerprint": fingerprint(model_dir), "message_ix_version": message_ix_version}


def label_commit(version: str | None) -> str | None:
    """The commit an editable install's version label names (`...+g79e8809...`), if any."""
    match = LABEL.search(version or "")
    return match.group(1) if match else None


def assess(config_dir: str, package_dir: str, source: dict) -> list[tuple[str, str]]:
    """(status, message) pairs: ok, or warn with what is inconsistent and what it means."""
    out = []
    if Path(config_dir).resolve() == Path(package_dir).resolve():
        out.append(("ok", f"solves use the GAMS source of the imported message_ix ({config_dir})"))
    else:
        out.append(("warn", f"solves use GAMS from {config_dir} but Python message_ix "
                            f"{source.get('message_ix_version')} from {package_dir}: two releases may be "
                            "mixed. Set message_model_dir to the package's model folder unless the "
                            "other source is deliberate"))
    label, commit = label_commit(source.get("message_ix_version")), source.get("git_commit")
    # The label names the package's checkout; compare it only when solves use that checkout.
    same = Path(config_dir).resolve() == Path(package_dir).resolve()
    if same and label and commit and not commit.startswith(label):
        out.append(("warn", f"message_ix's version label names commit {label}, but the checkout is at "
                            f"{commit[:10]}: the label misreports the code (refresh it by reinstalling "
                            "the editable package); records carry the commit instead"))
    if source.get("git_changed"):
        out.append(("warn", f"uncommitted changes in the GAMS source: {source['git_changed']}"))
    return out
