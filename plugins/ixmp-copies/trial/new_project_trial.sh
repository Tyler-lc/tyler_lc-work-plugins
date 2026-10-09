#!/bin/bash
# End-to-end acceptance trial of a new project's setup, on the real cluster.
#
#   bash trial/new_project_trial.sh WORKDIR CLUSTER_VENV
#
# Run from a shell whose `python` has ixmp, message_ix and pytest (message_ix's Dantzig test
# model needs pytest to import), with the VPN up, the H drive mounted and `ssh <host>` open.
# It builds a throwaway project in WORKDIR (a git repo with its own ixmp config, so the user's
# ixmp config is never touched), with a local platform holding message_ix's Dantzig model, and
# goes through what a new project does: init (asking ssh for the cluster user), platform-add,
# doctor, backup, stage; then on the cluster, through submit.sh and submit_runs.sh:
#   - a seed and a results main;
#   - two solves in parallel, each on its own copy, and their merges;
#   - a solve that forgets set_as_default(): its job fails and its merge never runs;
#   - a solve of the seed's unsolved `standard` v1 in place, then a clone to v2 solved without
#     set_as_default(): its job fails too (v1 is solved, but the run's result is v2);
#   - a solve in place of the seed's unsolved `standard` (no clone): accepted and merged;
#   - a third solve whose merge is cancelled and recovered with submit_merges.sh (the record's
#     model, which holds spaces, reaches the merge);
#   - a scenario made on the workstation afterwards, merged from a newer seed, and that seed
#     merge resubmitted, which must merge nothing twice;
#   - two read jobs (no merge), one writing a file into its code copy;
#   - collect, and cleanup in two passes: first only the job copies holding no run output (the
#     read job without a file, the seed merge's copy), then with --include-outputs the merged
#     solves (GDX files) and the read job's file; the two failed runs and the refused seed merge
#     stay.
# It writes on the H drive only below ixmp_copies/trial_<time>/. Exit 0 when every step and
# check passed.
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

say "a new project with a solve script and runs files"
cd "$PROJECT"
git init -q
cat > solve.py <<'EOF'
"""Solve the Dantzig model on the job's own copy as TARGET (re-solving `standard` makes a new
version) and make it the default, unless --no-default (the mistake run-mark must catch). With
--in-place, solve TARGET's default version itself, no clone (the seed holds it unsolved). With
--base-solve, solve `standard`'s default version in place first, then clone it."""
import sys

import ixmp
import message_ix

model, target = "Canning problem (MESSAGE scheme)", sys.argv[1]
mp = ixmp.Platform()
if "--in-place" in sys.argv:
    scen = message_ix.Scenario(mp, model, target)
else:
    base = message_ix.Scenario(mp, model, "standard")
    if "--base-solve" in sys.argv:
        base.solve(quiet=True)
    scen = base.clone(scenario=target, keep_solution=False)
scen.solve(quiet=True)
if "--no-default" not in sys.argv:
    scen.set_as_default()
print("solved", target, "version", scen.version, "OBJ", scen.var("OBJ")["lvl"])
mp.close_db()
EOF
cat > read.py <<'EOF'
"""Read the Dantzig model on the job's own copy; with --write FILE, also write FILE into the
working folder, the job's code copy: a run output cleanup must keep until --include-outputs."""
import sys

import ixmp
import message_ix

mp = ixmp.Platform()
scen = message_ix.Scenario(mp, "Canning problem (MESSAGE scheme)", "standard")
print("read standard version", scen.version, "solved", scen.has_solution())
mp.close_db()
if "--write" in sys.argv:
    with open(sys.argv[sys.argv.index("--write") + 1], "w") as f:
        f.write(f"standard v{scen.version}\n")
EOF
cat > runs.txt <<'EOF'
# NAME SCENARIO COMMAND
solve_a standard python solve.py standard
solve_b standard_b python solve.py standard_b
EOF
# Re-solving an existing name without set_as_default() leaves the old version as default: the
# trap. (A new name's first version is made default by ixmp itself.)
echo "solve_f standard python solve.py standard --no-default" > runs_forgot.txt
# The seed's default v1 solved in place on the way, then v2 made and solved without set_as_default():
# after the run v1 is default and solved, but it is not the run's result.
echo "solve_g standard python solve.py standard --base-solve --no-default" > runs_base.txt
echo "solve_c standard_c python solve.py standard_c" > runs_late.txt
echo "solve_i standard python solve.py standard --in-place" > runs_inplace.txt
printf 'read_n - python read.py\nread_o - python read.py --write read_output.txt\n' > runs_read.txt
ixc init --name "$NAME" --platform "$PLATFORM" --model "$MODEL" --venv "$VENV"
# Small model, small jobs: the heaps come from the config, the job sizes from the sbatch options below.
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
backup_out=$(ixc backup --apply)
echo "$backup_out"
REMOTE_BACKUP=$(echo "$backup_out" | sed -n 's/^on the cluster (BACKUP=): //p')
[ -n "$REMOTE_BACKUP" ] || { echo "backup printed no cluster path" >&2; exit 1; }
CODE=$(ixc stage --area test | tail -n 1)
HOST=$(python -c "from ixmp_copies import config; print(config.load().ssh_host)" | tail -n 1)
LOCAL_ROOT=$(python -c "from ixmp_copies import config, copies; print(copies.hdrive_root(config.load()))" | tail -n 1)
REMOTE_ROOT="${CODE%%/ixmp_copies/*}"
AREA="$REMOTE_ROOT/ixmp_copies/$NAME/test"
echo "code $CODE; backup $REMOTE_BACKUP; area $AREA"

remote() { ssh -o BatchMode=yes "$HOST" "bash -lc $(printf %q "$1")"; }
states() { remote "sacct -n -X -P -o JobID,State -j $1"; }
wait_done() { while remote "squeue -h -j $1 2>/dev/null" | grep -q .; do sleep 20; done; }
wait_for() {  # job ids: until none is pending or running, then fail unless all COMPLETED
    wait_done "$1"
    local s; s=$(states "$1"); echo "$s"
    ! echo "$s" | grep -qv "|COMPLETED"
}
SMALL="--mem=8G --cpus-per-task=2 --time=00:30:00"
SUBMIT="CODE=$CODE bash $CODE/.ixmp_copies/slurm/submit.sh"
RUNS="CODE=$CODE AREA=test RUN_OPTS='$SMALL' MERGE_OPTS='$SMALL' bash $CODE/.ixmp_copies/slurm/submit_runs.sh"
MAIN="$AREA/mains/results"

say "seed, then a results main, on the cluster (submit.sh)"
seed=$(remote "$SUBMIT job_seed.do AREA=test NAME=seed1 BACKUP=$REMOTE_BACKUP -- $SMALL")
wait_for "$seed"
main=$(remote "$SUBMIT job_make_main.do AREA=test SEED=$AREA/seeds/seed1 NAME=results -- $SMALL")
wait_for "$main"

say "two solves in parallel, each on its own copy, and their merges"
out=$(remote "cd $CODE && $RUNS $AREA/seeds/seed1 $MAIN runs.txt")
echo "$out"
wait_for "$(echo "$out" | sed 's/ cmd=.*//' | grep -o -E ' (job|merge)=[0-9]+' | cut -d= -f2 | paste -sd,)"
remote "grep -H '^Exit:' $AREA/runs/solve_*.out"
[ "$(remote "grep -l '^Exit: run 0 mark 0 close 0 seed 0' $AREA/runs/solve_*.out | wc -l")" = 2 ]

say "a solve that forgets set_as_default(): its job fails, its merge never runs"
forgot=$(remote "cd $CODE && $RUNS $AREA/seeds/seed1 $MAIN runs_forgot.txt")
echo "$forgot"
run_f=$(echo "$forgot" | sed 's/ cmd=.*//' | grep -o ' job=[0-9]*' | head -1 | cut -d= -f2)
merge_f=$(echo "$forgot" | sed 's/ cmd=.*//' | grep -o ' merge=[0-9]*' | head -1 | cut -d= -f2)
wait_done "$run_f,$merge_f"
states_f=$(states "$run_f,$merge_f"); echo "$states_f"
echo "$states_f" | grep -q "^$run_f|FAILED" || { echo "the run that forgot set_as_default did not fail" >&2; exit 1; }
echo "$states_f" | grep -q "^$merge_f|CANCELLED" || { echo "its merge was not cancelled" >&2; exit 1; }
remote "grep -h '^Exit: run 0 mark 3 ' $AREA/runs/solve_f_$run_f.out"

say "a base solve in place, then a new version solved without set_as_default(): refused too"
based=$(remote "cd $CODE && $RUNS $AREA/seeds/seed1 $MAIN runs_base.txt")
echo "$based"
run_g=$(echo "$based" | sed 's/ cmd=.*//' | grep -o ' job=[0-9]*' | head -1 | cut -d= -f2)
merge_g=$(echo "$based" | sed 's/ cmd=.*//' | grep -o ' merge=[0-9]*' | head -1 | cut -d= -f2)
wait_done "$run_g,$merge_g"
states_g=$(states "$run_g,$merge_g"); echo "$states_g"
echo "$states_g" | grep -q "^$run_g|FAILED" || { echo "the run that left its new version non-default did not fail" >&2; exit 1; }
echo "$states_g" | grep -q "^$merge_g|CANCELLED" || { echo "its merge was not cancelled" >&2; exit 1; }
remote "grep -h '^Exit: run 0 mark 3 ' $AREA/runs/solve_g_$run_g.out"
remote "grep -h 'a version the run made is not default' $AREA/runs/solve_g_$run_g.out"

say "a solve in place of the seed's unsolved default version: accepted, merged"
inplace=$(remote "cd $CODE && $RUNS $AREA/seeds/seed1 $MAIN runs_inplace.txt")
echo "$inplace"
run_i=$(echo "$inplace" | sed 's/ cmd=.*//' | grep -o ' job=[0-9]*' | head -1 | cut -d= -f2)
wait_for "$(echo "$inplace" | sed 's/ cmd=.*//; s/ model=.*//' | grep -o -E ' (job|merge)=[0-9]+' | cut -d= -f2 | paste -sd,)"
remote "grep -h '^Exit: run 0 mark 0 close 0 seed 0' $AREA/runs/solve_i_$run_i.out"
remote "grep -h 'solved in place' $AREA/runs/solve_i_$run_i.out"

say "a third solve whose merge is cancelled, recovered with submit_merges.sh"
late=$(remote "cd $CODE && $RUNS $AREA/seeds/seed1 $MAIN runs_late.txt")
echo "$late"
run_c=$(echo "$late" | sed 's/ cmd=.*//' | grep -o ' job=[0-9]*' | head -1 | cut -d= -f2)
merge_c=$(echo "$late" | sed 's/ cmd=.*//' | grep -o ' merge=[0-9]*' | head -1 | cut -d= -f2)
record_c=$(echo "$late" | grep -o 'record [^;]*' | cut -d' ' -f2)
echo "$late" | grep -qF " model=$MODEL cmd=" || { echo "the record names no model" >&2; exit 1; }
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

say "a scenario made on the workstation after the main, merged from a newer seed, twice"
python - "$PLATFORM" <<'EOF'
import sys

import ixmp
import message_ix

mp = ixmp.Platform(sys.argv[1])
message_ix.Scenario(mp, "Canning problem (MESSAGE scheme)", "standard").clone(scenario="standard_extra").set_as_default()
mp.close_db()
EOF
backup_out=$(ixc backup --apply)
REMOTE_BACKUP2=$(echo "$backup_out" | sed -n 's/^on the cluster (BACKUP=): //p')
seed2=$(remote "$SUBMIT job_seed.do AREA=test NAME=seed2 BACKUP=$REMOTE_BACKUP2 -- $SMALL")
wait_for "$seed2"
from_seed=$(remote "$SUBMIT job_merge_from_seed.do AREA=test SEED=$AREA/seeds/seed2 MAIN=$MAIN SCENARIOS=standard_extra -- $SMALL --job-name=merge_into_results --dependency=singleton")
wait_for "$from_seed"
resubmitted=$(remote "$SUBMIT job_merge_from_seed.do AREA=test SEED=$AREA/seeds/seed2 MAIN=$MAIN SCENARIOS=standard_extra -- $SMALL --job-name=merge_into_results --dependency=singleton")
wait_done "$resubmitted"
# The log is named after the job name given above. Refused either by an earlier merge record or,
# for a seed's default version (known only once the copy is open), by the main's own marker.
log_r="$AREA/runs/merge_into_results_$resubmitted.out"
remote "grep -h 'was merged into\|already merged\|^Exit:' $log_r"
remote "grep -q 'was merged into\|already merged' $log_r"

say "two read jobs (no merge), one writing a file into its code copy"
reads=$(remote "cd $CODE && $RUNS $AREA/seeds/seed1 $MAIN runs_read.txt")
echo "$reads"
run_n=$(echo "$reads" | grep '^run read_n ' | grep -o ' job=[0-9]*' | cut -d= -f2)
run_o=$(echo "$reads" | grep '^run read_o ' | grep -o ' job=[0-9]*' | cut -d= -f2)
wait_for "$run_n,$run_o"
remote "test -f $AREA/jobs/read_o_$run_o/code/read_output.txt"

say "collect, and check the records"
ixc collect --area test
python - "$PROJECT/ixmp_copies_records" <<'EOF'
import json
import sys
from pathlib import Path

records = [json.loads(p.read_text()) for p in sorted(Path(sys.argv[1]).glob("merge_results_*.json"))]
names = sorted(r["scenario"] for r in records)
assert names == ["standard", "standard", "standard_b", "standard_c", "standard_extra"], names
for r in records:
    name = r["scenario"]
    solved = name != "standard_extra"  # cloned on the workstation from the unsolved model
    assert r["compare"]["ok"] and r["set_default"] and r["compare"]["solved"] == [solved, solved], name
    assert r["model_source"]["fingerprint"], name
    assert r["marker"].endswith(f" {r['model']}/{name} v{r['source_version']}"), r["marker"]
    if name != "standard_extra":
        assert r["run"]["accepted"] and r["run"]["default"] == r["source_version"], name
    print(f"{name}: job v{r['source_version']} -> main v{r['merged_version']}, OBJ {r['compare']['OBJ'][1]}, "
          f"in place {bool(r['run'] and r['run']['in_place'])}, "
          f"GAMS {str(r['model_source']['git_commit'])[:10]} {r['model_source']['fingerprint'][:12]}")
assert sum(bool(r["run"] and r["run"]["in_place"]) for r in records) == 1
EOF

say "cleanup: first what holds no run output, then the rest with --include-outputs"
ixc cleanup --area test --main results
ixc cleanup --area test --main results --apply
LOCAL_AREA="$LOCAL_ROOT/ixmp_copies/$NAME/test"
left=$(ls "$LOCAL_AREA/jobs" | sort | paste -sd' ')
echo "left: $left"
for gone in "read_n_$run_n" "merge_seed_$from_seed"; do
    case " $left " in *" $gone "*) echo "cleanup kept $gone (no output, merged or read only)" >&2; exit 1 ;; esac
done
for kept in "read_o_$run_o" "solve_i_$run_i" "solve_c_$run_c" "solve_f_$run_f" "solve_g_$run_g"; do
    case " $left " in *" $kept "*) ;; *) echo "cleanup deleted $kept, which holds run output" >&2; exit 1 ;; esac
done
ixc cleanup --area test --main results --include-outputs --apply
left=$(ls "$LOCAL_AREA/jobs" | sort | paste -sd' ')
want="merge_seed_$resubmitted solve_f_$run_f solve_g_$run_g"
[ "$left" = "$want" ] || { echo "after cleanup --include-outputs: [$left], wanted [$want]" >&2; exit 1; }
ixc verify "$LOCAL_AREA/seeds/seed1"
ixc verify "$LOCAL_AREA/seeds/seed2"
say "TRIAL PASSED: project $PROJECT, H-drive area $AREA"
