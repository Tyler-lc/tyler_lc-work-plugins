#!/bin/bash
# Submit a batch of runs, each on its own copy of SEED, and merge each into MAIN.
# From the cluster login node:
#
#   CODE=<snapshot> AREA=<area> bash $CODE/.ixmp_copies/slurm/submit_runs.sh SEED MAIN RUNS_FILE
#
# RUNS_FILE: one run per line, `NAME SCENARIO COMMAND...` (# comments and blank lines skipped).
# NAME names the job folder ([a-z0-9_]); SCENARIO is what the run leaves solved as its default
# version and what gets merged ("-" for no merge); COMMAND runs in the job's code copy.
# Optional env: AFTER (a job id every run waits for, e.g. the seed job), MODEL (the scenarios'
# model; default [project] model), RUN_OPTS / MERGE_OPTS (extra sbatch options, e.g.
# "--time=08:00:00 --mem=64G").
# The submission record gets a run's line as soon as the run is submitted,
#   run NAME job=ID dir=JOB_DIR [scenario=SCENARIO] model=MODEL cmd=COMMAND
# (the model, which may hold spaces, is the last field before cmd=), and then its merge's line,
#   merge NAME merge=ID
# so a run whose merge could not be submitted is still recorded, and submit_merges.sh merges it
# with the same model and scenario.
# Every merge into MAIN runs under one job name with --dependency=singleton, one at a time;
# a failed run cancels its own merge only. Refuses a second submission of the same RUNS_FILE
# into the same MAIN. Logs and the submission record go to <area>/runs/.
set -euo pipefail
: "${CODE:?}" "${AREA:?}"
SEED="${1:?SEED}"; MAIN="${2:?MAIN}"; RUNS_FILE="${3:?RUNS_FILE}"
# common.sh changes directory: paths given relative to the caller's folder are made absolute first.
SEED="$(realpath -m "$SEED")"; MAIN="$(realpath -m "$MAIN")"; RUNS_FILE="$(realpath -m "$RUNS_FILE")"
# The login node's own python may predate tomllib: use the jobs' modules and venv.
set +u; source "$CODE/.ixmp_copies/slurm/common.sh"; set -u
D="$CODE/.ixmp_copies/slurm"
RUNS="$(ixc where --area "$AREA" runs)"
JOBS="$(ixc where --area "$AREA" jobs)"
[ -f "$SEED/manifest.json" ] || { echo "$SEED is not a seed" >&2; exit 1; }
[ -f "$MAIN/ixmp/config.json" ] || { echo "$MAIN is not a results main" >&2; exit 1; }
[ -f "$RUNS_FILE" ] || { echo "no $RUNS_FILE" >&2; exit 1; }
mkdir -p "$RUNS"
MAIN_NAME=$(basename "$MAIN"); BATCH=$(basename "$RUNS_FILE" | sed 's/\.[^.]*$//')
if compgen -G "$RUNS/submitted_${BATCH}_into_${MAIN_NAME}_*.txt" > /dev/null; then
    echo "$BATCH was already submitted into $MAIN_NAME: $(ls "$RUNS"/submitted_"${BATCH}"_into_"${MAIN_NAME}"_*.txt)" >&2
    exit 1
fi
REC="$RUNS/submitted_${BATCH}_into_${MAIN_NAME}_$(date +%Y%m%d_%H%M%S).txt"
cd "$RUNS"
DEP=(); [ -n "${AFTER:-}" ] && DEP=(--dependency="afterok:$AFTER" --kill-on-invalid-dep=yes)
echo "batch $BATCH seed $SEED main $MAIN code $CODE runs_file $RUNS_FILE" | tee "$REC"
# A last line without a newline is still a run: read fails on it but has filled the fields.
while read -r name scenario cmd || [ -n "$name" ]; do
    [ -z "$name" ] || [ "${name:0:1}" = "#" ] && continue
    # Values go through the environment (--export=ALL): --export=VAR=value splits on commas.
    # MERGE_SCENARIO "-" tells the job it merges nothing, which lets cleanup delete its copy.
    # shellcheck disable=SC2086
    run=$(CODE="$CODE" AREA="$AREA" SEED="$SEED" NAME="$name" CMD="$cmd" MERGE_SCENARIO="$scenario" MODEL="${MODEL:-}" \
        sbatch --parsable --export=ALL --partition="$IXC_PARTITION" --job-name="$name" "${DEP[@]}" \
        ${RUN_OPTS:-} "$D/job_run.do")
    line="run $name job=$run dir=$JOBS/${name}_${run}"
    if [ "$scenario" != "-" ]; then line="$line scenario=$scenario"; fi
    echo "$line model=${MODEL:-$IXC_MODEL} cmd=$cmd" | tee -a "$REC"
    [ "$scenario" != "-" ] || continue
    # shellcheck disable=SC2086
    if ! merge=$(CODE="$CODE" MAIN="$MAIN" SRC_JOB="$JOBS/${name}_${run}" SCENARIO="$scenario" \
            MODEL="${MODEL:-}" VERSION="" sbatch --parsable --export=ALL --partition="$IXC_PARTITION" \
            --job-name="merge_into_$MAIN_NAME" --dependency="afterok:$run,singleton" \
            --kill-on-invalid-dep=yes ${MERGE_OPTS:-} "$D/job_merge.do"); then
        echo "the merge of $name could not be submitted; run $run is recorded in $REC: submit its merge with" \
            "submit_merges.sh $REC. Runs after $name in $RUNS_FILE were not submitted." >&2
        exit 1
    fi
    echo "merge $name merge=$merge" | tee -a "$REC"
done < "$RUNS_FILE"
echo "record $REC; monitor: squeue -u \$USER; per-job exit: grep -H Exit: $RUNS/*.out"
