#!/bin/bash
# End-to-end acceptance trial of a new project's setup, on the real cluster.
#
#   bash trial/new_project_trial.sh WORKDIR CLUSTER_VENV
#
# Run from a shell whose `python` has ixmp, message_ix and pytest (message_ix's Dantzig test
# model needs pytest to import), with the VPN up, the H drive mounted and `ssh <host>` open.
# It builds a throwaway project in WORKDIR (a git repo with its own ixmp config, so the user's
# ixmp config is never touched), with a local platform holding message_ix's Dantzig model,
# and goes through everything a new project does: init, platform-add, doctor, backup, stage;
# on the cluster a seed, a results main, two solves in parallel (one re-solving `standard`, one
# making `standard_b`), each on its own copy, and their merges; a third solve whose merge is
# cancelled and recovered with submit_merges.sh; a scenario made on the workstation afterwards,
# merged from a newer seed with job_merge_from_seed.do; then collect, and cleanup of the merged
# job copies. It writes on the H drive only below ixmp_copies/trial_<time>/. Exit 0 when every
# step and check passed.
set -euo pipefail
WORK="${1:?WORKDIR (new or empty)}"; VENV="${2:?CLUSTER_VENV, e.g. ~/repos/.venv}"
PLUGIN="$(cd "$(dirname "$0")/.." && pwd)"
STAMP=$(date +%Y%m%d_%H%M%S); NAME="trial_$STAMP"; PLATFORM="trial-$STAMP-local"
MODEL="Canning problem (MESSAGE scheme)"
[ ! -e "$WORK" ] || [ -z "$(ls -A "$WORK")" ] || { echo "$WORK is not empty" >&2; exit 1; }
mkdir -p "$WORK/$NAME" "$WORK/ixmp_home"
PROJECT="$WORK/$NAME"
export IXMP_DATA="$WORK/ixmp_home" PYTHONPATH="$PLUGIN/src" JAVA_TOOL_OPTIONS=-Xmx2g
echo '{"platform": {}}' > "$IXMP_DATA/config.json"
ixc() { python -u -m ixmp_copies "$@"; }
say() { echo; echo "##### $*"; }

say "a new project with a solve script and a runs file"
cd "$PROJECT"
git init -q
cat > solve.py <<'EOF'
"""Solve the Dantzig model on the job's own copy, as TARGET (re-solving `standard` makes a new version)."""
import sys

import ixmp
import message_ix

model, target = "Canning problem (MESSAGE scheme)", sys.argv[1]
mp = ixmp.Platform()
base = message_ix.Scenario(mp, model, "standard")
scen = base.clone(scenario=target, keep_solution=False)
scen.solve(quiet=True)
scen.set_as_default()
print("solved", target, "version", scen.version, "OBJ", scen.var("OBJ")["lvl"])
mp.close_db()
EOF
cat > runs.txt <<'EOF'
# NAME SCENARIO COMMAND
solve_a standard python solve.py standard
solve_b standard_b python solve.py standard_b
EOF
cat > runs_late.txt <<'EOF'
solve_c standard_c python solve.py standard_c
EOF
ixc init --name "$NAME" --platform "$PLATFORM" --model "$MODEL" --venv "$VENV"
# Small model, small jobs: the heaps come from the config, the job sizes from RUN_OPTS below.
sed -i 's/^java_heap_run = .*/java_heap_run = "4g"/; s/^java_heap_merge = .*/java_heap_merge = "4g"/' ixmp_copies.toml

say "platform-add, then the Dantzig model in it"
ixc platform-add --dir "$WORK/db" --apply
python - "$PLATFORM" <<'EOF'
import sys

import ixmp
from message_ix.testing import make_dantzig

mp = ixmp.Platform(sys.argv[1])
make_dantzig(mp).set_as_default()
mp.close_db()
EOF
git add -A && git -c user.name=trial -c user.email=trial@localhost commit -qm "trial project"

say "doctor"
ixc doctor

say "backup and stage"
ixc backup --apply
LOCAL_ROOT=$(python -c "from ixmp_copies import config, copies; print(copies.hdrive_root(config.load()))" | tail -n 1)
BACKUP=$(ls -d "$LOCAL_ROOT/ixmp_copies/$NAME/backups/$PLATFORM"/*/ | tail -n 1)
CODE=$(ixc stage --area test | tail -n 1)
HOST=$(python -c "from ixmp_copies import config; print(config.load().ssh_host)" | tail -n 1)
REMOTE_ROOT="${CODE%%/ixmp_copies/*}"
REMOTE_BACKUP="$REMOTE_ROOT/${BACKUP#"$LOCAL_ROOT"/}"
AREA="$REMOTE_ROOT/ixmp_copies/$NAME/test"
echo "code $CODE; backup $REMOTE_BACKUP; area $AREA"

remote() { ssh -o BatchMode=yes "$HOST" "bash -lc $(printf %q "$1")"; }
wait_for() {  # job ids: until none is pending or running, then fail unless all COMPLETED
    while remote "squeue -h -j $1 2>/dev/null" | grep -q .; do sleep 20; done
    local states; states=$(remote "sacct -n -X -P -o JobID,State -j $1")
    echo "$states"
    ! echo "$states" | grep -qv "|COMPLETED"
}
SMALL="--mem=8G --cpus-per-task=2 --time=00:30:00"

say "seed, then a results main, on the cluster"
mkdir_runs="mkdir -p $AREA/runs && cd $AREA/runs"
seed=$(remote "$mkdir_runs && CODE=$CODE AREA=test NAME=seed1 BACKUP=$REMOTE_BACKUP sbatch --parsable --export=ALL $CODE/.ixmp_copies/slurm/job_seed.do")
wait_for "$seed"
main=$(remote "$mkdir_runs && CODE=$CODE AREA=test SEED=$AREA/seeds/seed1 NAME=results sbatch --parsable --export=ALL $CODE/.ixmp_copies/slurm/job_make_main.do")
wait_for "$main"

say "two solves in parallel, each on its own copy, and their merges"
out=$(remote "cd $CODE && CODE=$CODE AREA=test RUN_OPTS='$SMALL' MERGE_OPTS='$SMALL' bash $CODE/.ixmp_copies/slurm/submit_runs.sh $AREA/seeds/seed1 $AREA/mains/results $CODE/runs.txt")
echo "$out"
ids=$(echo "$out" | grep -o -E '(job|merge)=[0-9]+' | cut -d= -f2 | paste -sd,)
wait_for "$ids"
remote "grep -H '^Exit:' $AREA/runs/*.out"

say "a third solve whose merge is cancelled, recovered with submit_merges.sh"
late=$(remote "cd $CODE && CODE=$CODE AREA=test RUN_OPTS='$SMALL' MERGE_OPTS='$SMALL' bash $CODE/.ixmp_copies/slurm/submit_runs.sh $AREA/seeds/seed1 $AREA/mains/results $CODE/runs_late.txt")
echo "$late"
run_c=$(echo "$late" | grep -o 'job=[0-9]*' | head -1 | cut -d= -f2)
merge_c=$(echo "$late" | grep -o 'merge=[0-9]*' | head -1 | cut -d= -f2)
record_c=$(echo "$late" | grep -o 'record [^;]*' | cut -d' ' -f2)
remote "scancel $merge_c"
wait_for "$run_c"
again=$(remote "cd $AREA/runs && CODE=$CODE MERGE_OPTS='$SMALL' bash $CODE/.ixmp_copies/slurm/submit_merges.sh $record_c")
echo "$again"
remerge=$(echo "$again" | grep -o 'remerge solve_c run=[0-9]* merge=[0-9]*' | grep -o 'merge=[0-9]*' | cut -d= -f2)
[ -n "$remerge" ] || { echo "submit_merges.sh resubmitted nothing" >&2; exit 1; }
wait_for "$remerge"
repeat=$(remote "cd $AREA/runs && CODE=$CODE bash $CODE/.ixmp_copies/slurm/submit_merges.sh $record_c")
echo "$repeat"
echo "$repeat" | grep -q "skip solve_c: merge $remerge is COMPLETED" || { echo "a repeat resubmitted again" >&2; exit 1; }

say "a scenario made on the workstation after the main, merged from a newer seed"
python - "$PLATFORM" <<'EOF'
import sys

import ixmp
import message_ix

mp = ixmp.Platform(sys.argv[1])
message_ix.Scenario(mp, "Canning problem (MESSAGE scheme)", "standard").clone(scenario="standard_extra").set_as_default()
mp.close_db()
EOF
ixc backup --apply
BACKUP2=$(ls -d "$LOCAL_ROOT/ixmp_copies/$NAME/backups/$PLATFORM"/*/ | tail -n 1)
seed2=$(remote "$mkdir_runs && CODE=$CODE AREA=test NAME=seed2 BACKUP=$REMOTE_ROOT/${BACKUP2#"$LOCAL_ROOT"/} sbatch --parsable --export=ALL $CODE/.ixmp_copies/slurm/job_seed.do")
wait_for "$seed2"
from_seed=$(remote "$mkdir_runs && CODE=$CODE AREA=test SEED=$AREA/seeds/seed2 MAIN=$AREA/mains/results SCENARIOS=standard_extra sbatch --parsable --export=ALL $SMALL --job-name=merge_into_results --dependency=singleton $CODE/.ixmp_copies/slurm/job_merge_from_seed.do")
wait_for "$from_seed"

say "collect, and check the records"
ixc collect --area test
python - "$PROJECT/ixmp_copies_records" <<'EOF'
import json
import sys
from pathlib import Path

records = sorted(Path(sys.argv[1]).glob("merge_results_*.json"))
by_scenario = {json.loads(p.read_text())["scenario"]: json.loads(p.read_text()) for p in records}
assert len(records) == 4 and sorted(by_scenario) == ["standard", "standard_b", "standard_c", "standard_extra"], records
for name, r in by_scenario.items():
    solved = name != "standard_extra"  # cloned on the workstation from the unsolved model
    assert r["compare"]["ok"] and r["set_default"] and r["compare"]["solved"] == [solved, solved], name
    assert r["model_source"]["fingerprint"], name
    print(f"{name}: job v{r['source_version']} -> main v{r['merged_version']}, OBJ {r['compare']['OBJ'][1]}, "
          f"GAMS {str(r['model_source']['git_commit'])[:10]} {r['model_source']['fingerprint'][:12]}")
EOF

say "cleanup of the merged job copies"
ixc cleanup --area test --main results
ixc cleanup --area test --main results --apply
LOCAL_AREA="$LOCAL_ROOT/ixmp_copies/$NAME/test"
left=$(ls -A "$LOCAL_AREA/jobs")
[ -z "$left" ] || { echo "job copies left after cleanup: $left" >&2; exit 1; }
ixc verify "$LOCAL_AREA/seeds/seed1"
ixc verify "$LOCAL_AREA/seeds/seed2"
say "TRIAL PASSED: project $PROJECT, H-drive area $AREA"
