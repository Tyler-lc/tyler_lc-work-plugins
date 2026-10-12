# tyler_lc-work-plugins

Public Claude Code marketplace (`tyler-lc-work-plugins` in `.claude-plugin/marketplace.json`; the
repository keeps the underscore). Each plugin lives in `plugins/<name>/` with its own README,
CHANGELOG, skill and, where it carries a tool, a Python package.

## ixmp-copies

Tests, from `plugins/ixmp-copies/`, with the Python of a venv that has ixmp and message_ix (on the
WSL box `~/repos/message_uv`):

```bash
PYTHONPATH=src uv run --no-project --python ~/repos/message_uv/bin/python --with pytest python -m pytest -q
```

The full suite starts HyperSQL JVMs and one GAMS solve (GAMS under `~/opt/gams/`) and takes about
ten minutes; without ixmp most JVM tests skip. Run it serially: one ixmp JVM at a time on the
workstation, never several pytest processes in parallel. A changed or new test should fail
against the previous release: run it on a `git archive` of the last tag's `src/` before calling
it done.

The cluster acceptance check is `trial/new_project_trial.sh` (needs the user's UniCC socket and
the VPN). A release that changes a SLURM template or the merge path is pushed only after it
passes.

## Releases

Version lives in four places: `plugins/ixmp-copies/pyproject.toml`, `.claude-plugin/plugin.json`,
`src/ixmp_copies/__init__.py`, and the plugin's entry in `.claude-plugin/marketplace.json`. Every
release gets a CHANGELOG entry saying what changed, how to upgrade, and what was verified.

## Commits

Subject `component: what changed` or an imperative sentence; the body says why. No Claude or
Anthropic co-author trailers. Ask the user before pushing: the repository is public.
