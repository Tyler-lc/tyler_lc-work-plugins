# tyler_lc-work-plugins

Claude Code plugins for MESSAGE-ix / IIASA modelling work.

| Plugin | What it does |
|---|---|
| [ixmp-copies](plugins/ixmp-copies) | Run solves on a SLURM cluster against checked copies of a local HyperSQL ixmp database, and merge the results back |

```
/plugin marketplace add Tyler-lc/tyler_lc-work-plugins
/plugin install ixmp-copies@tyler_lc-work-plugins
```

A plugin that carries a Python tool also installs into a project venv from a clone:
`uv pip install --python <venv>/bin/python -e plugins/<plugin>`.
