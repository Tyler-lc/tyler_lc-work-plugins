---
name: db-copies
description: >-
  Run MESSAGE-ix / ixmp work on a SLURM cluster (UniCC) against checked copies of a local
  HyperSQL ixmp database, and merge the solved scenarios back, with the `ixmp-copies` tool.
  Trigger when: a project keeps its scenarios in a local HyperSQL platform (e.g. while the
  shared Oracle database is full or slow) and wants parallel solves on the cluster; anything
  would copy, move, back up, restore or delete HyperSQL database files (db.script, db.data,
  db.properties, ...); scenarios must move between ixmp-dev and a local platform; solved runs
  from job copies must come back into a main database; a database refuses to open, looks
  corrupt, or needs a backup before a risky step; or a colleague is setting this up. Do not use
  for: what a project's runs build or solve (the project's own code), or general cluster use
  with no local database involved.
---
# Local ixmp databases on the cluster: ixmp-copies

A HyperSQL file database is a set of files (`db.properties`, `db.script`, `db.data`, `db.lobs`,
...) owned by the one process that has it open; a byte copy taken while it is open is not a
database. To solve many scenarios in parallel, each cluster job gets its own verified copy, and
solved scenarios are merged back one at a time. Every step goes through one tool that refuses
rather than guesses. **Never copy, move, edit or delete these files by hand (`cp`, `rsync`, `rm`,
backup software), and never script around a refusal.** A refusal means the state is unsafe or not
understood: report it to the user.

Tool: `ixmp-copies` (`python -m ixmp_copies`), in this plugin's `src/`. Settings: the project's
`ixmp_copies.toml`. First-time setup, for a new user or a new project: `SETUP.md` beside this
plugin's README; then `ixmp-copies doctor` says what is missing.

## The chain (one way)

```
live database --backup--> <backups>/<label>/<time>/     (only backup, merge, transfer touch a live db)
backup --seed--> <area>/seeds/<name>/                    (read-only; make it on the cluster)
seed --job-copy--> <area>/jobs/<name>_<jobid>/{db,model,ixmp,code}   (one per job)
seed --job-copy --kind main--> <area>/mains/<name>/      (a results main: merges go here)
job copy --merge--> a main                               (backs the main up first)
backup --restore--> a new local folder                   (the way back; never overwrites)
platform A --transfer--> platform B                      (e.g. ixmp-dev <-> local; registry first)
```

Areas are folders on the shared H drive named in `[storage.areas]` (`test` for trials of the
procedure, `live` for real runs). `ixmp-copies where --area A [jobs|seeds|mains|runs|code|backups]`
prints any of them as the current machine sees the share.

## Running a batch on the cluster

1. Workstation: commit, then `ixmp-copies stage --area live` (prints the snapshot path, the jobs'
   `CODE`; it holds the code, the tool, the templates and `job.env`). Untracked inputs the jobs
   need: `--extra PATH`.
2. Workstation: `ixmp-copies backup` (dry run: checks only), then `backup --apply`. Stop
   whatever has the database open first.
3. Login node, once per backup: `sbatch --export=ALL,CODE=$CODE,AREA=live,NAME=<seed>,BACKUP=<backup dir> $CODE/.ixmp_copies/slurm/job_seed.do`.
4. Login node, once per campaign: `job_make_main.do` with `SEED`, `NAME`.
5. Login node: `CODE=$CODE AREA=live bash $CODE/.ixmp_copies/slurm/submit_runs.sh SEED MAIN RUNS_FILE`.
   `RUNS_FILE` has one line per run, `NAME SCENARIO COMMAND...`: the command runs in the job's
   code copy with `IXMP_DATA` pointing at the job's database, so `ixmp.Platform()` (or the
   project's platform name) opens that copy and nothing else. The run must leave `SCENARIO`'s
   result as its default version; that version is merged. `AFTER=<jobid>` makes every run wait
   for, e.g., the seed job. Every merge into a main runs alone (`--dependency=singleton`).
6. Check each job's `Exit:` line (`grep -H Exit: <area>/runs/*.out`) and its merge record.
7. Workstation: `ixmp-copies collect --area live` brings the records home (write-once).

A chain (solve A, seed from A's closed copy, solve B..N from that seed) is `job_seed.do` with
`SRC_JOB=<A's job dir>` and `--dependency=afterok:<A>`, then `submit_runs.sh` with `AFTER=<seed job>`.

## Exit codes

0 done; 3 refused, nothing changed; 2 a copy does not match its source, `verify` found a
difference, or a merge/transfer comparison failed; 4 a merge or transfer failed after its backup
(the target may hold a partial version: the message names the backup to restore from);
1 a failed `doctor` check, or Python's own code for an uncaught error, which is a bug or an
unforeseen failure, never a refusal.

## What refuses

| Command | Refuses when |
|---|---|
| `backup [--platform P] [--apply]` | the database is not provably closed: `modified` not `no`, a `.lck`, a non-empty `.log` or `.tmp`, a local process holding a file, an unrecognised file beside the stem; the destination exists |
| `seed --from B \| --from-job D` | the backup no longer matches its manifest; not a backup; a job copy its job did not close; MEMORY tables; name exists |
| `job-copy` | `IXMP_DATA` already set; the folder exists or is not directly below `<area>/jobs/` (`mains/` for `--kind main`); the seed does not match its manifest; no message model dir |
| `job-check` | ixmp in this process does not resolve the platform to the job's copy, the model dir to the job's `model/`, or knows any other platform |
| `job-close` | the copy is not closed; `result.json` exists |
| `merge` | the job copy changed since `job-close`; the target is not closed; the same merge was made before (scenario meta `[merge] marker_key`); no model and no default version |
| `restore` | the backup does not match its manifest; the destination exists or is on the H drive |
| `transfer` | a HyperSQL target is not closed; the scenario has no default version and no `--version` |

## Rules

1. Back up before anything risky (an upgrade, a merge by hand, a long unattended run).
2. Verify a copy from the cluster (`ixmp-copies verify <path>` in a job or on the login node):
   a workstation reading the share over CIFS may read its own cache.
3. A job opens only its own copy: the templates set `IXMP_DATA` after `job-copy` and run
   `job-check` before anything opens a platform. The job's ixmp config carries no other platform
   and no password, so a job cannot write to ixmp-dev or another copy.
4. Each job runs from its own copy of the staged code; records written beside the code never
   collide between jobs. `collect` brings them home.
5. Merges: one at a time per target, a 16 GB heap in a 48 GB job (both platforms sit in one JVM:
   8 GB ran out on a full MESSAGE scenario; RSS reached 21 GB on a 300 MB database and 33.5 GB on
   a 1.1 GB one). A workstation with less free memory than that cannot host a merge.
6. On a workstation, one ixmp JVM at a time; never raise the heap past the free memory.

## Limits worth knowing

- Process detection sees the local host only. Across hosts a refusal rests on HyperSQL's own
  markers (`modified=yes`, `.lck`, the transaction log), and a process killed with `-9` leaves the
  same markers as a live one: treat a stale `.lck` as a question for the user, never delete it.
- CIFS ignores `chmod`: a seed made on a workstation is not read-only (the tool warns). Make seeds
  on the cluster with `job_seed.do`.
- Clones renumber versions: a merged scenario gets the main's next version. The merge record maps
  job version to main version. The JDBC cross-platform clone replaces the annotation with its own,
  so never identify a version by its annotation; the merge marker is scenario meta.
- A failed clone (out of heap) has been seen to roll back, but that does not prove it for every
  failure: exit 4 says the target *may* hold a partial version. The remedy is the named backup.
- Records and backups of a results main carry the main's folder name as label, those of any other
  database the platform name.
- The cluster's GAMS may differ from the workstation's: compare a re-run within tolerances, not
  for bit equality.
- `ixmp.config.get("message model dir")` exists only after `import message_ix`; any project code
  reading it must import message_ix first.

## Which GAMS source a run used

A solve runs the GAMS files in the ixmp config's `message_model_dir`, wherever the venv's Python
`message_ix` comes from. Each job copies that folder fresh (so concurrent solves never share
`cplex.opt`, GDX files or listings) and writes `<job>/model_source.json`: the source path, the git
commit of its checkout, uncommitted changes to the GAMS source, a fingerprint of the source files,
and the message_ix version label. Merge records carry it. Read the commit, not the label: an
editable install keeps the label it had when installed, so after the checkout moves the label
names old code. The fingerprint ignores run folders, solver option files, GAMS scratch and hidden
paths, and is equal on two machines exactly when their GAMS sources are byte-identical.

`ixmp-copies doctor` reports, here and on the cluster, the GAMS source in use and warns when
(a) solves use a different folder than the imported message_ix's own `model/` (Python and GAMS
from two releases), (b) the version label names another commit than the checkout, or (c) the GAMS
source has uncommitted changes. Several venvs importing one editable `message_ix` checkout share
its version: moving that checkout for one project changes every project using it.
