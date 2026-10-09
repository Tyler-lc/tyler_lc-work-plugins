# Changelog

## 0.4.0 (2026-10-09)

Fixes from a second cold review of 0.3.0. Do not run `cleanup --apply` with 0.3.0: it deletes
job copies holding run output in their code or model folders.

- **Merges are tied to the model and scenario.** A merge refuses a run record about another
  model (`--model` must match what the run marked). The merge marker is now
  `merged from <source> <model>/<scenario> v<N>`, so seed merges of several scenarios at one
  version (`SCENARIOS="a:1 b:1"`) no longer refuse each other. `submit_runs.sh` writes the model
  into each run line of the submission record (`model=...`, last before `cmd=`) and
  `submit_merges.sh` merges with it.
- **Seed merges are marked by where the versions came from:** the database the seed's backup
  copied (new seeds record it as `origin`), or the job copy a seed was made from. The same
  workstation version merged again through a newer seed is refused.
- **`run-mark` accepts a solve in place:** the same default version, unsolved before the command
  and solved after it. It still refuses the same default solved before and after (a re-solve in
  place cannot be told from a command that did nothing; clone first), and says why.
- **`cleanup` keeps run output outside the job folder's top level:** new or changed files in the
  job's code copy (against `code_files.json`, which `job_run.do` now writes right after copying
  the code; bytecode caches and collected `.json` records aside), and GDX files, listings and GAMS
  scratch in its model copy (`model/output` holds the equation duals). `--include-outputs`
  deletes them anyway. Every job that solved has GDX files, so after copying out what is needed,
  merged solves are deleted with `--include-outputs`.
- **`cleanup` deletes read jobs** (scenario `-` in a runs file: `job_run.do` writes an empty
  `expected_merges.txt`) once their records are collected and they hold no output. A job submitted
  by hand without `MERGE_SCENARIO` is still kept. New: `cleanup --area A --discard JOB --reason
  TEXT [--apply]` deletes one closed job copy whatever its merges, and records the reason; it
  refuses an open database, uncollected records and (without `--include-outputs`) run output.
- `transfer` into a platform registered but never opened (no database files yet) proceeds with
  nothing to back up, instead of refusing.
- `submit_runs.sh` reads a last line without a newline. `submit.sh` unsets the templates'
  variables before applying the given `VAR=value` pairs (an exported `BACKUP` no longer reaches a
  `SRC_JOB` seed job), and makes only `BACKUP`, `SEED`, `MAIN` and `SRC_JOB` absolute.
  `job_seed.do` refuses both `BACKUP` and `SRC_JOB`.
- `submit_merges.sh` finds the main of a batch named `main`, skips a merge whose last attempt
  exited 4 ("restore first"), and no longer exits 1 on a record without run lines.
- A command line the tool does not accept exits 3 (refused), not argparse's 2.
- `doctor` checks that the share reached here is the cluster's (top-level names of both roots)
  and that `[cluster] partition` exists (`sinfo`).
- SETUP: the H drive mounted on first access by systemd (`x-systemd.automount`, with a
  `~/hdrive` symlink), the plain mount as fallback. README: how to run the tests.

Upgrade from 0.3.0:
- Batches started under 0.2.0 have no run record (`run_result.json`): remerge them with a 0.2.0
  snapshot, or by hand with `merge --version N` after checking which version the run made.
- Versions merged under 0.3.0 carry the old marker; 0.4.0 still recognises it for the same
  scenario (on the main and in merge records), so repeats stay refused.
- Job copies made under 0.3.0 have no `code_files.json`: `cleanup` counts their code copy as
  output and keeps them until `--include-outputs`. Their read jobs have no `expected_merges.txt`:
  delete those with `--discard`.
- Submission records of 0.3.0 name no model: `submit_merges.sh` uses `MODEL` from the
  environment, else `[project] model`.

Verified: 86 tests (offline, fake `sbatch`/`sacct`/`ssh`/`sinfo`, and real-HyperSQL tests with a
JVM, one of which solves the Dantzig model in place with a local GAMS), each new test confirmed
to fail against 0.3.0. The cluster trial passed on UniCC (2026-10-09, `TRIAL PASSED`): a solve in
place of the seed's unsolved default was accepted and merged; a re-solve that forgot
`set_as_default()` was refused (`mark 3`); a seed merge's resubmission was refused by the main's
marker naming the version's origin; cleanup's first pass deleted only the merged seed copy and
the read job without output, keeping every job that held GDX, listings or a written file, and
`--include-outputs` then deleted those, keeping the failed run and the refused seed merge.

## 0.3.0 (2026-10-09)

Fixes from a cold review of 0.2.0. Upgrade from 0.2.0: in `ixmp_copies.toml`, add
`user = "<cluster account>"` under `[cluster]`, and put `"/hdrive/all_users/{cluster_user}"` first
in `[storage] roots` (or re-run `init` in a scratch folder and copy the new keys).

- A run job records its scenario's versions before its command and the version the command
  left as default (`run-mark`). The job fails when that is no new version (a re-solve that forgot
  `set_as_default()`), and the merge brings back exactly the recorded version, refusing an
  unsolved one unless `--allow-unsolved`. A merge of a job without that record needs `--version`.
- Seed merges are marked by the seed: resubmitting one cannot merge a scenario twice. A passing
  merge record refuses a repeat before any backup is taken.
- `cleanup` keeps job copies whose records are not collected or that hold other files a run
  wrote (`--include-outputs` deletes those too).
- `submit_merges.sh` reads record fields only before the command text, and no longer stops at a
  run without a scenario. `collect` updates submission records, which only grow.
- The held-open check sees writers through symlinked paths and lets jobs read one seed together.
- `doctor` also checks the cluster user, the cluster's Python (3.11+), its ixmp config file and
  `[storage] roots` there; its cluster report works with ixmp versions that return paths.
- No H drive, an unknown platform or a missing config exit 3 (refused), not with a traceback.
- Templates carry no partition: `submit.sh` submits any template with `[cluster] partition`
  and passes `VAR=value` through the environment. `backup` prints the cluster's path of the backup.
- The share is probed with a 10 s timeout. `init` requires `--venv` and records the cluster user;
  `/hdrive/all_users/<user>` is tried first.

Verified: 65 tests (offline, fake `sbatch`/`ssh`, and a real-HyperSQL test with a JVM), and the
new-project trial on UniCC (2026-10-09, `TRIAL PASSED`): `submit.sh` for seed and main; two
parallel solves merged; a re-solve that forgot `set_as_default()` failed (`mark 3`) and its merge
never ran; a cancelled merge recovered by `submit_merges.sh` and the repeat skipped; a seed merge,
and its resubmission refused by the main's marker; `cleanup` deleted the four merged job copies
and kept the two that did not merge; both seeds still verify.

## 0.2.0 (2026-10-08)

`submit_merges.sh`, `job_merge_from_seed.do`, `cleanup`, MIT licence. Cluster trial passed on
UniCC.

## 0.1.0 (2026-10-08)

First release: backup, seed, job copies, merge, restore, verify, transfer, stage, collect,
doctor, GAMS-source records. Cluster trial passed on UniCC.
