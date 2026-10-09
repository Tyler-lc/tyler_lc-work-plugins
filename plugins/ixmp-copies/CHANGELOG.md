# Changelog

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

Verified: 65 tests (offline, fake `sbatch`/`ssh`, and a real-HyperSQL test with a JVM). The
new-project trial passed its local steps on 2026-10-09; its cluster steps for 0.3.0 (`submit.sh`,
`run-mark` in `job_run.do`, seed-merge resubmission) were interrupted by a VPN drop and are
still to be run.

## 0.2.0 (2026-10-08)

`submit_merges.sh`, `job_merge_from_seed.do`, `cleanup`, MIT licence. Cluster trial passed on
UniCC.

## 0.1.0 (2026-10-08)

First release: backup, seed, job copies, merge, restore, verify, transfer, stage, collect,
doctor, GAMS-source records. Cluster trial passed on UniCC.
