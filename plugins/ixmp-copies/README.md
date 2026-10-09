# ixmp-copies

Run MESSAGE-ix / ixmp work on a SLURM cluster against checked copies of a local HyperSQL ixmp
database, and merge the solved scenarios back into a main database.

A HyperSQL database can be open in one process at a time, and a copy taken while it is open is
corrupt. `ixmp-copies` gives every cluster job its own verified copy of a read-only seed, keeps
each job's ixmp config pointed at that copy alone, gives each job its own copy of the GAMS model
folder (recording which source it came from), and brings results back through a backed-up,
compared, one-at-a-time merge. Every operation refuses rather than guesses when the state is not
what it expects. It also moves scenarios between a shared database (e.g. ixmp-dev) and a local
one.

| File | For |
|---|---|
| [`SETUP.md`](SETUP.md) | first-time setup, per person and per project, ending in an end-to-end trial |
| [`skills/db-copies/SKILL.md`](skills/db-copies/SKILL.md) | the procedure: the chain, running a batch, refusals, exit codes, limits |
| `src/ixmp_copies/` | the tool (`ixmp-copies --help`); `slurm/` inside it holds the job templates |
| `trial/new_project_trial.sh` | the acceptance trial a new setup must pass |
| `tests/` | offline tests, plus a real-HyperSQL integration test when java is present |

## For an agent installing this

The skill is plain Markdown: any agent can read `skills/db-copies/SKILL.md` as its instructions.
In Claude Code it is loaded by the plugin:

```
/plugin marketplace add Tyler-lc/tyler_lc-work-plugins
/plugin install ixmp-copies@tyler_lc-work-plugins
```

Setting up a project, in order. Steps 1-3 need the user, because they involve their accounts and
machine; ask, do not work around them.

1. **Ask the user** to connect the VPN and open the SSH connection (`ssh <host>` once,
   interactively; SETUP.md step 1). Without it every cluster step fails with
   `Permission denied`; never try to fix keys.
2. **Ask the user** which venv on the cluster the project uses (SETUP.md step 3). Never pick
   another project's venv: it runs that project's branches.
3. **Check** that the H drive is mounted on the workstation (`timeout 10 ls ~/hdrive`;
   SETUP.md step 2). A hang means the VPN is down.
4. Install the tool into the project's venv (SETUP.md step 4).
5. At the project's git root: `ixmp-copies init --venv 'CLUSTER_VENV' --model MODEL --cluster-user USER`,
   review the file, commit it.
6. `ixmp-copies platform-add` (dry run), then `--apply`, if the platform is not registered yet.
7. `ixmp-copies doctor`. Fix every `FAIL` with the printed fix; report each `warn` to the user.
8. Optionally `bash trial/new_project_trial.sh <empty dir> 'CLUSTER_VENV'` (SETUP.md step 8) to
   run one real job and merge; it should end with `TRIAL PASSED`.

Then follow the skill for the work itself. A run's command must solve its scenario and call
`set_as_default()` on the result: the merge brings back exactly the version the run left as
default, and a run that left none fails. Never copy, move or delete database files by hand, and
never script around a refusal (exit 3): report it.

## Commands

```
ixmp-copies init | platform-add | doctor | where
ixmp-copies backup | restore | verify | seed | stage | collect | cleanup | transfer
ixmp-copies job-copy | job-check | run-mark | job-close | merge   # inside jobs, via the templates
```

## Tests

In an environment with ixmp and message_ix:

```bash
uv run --with pytest python -m pytest
```

The integration test builds real HyperSQL databases in a temp folder and needs java; it is
skipped without it.
