"""Moving scenarios between ixmp platforms: from a shared database (e.g. ixmp-dev) to a local
HyperSQL one and back, and from a job's database copy into a results main.

A scenario is moved by ixmp's Java-level cross-platform clone (clone_across: solution and
timeseries included). Before a scenario can land, the target platform must know every unit,
region and time slice it uses (ixmp's JDBC backend refuses a row whose unit or node it
lacks), and reporting maps regions through the platform's region synonyms: those
platform-level lists are the "registry" here, read from the source and written into the
target.

A local platform is a HyperSQL file database. It must use CACHED tables (see
copies.require_cached_tables); only one process may open it at a time.

Everything that opens a platform takes it from the caller; nothing here opens one.
ixmp and message_ix are imported where they are used, so the file-level commands run in an
environment without them.
"""

from __future__ import annotations

from pathlib import Path


class AlreadyMerged(ValueError):
    """The version a merge would bring in is already on the target (its marker is there)."""


def hsqldb_file(platform_info: dict) -> Path:
    """The database file stem of a file-backed HyperSQL platform, from its ixmp config entry
    (`ixmp.config.get_platform_info(name)[1]`): `path`, or the file part of a
    jdbc:hsqldb:file: `url` (properties after ';' dropped). Raises ValueError otherwise."""
    if platform_info.get("driver") != "hsqldb":
        raise ValueError(f"not a HyperSQL platform: {platform_info}")
    if platform_info.get("path"):
        return Path(platform_info["path"]).expanduser()
    url = platform_info.get("url", "")
    prefix = "jdbc:hsqldb:file:"
    if not url.startswith(prefix):
        raise ValueError(f"not a file-backed HyperSQL url: {url!r}")
    return Path(url[len(prefix):].split(";", 1)[0]).expanduser()


def platform_db(name: str) -> Path:
    """The database stem of the platform `name` in this process's ixmp config."""
    import ixmp

    return hsqldb_file(ixmp.config.get_platform_info(name)[1])


def ixmp_config():
    """ixmp's config, with message_ix imported: only that import registers the
    'message model dir' key, and reading it before raises AttributeError."""
    import ixmp
    import message_ix  # noqa: F401

    return ixmp.config


def is_hsqldb(name: str) -> bool:
    import ixmp

    return ixmp.config.get_platform_info(name)[1].get("driver") == "hsqldb"


def read_registry(mp) -> dict:
    """The platform-level lists of `mp`: units (unit), regions (region, mapped_to, parent,
    hierarchy; a region proper has an empty mapped_to, a synonym names the region it maps to)
    and time slices (name, category, duration)."""
    import pandas as pd

    return {"units": pd.DataFrame({"unit": sorted(mp.units())}),
            "regions": mp.regions().reset_index(drop=True),
            "timeslices": mp.timeslices().reset_index(drop=True)}


def seed_registry(mp, registry: dict, log=print) -> dict:
    """Add to `mp` every unit, region, region synonym and time slice of `registry` that it
    lacks. Regions go parents first (a region's parent must exist when it is added); a
    synonym is added after its target. Returns the number added per kind."""
    added = {"units": 0, "regions": 0, "synonyms": 0, "timeslices": 0, "units_space_twins": []}
    # HyperSQL compares names with trailing spaces padded (SQL PAD SPACE), so 'USD/t ' on
    # Oracle is the same unit as 'USD/t' here and adding both fails; case is distinct.
    have_units = {u.rstrip() for u in mp.units()}
    for unit in registry["units"]["unit"]:
        if unit.rstrip() in have_units:
            if unit not in set(mp.units()):
                added["units_space_twins"].append(unit)
            continue
        mp.add_unit(unit, "copied by ixmp-copies")
        have_units.add(unit.rstrip())
        added["units"] += 1
    regions = registry["regions"].fillna("")
    is_synonym = (regions["mapped_to"] != "") & (regions["mapped_to"] != regions["region"])
    have = set(mp.regions()["region"])
    pending = regions[~is_synonym].to_dict("records")
    while pending:
        progressed, rest = False, []
        for row in pending:
            if row["region"] in have:
                progressed = True
                continue
            if row["parent"] in have or row["parent"] == row["region"]:
                mp.add_region(row["region"], row["hierarchy"], row["parent"])
                have.add(row["region"])
                added["regions"] += 1
                progressed = True
            else:
                rest.append(row)
        if not progressed:
            raise ValueError(f"regions whose parent never appears: {[r['region'] for r in rest][:10]}")
        pending = rest
    for row in regions[is_synonym].to_dict("records"):
        if row["region"] not in have:
            mp.add_region_synonym(row["region"], row["mapped_to"])
            have.add(row["region"])
            added["synonyms"] += 1
    have_ts = set(mp.timeslices()["name"])
    for row in registry["timeslices"].to_dict("records"):
        if row["name"] not in have_ts:
            mp.add_timeslice(row["name"], row["category"], float(row["duration"]))
            added["timeslices"] += 1
    log(f"registry seeded: {added}")
    return added


def clone_across(scen, dest_mp, name: str | None = None, annotation: str = ""):
    """Copy `scen` (solution and timeseries included) onto another JDBC platform by ixmp's
    Java-level cross-platform clone; returns the copy. ixmp only supports this with
    keep_solution=True and without a first_model_year shift."""
    return scen.clone(model=scen.model, scenario=name or scen.scenario, platform=dest_mp,
                      annotation=annotation or f"copy of {scen.model}/{scen.scenario} v{scen.version}",
                      keep_solution=True)


def compare_copy(original, copy, tol_obj: float = 1e-9) -> dict:
    """What a copy must keep: the solution flag, the objective (relative `tol_obj`), the
    timeseries row count and the row counts of demand, input and output. Returns
    {ok, failures, rows, timeseries_rows, solved, OBJ}."""
    failures, rows = [], {}
    for name in ("demand", "input", "output"):
        n_orig, n_copy = len(original.par(name)), len(copy.par(name))
        rows[name] = (n_orig, n_copy)
        if n_copy != n_orig:
            failures.append(f"{name}: {n_orig} rows in the original, {n_copy} in the copy")
    ts = (len(original.timeseries()), len(copy.timeseries()))
    if ts[0] != ts[1]:
        failures.append(f"timeseries: {ts[0]} rows in the original, {ts[1]} in the copy")
    solved = (original.has_solution(), copy.has_solution())
    obj = (None, None)
    if solved[0] != solved[1]:
        failures.append(f"solution flag {solved[0]} -> {solved[1]}")
    elif solved[0]:
        obj = (float(original.var("OBJ")["lvl"]), float(copy.var("OBJ")["lvl"]))
        if abs(obj[1] / obj[0] - 1.0) > tol_obj:
            failures.append(f"OBJ {obj[0]} -> {obj[1]}")
    return {"ok": not failures, "failures": failures, "rows": rows, "timeseries_rows": ts,
            "solved": solved, "OBJ": obj}


def versions(mp, model: str, scenario: str, default: bool):
    # Unfiltered by name: ixmp's JDBC scenario_list(scen=...) raises ValueError for a name the
    # platform does not have, which is the normal case for a first merge.
    listed = mp.scenario_list(model=model, default=default)
    return listed[listed["scenario"] == scenario]


def default_version(mp, model: str, scenario: str) -> int:
    listed = versions(mp, model, scenario, default=True)
    if not len(listed):
        raise LookupError(f"{model}/{scenario} has no default version on this platform")
    return int(listed["version"].iloc[0])


def merge_scenario(src_mp, dst_mp, model: str, scenario: str, version: int,
                   marker: str, marker_key: str) -> dict:
    """Bring one version from a job's database copy (`src_mp`) into a main platform
    (`dst_mp`) by cross-platform clone (default_version finds the job copy's default). The
    clone lands as the next version there; ixmp's clone never sets a default, so it is made
    default only when it was the default in the job copy and compare_copy passes. `marker`
    is stored as the new version's scenario meta `marker_key` right after the clone (the
    JDBC clone overwrites the annotation with its own, so the annotation cannot carry it). A
    version on `dst_mp` already carrying the marker means this merge was made before:
    AlreadyMerged, rather than a duplicate."""
    import message_ix

    listed = versions(dst_mp, model, scenario, default=False)
    done = [int(v) for v in listed["version"]
            if message_ix.Scenario(dst_mp, model, scenario, version=int(v)).get_meta().get(marker_key) == marker]
    if done:
        raise AlreadyMerged(f"{model}/{scenario} already merged as version(s) {done}: {marker}")
    src_default = versions(src_mp, model, scenario, default=True)
    default = int(src_default["version"].iloc[0]) if len(src_default) else None
    src = message_ix.Scenario(src_mp, model, scenario, version=version)
    copy = clone_across(src, dst_mp, annotation=marker)
    copy.set_meta(marker_key, marker)
    check = compare_copy(src, copy)
    was_default = default == version
    if was_default and check["ok"]:
        copy.set_as_default()
    return {"model": model, "scenario": scenario, "source_version": version,
            "merged_version": int(copy.version), "was_default_in_job": was_default,
            "set_default": was_default and check["ok"], "marker": marker, "compare": check}
