# tyler_lc-work-plugins

Claude Code plugins for MESSAGE-ix / IIASA modelling work. Each plugin's skill is plain Markdown,
so other agents can use it too.

| Plugin | What it does |
|---|---|
| [ixmp-copies](plugins/ixmp-copies) | Run solves on a SLURM cluster against checked copies of a local HyperSQL ixmp database, merge the results back, and move scenarios between ixmp-dev and a local database |

## Install

Claude Code:

```
/plugin marketplace add Tyler-lc/tyler_lc-work-plugins
/plugin install ixmp-copies@tyler_lc-work-plugins
```

A plugin that carries a Python tool is also installed into the project's venv; each plugin's
README says how, and its `SETUP.md` covers first-time setup.

Other agents: clone this repository and point the agent at `plugins/<plugin>/README.md`, which
links the skill and the setup steps.
