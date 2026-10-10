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
`ixmp_copies.toml`. First-time setup, for a new user or a new project: the README's "For an
agent installing this" (the order, and what to ask the user) and `SETUP.md` beside it; then
`ixmp-copies doctor` says what is missing, and `trial/new_project_trial.sh` runs one real job
and merge end to end.

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
prints any of them as the current machine sees the share; `where --backups` prints where backups
of the working database go (outside every area). The share is reached at the first of
`[storage] roots` that answers: at IIASA `/hdrive/all_users/<IIASA user>` on the cluster, and
wherever it is mounted on the workstation.

## Running a batch on the cluster

1. Workstation: commit, then `ixmp-copies stage --area live` (prints the snapshot path, the jobs'
   `CODE`; it holds the code, the tool, the templates and `job.env`). Untracked inputs the jobs
   need: `--extra PATH`.
2. Workstation: `ixmp-copies backup` (dry run: checks only), then `backup --apply`. Stop
   whatever has the database open first. It prints the backup's path as the cluster spells it
   (`on the cluster (BACKUP=): ...`): use that line on the login node.
3. Login node: submit single jobs with `submit.sh`, which adds `[cluster] partition` and passes
   `VAR=value` pairs through the environment (values may hold commas; options after `--` go to
   sbatch, e.g. `-- --mem=64G`):
   `CODE=$CODE bash $CODE/.ixmp_copies/slurm/submit.sh job_seed.do AREA=live NAME=<seed> BACKUP=<cluster path>`.
   Only the pairs given reach the job: the templates' variables exported in your shell are
   unset first. Relative `BACKUP`, `SEED`, `MAIN` and `SRC_JOB` are made absolute; every other
   value passes as written. `job_seed.do` refuses both `BACKUP` and `SRC_JOB`.
4. Login node, once per campaign: `submit.sh job_make_main.do AREA=live SEED=<seed folder> NAME=<main>`.
5. Login node: `CODE=$CODE AREA=live bash $CODE/.ixmp_copies/slurm/submit_runs.sh SEED MAIN RUNS_FILE`
   (relative paths are taken from the folder you call it from). `RUNS_FILE` has one line per run,
   `NAME SCENARIO COMMAND...`. The command runs in the job's code copy with `IXMP_DATA` pointing
   at the job's database, so `ixmp.Platform()` (or the project's platform name) opens that copy
   and nothing else. **The command must call `set_as_default()` on its result** (and solve it):
   ixmp makes only a new scenario name's first version default by itself, so a run that re-solves
   an existing name and forgets the call leaves the old version as default. The job records
   `SCENARIO`'s versions before the command and the default version after it (`run-mark`). It
   accepts a default version that was not there before, or the same default version solved in
   place (unsolved before, solved after) when the run made no other version. It refuses
   everything else: the same default solved both before and after (a re-solve in place cannot be
   told from a command that did nothing, so clone to a new version before solving a scenario the
   seed holds solved); the default solved in place beside a new version left non-default (a base
   solve, then a clone whose `set_as_default()` was forgotten); an older version made default. A
   refused run fails its job, and the merge brings back exactly the version the job recorded, for
   the model it recorded, refusing an unsolved one unless told (`--allow-unsolved`). A job whose
   command failed, whose run never started (`run-mark --before` failed, so the command never ran)
   or whose run was refused has no record of an accepted run, and its merge is refused (see "A
   merge by hand"). `SCENARIO` `-` means no merge (a read job). `AFTER=<jobid>` makes every run wait for, e.g., the seed job; `MODEL` sets the model
   when it is not `[project] model`. The submission record (`<area>/runs/submitted_*.txt`) gets
   each run's line as soon as it is submitted (`run NAME job=ID dir=DIR [scenario=S] model=M
   cmd=...`) and its merge's on a line of its own (`merge NAME merge=ID`): a merge whose submission
   failed leaves its run recorded, the batch stops there, and `submit_merges.sh` submits that
   merge. The runs after it go in a new runs file of another name: a runs file of the same name
   (whatever its extension) is refused as already submitted into that main. Every merge into a
   main runs alone (`--dependency=singleton`).
6. Check each job's `Exit:` line (`grep -H Exit: <area>/runs/*.out`: run, mark, close and seed
   all 0) and its merge record.
7. Workstation: `ixmp-copies collect --area live` brings the records home (json records are
   write-once; submission records, which only grow, are updated).

A chain (solve A, seed from A's closed copy, solve B..N from that seed) is `submit.sh job_seed.do
AREA=live NAME=<seed> SRC_JOB=<A's job dir> -- --dependency=afterok:<A>`, then `submit_runs.sh` with
`AFTER=<seed job>`.

## When merges did not happen

- **A merge failed or was cancelled** (a bug since fixed, a lost node): from the login node,
  `CODE=<snapshot> bash $CODE/.ixmp_copies/slurm/submit_merges.sh <area>/runs/submitted_*.txt`.
  It reads the batch's submission record and resubmits only what is missing: it skips runs whose
  latest merge completed or is still queued, merges finished runs now and still-running ones
  after them, and skips failed runs. `CODE` may be a newer snapshot with the fix. Safe to
  repeat: the tool refuses a merge already made.
- **A merge exited 4** (failed after its pre-merge backup): `submit_merges.sh` skips it until
  told. The merge log's `FAILED:` line says which of two cases it is.
  - `merged and recorded (<record>)`: the clone landed, its marker and its merge record are
    written (a resubmission is refused), but the main was left not shut down cleanly. Nothing
    merges into it until it is: find what holds it, and take a stale `.lck` to the user.
  - `MERGE: ...`: the failure came before the merge record, and the main may hold what the merge
    made. Check the main first (`job_backup_main.do` and a `restore` on the workstation, or a read
    job on a seed made from a backup of the main). A version of the scenario carrying that merge's
    marker (scenario meta `[merge] marker_key`; the message quotes it) is the merge, landed
    without its record: check it by hand; a resubmission is refused by the marker. A version newer
    than every version of the pre-merge backup without the marker is what the clone left:
    half-made, or complete if only setting the marker failed. Inspect it. Only when no complete
    copy is there, resubmit with `FORCE_RUNS="NAME ..." CODE=... bash .../submit_merges.sh
    <record>` (the runs' names), which adds another version beside it. Note the number of a
    half-made version: it stays.

  The tool does not restore a main in place, and its database files are never swapped by hand:
  records of merges made since would outlive the swap. The backup the merge's log names is a copy
  to read, or to restore elsewhere.
- **Scenarios exist only in a seed** (built on the workstation after the main was made): back up,
  seed, then `submit.sh job_merge_from_seed.do AREA=live SEED=... MAIN=... SCENARIOS="name name:version ..."
  -- --job-name=merge_into_<main> --dependency=singleton` (no version: the seed's default). Its
  merges are marked by the database the seed's versions came from (the database that was backed
  up, or the job copy a seed was made from) and by model, scenario and version: after a partial
  failure resubmit the same `SCENARIOS`, and what merged before is refused, the rest merges; the
  same version brought through a newer seed of the same database is refused too. The marker names
  a version number, not its content: a version solved or edited in place on the workstation after
  its merge (or a database recreated since) keeps its number and is refused as merged. Clone it to
  a new version there, back up, seed, and merge that.
- **A merge by hand** of a job without run records names the version: `merge ... --version N`, or
  `--version default` for the copy's default version. A job whose run did not complete
  (`run_before.json` without `run_result.json`: its command failed, or `run-mark --after` never
  ran), never started (`expected_merges.txt` names a scenario and there is no `run_before.json`:
  `run-mark --before` failed; a seed merge's job aside) or was refused (`run_result.json` refuses
  it; a record an earlier release accepted is judged by this release's rule too) is refused
  whatever the version. After checking a version by hand, `--version N --despite-failed-run`
  merges it, and the merge record says so (`despite_failed_run`, and the run's record as it
  was).

## Getting results out

Everything is read from the results main, never job by job: each job copy only feeds its merge.

- **On the workstation, one database:** on the cluster, `submit.sh job_backup_main.do AREA=live NAME=<main>`; then
  `ixmp-copies restore --from <that backup> --dest ~/ixmp_local/<name> --apply` and
  `ixmp-copies platform-add --name <name> --dir ~/ixmp_local/<name> --apply`. Read or report the
  runs one per process from that platform. What you write there (e.g. reported timeseries)
  exists only in that copy.
- **On the cluster, in parallel:** seed from a backup of the main, then `submit_runs.sh` with read
  commands and scenario `-` (no merge). Each job reads its own copy; have the commands write
  `.json` records into the project's records folder in the job's code copy (`collect` brings them
  home) or other files into the code copy or `$IXC_JOB_DIR`. `cleanup` keeps a job holding such
  files until you have copied them out and pass `--include-outputs`.
- **Equation duals and other GDX output** of a solve stay in the job copy's `model/output/` (and
  `model/data/`); the merge brings back the scenario, not the GDX. Copy them out before cleanup.

## Cleaning up

Job copies are full databases and stay on the H drive until deleted. Run `collect` first, after
the batch's jobs have finished. `ixmp-copies cleanup --area A --main NAME` lists each job copy
with a verdict; `--apply` deletes those a merge record proves merged into `<area>/mains/NAME` with
its comparison passing, and only when every record for that job passed, every scenario the job
was meant to merge (`expected_merges.txt`, written by `job_run.do` from `submit_runs.sh`'s
scenario and by `job_merge_from_seed.do`) has one, the job's own `.json` records are collected
(byte for byte), and the job holds no output a run wrote. A read job (scenario `-`: an empty
`expected_merges.txt`) needs no merge record. Output a run wrote is:
- any file in the job folder besides the tool's own;
- a new or changed file in the job's code copy, against `code_files.json` (written by
  `job_run.do` right after copying the code; Python's `__pycache__` and the `.json` records,
  which `collect` brings home, aside). A code copy without `code_files.json` (a job of 0.3.0 or
  before) counts as output as a whole: its run's files cannot be told from the staged code;
- GDX files (`model/data`, and `model/output`, which holds the equation duals), listings and
  GAMS scratch (`225*`) in the job's model copy. Every job that solved has these.

So copy out what is needed, then `cleanup ... --include-outputs --apply` deletes merged job
copies whatever they hold. Kept: open or failed jobs, jobs holding a merge's copy of their
database (`merge_src_*`) that is not closed (a merge running, on any host, or one that died),
jobs without a record (a job submitted by hand without `MERGE_SCENARIO` included), jobs whose
merge failed or went elsewhere. New files and files of another size in a code copy count as
output without being read; files of the recorded size are hashed. Merge records
are read from the area's snapshots and from the project's records folder.

One job copy the user decided is not needed (a failed run, a merge that will never be made):
`ixmp-copies cleanup --area A --discard JOB --reason "<why>"` (dry run), then with `--apply`.
It deletes that copy whatever its merges, once its job closed it, its records are collected and
it holds no output (unless `--include-outputs`), and writes a cleanup record with the reason. It
refuses a copy whose database, or a merge's copy of it (`merge_src_*`), is not closed: a job killed mid-solve leaves the same markers as a
running one, so that is a question for the user. Seeds, mains and backups are never touched: old
seeds and pre-merge backups stay until the user decides to delete them.

## Exit codes

0 done; 3 refused, nothing changed (including: no H drive reachable, an unknown platform, no
project or ixmp config, a command line the tool does not accept); 2 a copy does not match its source, `verify` found a difference, a
merge/transfer comparison failed, or `collect` met a record it will not overwrite; 4 a merge or
transfer failed after its backup, or left its target not shut down cleanly (the message says what
to check and names the backup taken before it; for a results main, see "A merge exited 4"
above); 1 a failed `doctor` check, or Python's own code for an uncaught
error, which is a bug or an unforeseen failure, never a refusal. In job scripts the `Exit:` line
lists each step's code.

## What refuses

| Command | Refuses when |
|---|---|
| `backup [--platform P] [--apply]` | the database is not provably closed: `modified` not `no`, a `.lck`, a non-empty `.log` or `.tmp`, a local process holding a file open for writing, an unrecognised file beside the stem; the destination exists |
| `seed --from B \| --from-job D` | the backup no longer matches its manifest; not a backup; a job copy its job did not close; MEMORY tables; name exists |
| `job-copy` | `IXMP_DATA` already set; this account has no ixmp config file; the folder exists or is not directly below `<area>/jobs/` (`mains/` for `--kind main`); the seed does not match its manifest; no message model dir |
| `job-check` | ixmp in this process does not resolve the platform to the job's copy, the model dir to the job's `model/`, or knows any other platform |
| `job-close` | the copy is not closed; `result.json` exists |
| `run-mark --after` | the default version is neither new nor the default solved in place (unsolved before, solved after, no other version made): no default, an older version made default, still unsolved, solved before and after, or solved in place beside a new version left non-default |
| `merge` | the job copy changed since `job-close`; the run did not complete (`run_before.json` without `run_result.json`), never started (`expected_merges.txt` names a scenario, no `run_before.json`, not a seed merge) or was refused (by `run-mark`, or by this release's rule for a record an earlier release accepted), unless `--version N --despite-failed-run`; `--despite-failed-run` on any other job; the run's record shows an unsolved version (without `--allow-unsolved`), another model or scenario, or another version than `--version`; no run record and no `--version`; a passing merge record of the same model and scenario with the same marker exists (checked before the backup); the target is not closed; the same merge was made before (scenario meta `[merge] marker_key`: `merged from <source> <model>/<scenario> v<N>`, the source being the job copy, or for a seed merge the database the seed's versions came from); no model |
| `cleanup` | (keeps, with the reason) open jobs, a `merge_src_*` copy not closed, uncollected records, run outputs (job folder, code copy, GDX and listings in the model copy), missing or failed merges; no `--main` and no `--discard` |
| `cleanup --discard` | no `--reason`; with `--main`; no such job copy; not closed by its job, or its database or a `merge_src_*` copy not closed; uncollected records; run outputs (without `--include-outputs`) |
| `job_seed.do` | both `BACKUP` and `SRC_JOB` set |
| `restore` | the backup does not match its manifest; the destination exists or is on the H drive |
| `transfer` | a HyperSQL target is not closed (a target whose database files do not exist yet, in an existing empty folder, is new: nothing to back up); a target folder that is missing (a mistyped url, a disk not mounted: run `platform-add`, which creates it, or create it) or holds other files; the scenario has no default version and no `--version` |

## Rules

1. Back up before anything risky (an upgrade, a merge by hand, a long unattended run).
2. Verify a copy from the cluster (`ixmp-copies verify <path>` in a job or on the login node):
   a workstation reading the share over CIFS may read its own cache.
3. A job opens only its own copy by name: the templates set `IXMP_DATA` after `job-copy` and run
   `job-check` before anything opens a platform, and the job's ixmp config carries no other
   platform and no password. Code that builds a database URL itself can still open another file
   database; the job's ixmp config is a guard against mistakes, not a sandbox.
4. Each job runs from its own copy of the staged code; records written beside the code never
   collide between jobs. `collect` brings them home.
5. Merges: one at a time per target, a 16 GB heap in a 48 GB job (both platforms sit in one JVM:
   8 GB ran out on a full MESSAGE scenario; RSS reached 21 GB on a 300 MB database and 33.5 GB on
   a 1.1 GB one). A workstation with less free memory than that cannot host a merge.
6. On a workstation, one ixmp JVM at a time; never raise the heap past the free memory.

## Limits worth knowing

- Process detection sees the local host only, and counts processes holding a file open for
  writing (readers, e.g. two jobs copying one seed, are no risk). Across hosts a refusal rests on
  HyperSQL's own markers (`modified=yes`, `.lck`, the transaction log), and a process killed with
  `-9` leaves the same markers as a live one: treat a stale `.lck` as a question for the user,
  never delete it.
- A share that does not answer within 10 seconds counts as unreachable (the mount hangs while
  the VPN is down). On WSL an unmounted mount point is an empty folder that `ls` lists without
  complaint; the tool requires the folder to hold something.
- CIFS ignores `chmod`: a seed made on a workstation is not read-only (the tool warns). Make seeds
  on the cluster with `job_seed.do`.
- Clones renumber versions: a merged scenario gets the main's next version. The merge record maps
  job version to main version. The JDBC cross-platform clone replaces the annotation with its own,
  so never identify a version by its annotation; the merge marker is scenario meta.
- A failed clone (out of heap) has been seen to roll back, but that does not prove it for every
  failure: exit 4 says the target *may* hold a partial version. For a local database, the remedy
  is the named backup, restored to a new folder; a results main is checked and resubmitted as in
  "A merge exited 4", never restored in place.
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
