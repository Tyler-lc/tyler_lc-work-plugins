#!/bin/bash
# Resubmit the merges of one submit_runs.sh batch that did not happen: a merge that failed (a
# bug since fixed, a node lost) or was cancelled. From the cluster login node:
#
#   CODE=<snapshot> bash $CODE/.ixmp_copies/slurm/submit_merges.sh <submission record>
#
# CODE may be a newer snapshot than the batch's (one with the fix). For every run in the record
# that has a scenario to merge: skipped if its latest merge job completed or is still queued or
# running, or exited 4 (failed after its backup: the main may hold a partial version, restore it
# from the backup that merge's log names first); merged now if the run COMPLETED and its job
# closed its copy (result.json); merged after it (afterok) if the run is still PENDING or
# RUNNING; otherwise listed and skipped. Merges into the main run one at a time (one job name,
# --dependency=singleton). Safe to repeat: the tool refuses a merge already made. The model is
# the record's (model=...); MODEL, for records that carry none. Optional env: MERGE_OPTS (extra
# sbatch options). Appends what it did to the record.
set -euo pipefail
: "${CODE:?}"
REC="${1:?submission record}"
[ -f "$REC" ] || { echo "no $REC" >&2; exit 1; }
REC="$(realpath "$REC")"  # common.sh changes directory
set +u; source "$CODE/.ixmp_copies/slurm/common.sh"; set -u
D="$CODE/.ixmp_copies/slurm"
# By position: "batch B seed S main M code C runs_file R" (B may itself be "main").
MAIN=$(sed -n -E 's/^batch [^ ]+ seed [^ ]+ main ([^ ]+) code .*/\1/p' "$REC" | head -1)
[ -f "$MAIN/ixmp/config.json" ] || { echo "the record names no results main ($MAIN)" >&2; exit 1; }
MAIN_NAME=$(basename "$MAIN")
cd "$(dirname "$REC")"
state() { sacct -n -X -P -o State -j "$1" 2>/dev/null | head -1 | awk '{print $1}'; }
exitcode() { sacct -n -X -P -o ExitCode -j "$1" 2>/dev/null | head -1; }
echo "merges resubmitted $(date -Is) from $CODE" >> "$REC"
# An absent field is empty, not an error: under set -e and pipefail a grep that finds nothing
# would end the script and silently drop every run after that line.
field() { echo " $1" | grep -o " $2=[^ ]*" | head -1 | cut -d= -f2- || true; }
{ grep -E '^run ' "$REC" || true; } | while read -r _ name rest; do
    # The command is the free text after " cmd=", always last, and the model (which may hold
    # spaces) the text between " model=" and it: the other fields are read only before both.
    head="${rest%% cmd=*}"
    model="${MODEL:-}"; case "$head" in *" model="*) model="${head#* model=}" ;; esac
    head="${head%% model=*}"
    scenario=$(field "$head" scenario)
    [ -n "$scenario" ] || continue
    run=$(field "$head" job); dir=$(field "$head" dir)
    # The latest merge of this run: a resubmission's line, else the original one.
    last=$(grep -E "^(remerge $name |run $name )" "$REC" | sed 's/ cmd=.*//; s/ model=.*//' | grep -o ' merge=[0-9]*' | tail -1 | cut -d= -f2 || true)
    if [ -n "$last" ]; then
        case "$(state "$last")" in
            COMPLETED|PENDING|RUNNING) echo "skip $name: merge $last is $(state "$last")"; continue ;;
        esac
        if [ "$(exitcode "$last")" = "4:0" ]; then
            echo "skip $name: merge $last exited 4 (failed after its backup): restore first, from the backup its log names"
            continue
        fi
    fi
    case "$(state "$run")" in
        COMPLETED)
            [ -f "$dir/result.json" ] || { echo "skip $name: run $run completed but $dir has no result.json"; continue; }
            dep=(--dependency=singleton) ;;
        PENDING|RUNNING)
            dep=(--dependency="afterok:$run,singleton" --kill-on-invalid-dep=yes) ;;
        *)
            echo "skip $name: run $run is $(state "$run")"; continue ;;
    esac
    # shellcheck disable=SC2086
    merge=$(CODE="$CODE" MAIN="$MAIN" SRC_JOB="$dir" SCENARIO="$scenario" MODEL="$model" VERSION="" \
        sbatch --parsable --export=ALL --partition="$IXC_PARTITION" --job-name="merge_into_$MAIN_NAME" \
        "${dep[@]}" ${MERGE_OPTS:-} "$D/job_merge.do")
    echo "remerge $name run=$run merge=$merge" | tee -a "$REC"
done
