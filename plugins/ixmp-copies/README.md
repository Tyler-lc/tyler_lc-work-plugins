# ixmp-copies

Run MESSAGE-ix / ixmp work on a SLURM cluster against checked copies of a local HyperSQL ixmp
database, and merge the solved scenarios back into a main database.

A HyperSQL database can be open in one process at a time, and a copy taken while it is open is
corrupt. `ixmp-copies` gives every cluster job its own verified copy of a read-only seed, keeps
each job's ixmp config pointed at that copy alone, and brings results back through a backed-up,
compared, one-at-a-time merge. Every operation refuses rather than guesses when the state is not
what it expects.

- `SETUP.md`: first-time setup (SSH, H drive, cluster venv, local platform, project config)
- `skills/db-copies/SKILL.md`: the procedure, refusals, exit codes and limits
- `src/ixmp_copies/`: the tool (`ixmp-copies --help`); `slurm/` inside it holds the job templates
- `tests/`: `uv run --with pytest python -m pytest` in an environment with ixmp and message_ix
  (the integration test also needs java; it builds real HyperSQL databases in a temp folder)

```
ixmp-copies init | doctor | where
ixmp-copies backup | restore | verify | seed | stage | collect | transfer
ixmp-copies job-copy | job-check | job-close | merge     # inside jobs
```
