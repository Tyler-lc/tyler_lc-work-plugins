#!/bin/bash
#SBATCH --job-name=ixc_run
#SBATCH --time=04:00:00
#SBATCH --mem=32G
#SBATCH --cpus-per-task=8
#SBATCH --output=%x_%j.out

# Run CMD (a shell command line) on this job's own copy of SEED:
# job-copy -> code into the job folder -> IXMP_DATA -> job-check -> CMD -> job-close -> seed verify.
# CMD runs from the job's code copy with IXMP_DATA set, so `ixmp.Platform("$PLATFORM")` (or the
# default platform) opens this job's copy and nothing else; IXC_JOB_DIR names the job folder.
# Records CMD writes inside the code copy are brought home by `ixmp-copies collect`.
# job-close runs whatever CMD returned, so a solved copy is always recorded.
# With MERGE_SCENARIO (submit_runs.sh sets it), the job records that scenario's versions before
# CMD and the version CMD left as default after it (run-mark); the job fails, and its merge does
# not run, when CMD left no new version as default. CMD must call set_as_default() on its result.
# Env: CODE, AREA, SEED, NAME (lower-case [a-z0-9_]), CMD; optional MERGE_SCENARIO, MODEL. Pass them through the environment
# with --export=ALL: --export=CMD=... splits the value on commas.
: "${CODE:?}" "${AREA:?}" "${SEED:?}" "${NAME:?}" "${CMD:?}"
source "$CODE/.ixmp_copies/slurm/common.sh"
[ -n "$IXC_GAMS_MODULE" ] && module load "$IXC_GAMS_MODULE"
export JAVA_TOOL_OPTIONS="-Xmx$IXC_HEAP_RUN"
JOB_DIR="$(ixc where --area "$AREA" jobs)/${NAME}_${SLURM_JOB_ID}"
echo "run [$CMD] on a copy of $SEED in $JOB_DIR; code $CODE; $(hostname)"
step ixc job-copy --seed "$SEED" --job-dir "$JOB_DIR" --area "$AREA" || exit 1
# The scenario this run's merge brings back (submit_runs.sh sets it): cleanup needs its record.
[ -n "${MERGE_SCENARIO:-}" ] && echo "$MERGE_SCENARIO" > "$JOB_DIR/expected_merges.txt"
step cp -r "$CODE" "$JOB_DIR/code" || exit 1
use_code "$JOB_DIR/code"
mkdir -p "$JOB_DIR/tmp"
export IXMP_DATA="$JOB_DIR/ixmp" IXC_JOB_DIR="$JOB_DIR" TMPDIR="$JOB_DIR/tmp"
step ixc job-check --job-dir "$JOB_DIR" || exit 1
MARK=0
if [ -n "${MERGE_SCENARIO:-}" ]; then
    # The scenario's versions before the command, so the merge can tell the run's result from a
    # version that was already there (a command that forgets set_as_default() leaves one).
    step ixc run-mark --job-dir "$JOB_DIR" --scenario "$MERGE_SCENARIO" ${MODEL:+--model "$MODEL"} --before
    MARK=$?
fi
RUN=1
if [ $MARK -eq 0 ]; then
    step bash -c "$CMD"
    RUN=$?
fi
if [ $RUN -eq 0 ] && [ -n "${MERGE_SCENARIO:-}" ]; then
    step ixc run-mark --job-dir "$JOB_DIR" --scenario "$MERGE_SCENARIO" ${MODEL:+--model "$MODEL"} --after
    MARK=$?
fi
step ixc job-close --job-dir "$JOB_DIR"
CLOSE=$?
step ixc verify "$SEED"
SEEDOK=$?
echo "Exit: run $RUN mark $MARK close $CLOSE seed $SEEDOK  $(date -Is)"
[ $RUN -eq 0 ] && [ $MARK -eq 0 ] && [ $CLOSE -eq 0 ] && [ $SEEDOK -eq 0 ]
