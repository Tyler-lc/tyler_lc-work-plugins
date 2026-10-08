"""Project settings for ixmp-copies, read from `ixmp_copies.toml`.

The file sits at a project's root (`ixmp-copies init` writes one). Every path the tool uses
comes from it, so nothing about a project, a user or a cluster is written into the code.
It is found through IXMP_COPIES_CONFIG, else by walking up from the working directory; a
job finds the copy that `stage` put at the root of its code snapshot the same way.
"""

from __future__ import annotations

import getpass
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

FILENAME = "ixmp_copies.toml"
ENV = "IXMP_COPIES_CONFIG"


class ConfigError(ValueError):
    """The project config is missing or does not say what the tool needs."""


@dataclass(frozen=True)
class Config:
    path: Path
    platform: str
    model: str | None
    records: str
    roots: tuple[str, ...]
    backups: str
    areas: dict[str, str]
    ssh_host: str
    remote_hdrive: str
    lmod_init: str
    modules: tuple[str, ...]
    gams_module: str
    venv: str
    partition: str
    java_heap_run: str
    java_heap_merge: str
    stage_paths: tuple[str, ...]
    marker_key: str
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def project_root(self) -> Path:
        return self.path.parent

    @property
    def records_dir(self) -> Path:
        return self.project_root / self.records


def expand(template: str, **extra: str) -> str:
    """`~` and `{user}` (the user running the tool on this machine), plus `extra` fields."""
    return os.path.expanduser(template.format(user=getpass.getuser(), **extra))


def find(start: Path | None = None) -> Path:
    if os.environ.get(ENV):
        path = Path(os.environ[ENV])
        if not path.is_file():
            raise ConfigError(f"{ENV}={path} is not a file")
        return path
    here = (start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        if (folder / FILENAME).is_file():
            return folder / FILENAME
    raise ConfigError(f"no {FILENAME} in {here} or above, and {ENV} is not set: run `ixmp-copies init` "
                      "at the project root")


def _require(table: dict, key: str, where: str):
    if key not in table:
        raise ConfigError(f"[{where}] lacks {key!r}")
    return table[key]


def parse(path: Path) -> Config:
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as err:
        raise ConfigError(f"{path}: {err}") from err
    project = raw.get("project", {})
    storage = raw.get("storage", {})
    cluster = raw.get("cluster", {})
    platform = _require(project, "platform", "project")
    areas = _require(storage, "areas", "storage")
    if not isinstance(areas, dict) or not areas:
        raise ConfigError("[storage.areas] must map at least one area name to a folder")
    return Config(
        path=path.resolve(),
        platform=platform,
        model=project.get("model"),
        records=project.get("records", "ixmp_copies_records"),
        roots=tuple(_require(storage, "roots", "storage")),
        backups=_require(storage, "backups", "storage"),
        areas=dict(areas),
        ssh_host=cluster.get("ssh_host", "unicc"),
        remote_hdrive=cluster.get("remote_hdrive", "/hdrive/all_users/{remote_user}"),
        lmod_init=cluster.get("lmod_init", "/opt/apps/lmod/8.7/init/bash"),
        modules=tuple(cluster.get("modules", ())),
        gams_module=cluster.get("gams_module", ""),
        venv=_require(cluster, "venv", "cluster"),
        partition=cluster.get("partition", "generic"),
        java_heap_run=cluster.get("java_heap_run", "12g"),
        java_heap_merge=cluster.get("java_heap_merge", "16g"),
        stage_paths=tuple(raw.get("stage", {}).get("paths", ["."])),
        marker_key=raw.get("merge", {}).get("marker_key", "ixmp_copies_merged_from"),
        raw=raw,
    )


def load(start: Path | None = None) -> Config:
    return parse(find(start))


TEMPLATE = """\
# ixmp-copies settings for this project. Paths below the storage roots are folders on the
# shared H drive; `~` and `{{user}}` expand on the machine that runs the tool.

[project]
platform = "{platform}"        # ixmp platform name of the working HyperSQL database
model = "{model}"              # default --model for merge and transfer ("" = always pass it)
records = "ixmp_copies_records"  # backup/seed/merge records, relative to this file

[storage]
# The same share as seen from each machine; the first reachable one is used.
roots = ["~/hdrive", "/hdrive/all_users/{{user}}"]
backups = "ixmp_copies/{name}/backups"   # backups of databases outside every area

[storage.areas]
test = "ixmp_copies/{name}/test"   # trials of the procedure
live = "ixmp_copies/{name}/live"   # the project's runs

[cluster]
ssh_host = "unicc"                               # a Host in ~/.ssh/config (ControlMaster)
remote_hdrive = "/hdrive/all_users/{{remote_user}}" # the H-drive root as the cluster sees it
lmod_init = "/opt/apps/lmod/8.7/init/bash"
modules = ["Python/3.11.5-GCCcore-13.2.0", "Java"]
gams_module = "gams/gams48.6_linux_x64_64_sfx"
venv = "{venv}"                                  # on the cluster; must import ixmp, message_ix
partition = "generic"
java_heap_run = "12g"     # a full MESSAGE scenario solve peaked at 17 GB RSS with 8 GB heap
java_heap_merge = "16g"   # a merge holds two platforms in one JVM; 8 GB ran out

[stage]
paths = ["."]             # what `git archive` ships to the cluster ("." = the whole repo)

[merge]
marker_key = "ixmp_copies_merged_from"   # scenario meta that marks a merged version
"""


def template(name: str, platform: str, model: str, venv: str) -> str:
    return TEMPLATE.format(name=name, platform=platform, model=model, venv=venv)
