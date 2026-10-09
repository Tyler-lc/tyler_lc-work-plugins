"""`ixmp-copies doctor`: every prerequisite the workflow has, checked in the order a new user
meets them, each with the fix when it fails. Local checks run always; cluster checks go over
the SSH connection and are skipped when it is not open.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ixmp_copies import provenance
from ixmp_copies.config import Config, ConfigError, load
from ixmp_copies.copies import hdrive_candidates, reachable, require_cached_tables
from ixmp_copies.platforms import hsqldb_file


@dataclass
class Check:
    name: str
    status: str  # ok, warn, FAIL, skip
    detail: str = ""
    fix: str = ""


def _local(cfg: Config) -> list[Check]:
    out = []
    roots = [r for r in hdrive_candidates(cfg) if reachable(r)]
    out.append(Check("H drive reachable here", "ok" if roots else "FAIL",
                     str(roots[0]) if roots else f"none of {[str(r) for r in hdrive_candidates(cfg)]}",
                     "connect the VPN, then mount the share (on WSL after every reboot or VPN drop: "
                     "sudo mount <mount point>; SETUP.md, step 2); "
                     "or fix [storage] roots"))
    try:
        import ixmp
        import message_ix  # noqa: F401 -- registers 'message model dir' in ixmp's config
    except ImportError as err:
        out.append(Check("ixmp and message_ix importable", "FAIL", str(err), "activate the project's venv"))
        return out
    out.append(Check("ixmp and message_ix importable", "ok", f"ixmp {ixmp.__version__}, config {ixmp.config.path}"))
    try:
        info = ixmp.config.get_platform_info(cfg.platform)[1]
    except ValueError as err:
        out.append(Check(f"platform {cfg.platform!r} registered", "FAIL", str(err),
                         "ixmp-copies platform-add --apply (SETUP.md, step 6)"))
        info = None
    if info is not None:
        try:
            db = hsqldb_file(info)
        except ValueError as err:
            out.append(Check(f"platform {cfg.platform!r} is a HyperSQL file database", "FAIL", str(err),
                             "[project] platform must name a local HyperSQL platform"))
        else:
            out.append(Check(f"platform {cfg.platform!r} is a HyperSQL file database", "ok", str(db)))
            url = info.get("url", "")
            if "default_table_type=cached" not in url:
                out.append(Check("url creates CACHED tables", "warn", url or "(path, no url)",
                                 "add ;hsqldb.default_table_type=cached to the url before the database is "
                                 "first opened; an existing MEMORY database must be recreated"))
            try:
                n = require_cached_tables(db)
                out.append(Check("database has CACHED tables only", "ok", f"{n} tables"))
            except FileNotFoundError as err:
                out.append(Check("database has CACHED tables only", "skip", str(err)))
            except ValueError as err:
                out.append(Check("database has CACHED tables only", "FAIL", str(err),
                                 "recreate the database with the cached url and copy the scenarios in "
                                 "(ixmp-copies transfer)"))
    model_dir = ixmp.config.get("message model dir")
    out.append(Check("message model dir", "ok" if model_dir and Path(model_dir).is_dir() else "FAIL",
                     str(model_dir), "install message_ix in this venv (it sets the model dir)"))
    if model_dir and Path(model_dir).is_dir():
        out += _model_checks("here", source_report(str(model_dir), message_ix))
    out.append(Check("java on PATH (merges and transfers run a JVM)",
                     "ok" if shutil.which("java") else "warn", shutil.which("java") or "not found",
                     "install a JRE locally if you run transfers here"))
    try:
        top = subprocess.run(["git", "-C", str(cfg.project_root), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=True).stdout.strip()
        at_root = Path(top).resolve() == cfg.project_root.resolve()
        out.append(Check("config at the repository root", "ok" if at_root else "FAIL", top,
                         f"move {cfg.path.name} to {top} (stage archives the repository from there)"))
    except (FileNotFoundError, subprocess.CalledProcessError) as err:
        out.append(Check("project is a git repository", "FAIL", str(err), "stage ships a commit: git init"))
    return out


# Run inside a venv (here, or on the cluster with provenance.py's source prepended): which
# GAMS source solves use, and which message_ix the venv imports.
REPORT = """
import json, pathlib, ixmp, message_ix
cfg = str(ixmp.config.get("message model dir"))  # a str or a Path, depending on the ixmp version
pkg = str(pathlib.Path(message_ix.__file__).parent / "model")
print("IXC_REPORT " + json.dumps({"config_dir": cfg, "package_dir": pkg,
      "source": model_source(pathlib.Path(cfg), message_ix.__version__)}))
"""


def source_report(model_dir: str, message_ix) -> dict:
    return {"config_dir": model_dir, "package_dir": str(Path(message_ix.__file__).parent / "model"),
            "source": provenance.model_source(Path(model_dir), message_ix.__version__)}


def _model_checks(where: str, report: dict) -> list[Check]:
    src = report["source"]
    commit = (src["git_commit"] or "not in git")[:10]
    out = [Check(f"GAMS source {where}", "ok",
                 f"{src['path']} at {commit}, message_ix {src['message_ix_version']}, "
                 f"fingerprint {src['fingerprint'][:12]}")]
    for status, msg in provenance.assess(report["config_dir"], report["package_dir"], src):
        out.append(Check(f"GAMS source {where} consistent", status, msg))
    return out


def _remote(cfg: Config) -> list[Check]:
    def ssh(command: str, timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", cfg.ssh_host,
                               f"bash -lc {shlex.quote(command)}"],
                              capture_output=True, text=True, timeout=timeout)

    try:
        probe = ssh("whoami", timeout=30)
    except subprocess.TimeoutExpired:
        probe = None
    if probe is None or probe.returncode:
        detail = "timed out" if probe is None else probe.stderr.strip()[-300:]
        return [Check(f"ssh {cfg.ssh_host} without a prompt", "FAIL", detail,
                      f"connect the VPN, then run `ssh {cfg.ssh_host}` once interactively to open the "
                      "shared connection (SETUP.md, step 1); remaining cluster checks skipped")]
    user = probe.stdout.strip().splitlines()[-1]
    out = [Check(f"ssh {cfg.ssh_host} without a prompt", "ok", f"as {user}")]
    if cfg.cluster_user and cfg.cluster_user != user:
        out.append(Check("[cluster] user is the account ssh logs in as", "FAIL",
                         f"config {cfg.cluster_user!r}, ssh {user!r}", f"set [cluster] user = \"{user}\""))
    root = cfg.remote_hdrive.format(cluster_user=user, remote_user=user, user=user)
    ok = ssh(f"test -d {shlex.quote(root)} && test -w {shlex.quote(root)}").returncode == 0
    out.append(Check("H drive on the cluster", "ok" if ok else "FAIL", root,
                     "fix [cluster] remote_hdrive: the same share as [storage] roots, as the cluster sees it"))
    # Jobs find the share through [storage] roots, expanded on the cluster: the first one there
    # that exists and holds something is the one they use.
    roots = [r.format(cluster_user=user, user=user) for r in cfg.roots]
    probe_roots = "; ".join(f'r={shlex.quote(r) if not r.startswith("~") else "$HOME" + shlex.quote(r[1:])}; '
                            'test -d "$r" && [ -n "$(ls -A "$r" | head -c 1)" ] && { echo "$r"; exit 0; }'
                            for r in roots) + "; exit 1"
    found = ssh(probe_roots, timeout=60)
    out.append(Check("[storage] roots reach the share on the cluster", "ok" if found.returncode == 0 else "FAIL",
                     found.stdout.strip() or f"none of {roots}",
                     "list the cluster's path of the share in [storage] roots (at IIASA: /hdrive/all_users/<user>)"))
    venv = cfg.venv.replace("~", "$HOME", 1) if cfg.venv.startswith("~") else shlex.quote(cfg.venv)
    modules = " ".join(f"module load {shlex.quote(m)};" for m in cfg.modules)
    env = f"source {shlex.quote(cfg.lmod_init)} && module purge && {modules} source {venv}/bin/activate"
    py = ssh(f"{env} && python -c 'import sys, ixmp, message_ix; print(sys.version_info >= (3, 11), "
             f"sys.version.split()[0], ixmp.__version__, message_ix.__version__, ixmp.config.path)'", timeout=180)
    fields = py.stdout.strip().splitlines()[-1].split() if py.returncode == 0 and py.stdout.strip() else []
    out.append(Check("cluster venv imports ixmp and message_ix", "ok" if fields else "FAIL",
                     " ".join(fields[1:4]) if fields else (py.stdout or py.stderr).strip()[-300:],
                     "build the venv on the cluster with the module Python (SETUP.md, step 3) or fix "
                     "[cluster] venv / modules / lmod_init"))
    if fields:
        out.append(Check("cluster venv is Python 3.11 or newer", "ok" if fields[0] == "True" else "FAIL",
                         fields[1], "rebuild the cluster venv on Python 3.11+ (the tool reads its config with tomllib)"))
        out.append(Check("an ixmp config file on the cluster", "ok" if fields[4] != "None" else "FAIL",
                         fields[4], "on the login node, register any platform once (e.g. `ixmp platform add "
                         "local jdbc hsqldb ~/.local/share/ixmp/localdb/default`) so ixmp writes its config file; "
                         "job-copy copies its settings"))
    if fields:
        script = Path(provenance.__file__).read_text() + REPORT
        rep = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", cfg.ssh_host,
                              f"bash -lc {shlex.quote(env + ' && python -')}"],
                             input=script, capture_output=True, text=True, timeout=300)
        lines = [ln for ln in rep.stdout.splitlines() if ln.startswith("IXC_REPORT ")]
        if rep.returncode or not lines:
            out.append(Check("GAMS source on the cluster", "FAIL", (rep.stderr or rep.stdout).strip()[-300:],
                             "the cluster venv must import ixmp and message_ix"))
        else:
            out += _model_checks("on the cluster", json.loads(lines[-1].removeprefix("IXC_REPORT ")))
        java = ssh(f"{env} && java -version", timeout=60)
        out.append(Check("java on the cluster", "ok" if java.returncode == 0 else "FAIL",
                         java.stderr.strip().splitlines()[0] if java.stderr.strip() else "",
                         "add the Java module to [cluster] modules"))
    if cfg.gams_module:
        gams = ssh(f"source {shlex.quote(cfg.lmod_init)} && module load {shlex.quote(cfg.gams_module)} "
                   "&& command -v gams", timeout=60)
        out.append(Check("GAMS module on the cluster", "ok" if gams.returncode == 0 else "FAIL",
                         (gams.stdout or gams.stderr).strip()[-200:],
                         "fix [cluster] gams_module (`module avail gams` on the login node)"))
    sb = ssh("command -v sbatch", timeout=30)
    out.append(Check("sbatch on the login node", "ok" if sb.returncode == 0 else "FAIL",
                     sb.stdout.strip(), "the ssh host must be a SLURM login node"))
    return out


def run(remote: bool = True) -> list[Check]:
    try:
        cfg = load()
    except ConfigError as err:
        return [Check("project config", "FAIL", str(err), "ixmp-copies init, at the project root")]
    checks = [Check("project config", "ok", str(cfg.path))]
    if not cfg.cluster_user:
        checks.append(Check("[cluster] user set", "warn", "empty: paths use the local account's name",
                            "set [cluster] user to your cluster account"))
    checks += _local(cfg)
    checks += _remote(cfg) if remote else [Check("cluster checks", "skip", "--local")]
    return checks


def report(checks: list[Check]) -> int:
    for c in checks:
        print(f"{c.status:4}  {c.name}" + (f": {c.detail}" if c.detail else ""))
        if c.status == "FAIL" and c.fix:
            print(f"      fix: {c.fix}")
    failed = sum(c.status == "FAIL" for c in checks)
    print(f"\n{failed} failed, {sum(c.status == 'warn' for c in checks)} warnings")
    return 1 if failed else 0

