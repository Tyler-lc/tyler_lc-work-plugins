#!/bin/bash
#SBATCH --job-name=ixc_merge_from_seed
#SBATCH --time=01:30:00
#SBATCH --partition=generic
#SBATCH --mem=48G
#SBATCH --cpus-per-task=2
#SBATCH --output=%x_%j.out

# Merge scenarios that exist only in a seed (e.g. built on the workstation after the main was
# made) into a results main. A merge never opens a seed: the job makes its own closed copy of it
# and merges from that, one scenario at a time. Merges into the main must not overlap: give
# this job the main's merge job name and --dependency=singleton when others may be running.
# Env: CODE, AREA, SEED, MAIN, SCENARIOS ("name name:version ..."; no version = the seed's
# default); optional MODEL.
: "${CODE:?}" "${AREA:?}" "${SEED:?}" "${MAIN:?}" "${SCENARIOS:?}"
source "$CODE/.ixmp_copies/slurm/common.sh"
export JAVA_TOOL_OPTIONS="-Xmx$IXC_HEAP_MERGE"
JOB_DIR="$(ixc where --area "$AREA" jobs)/merge_seed_${SLURM_JOB_ID}"
step ixc job-copy --seed "$SEED" --job-dir "$JOB_DIR" --area "$AREA" || exit 1
step ixc job-close --job-dir "$JOB_DIR" || exit 1
for sv in $SCENARIOS; do echo "${sv%%:*}"; done > "$JOB_DIR/expected_merges.txt"
export IXMP_DATA="$MAIN/ixmp"
step ixc job-check --job-dir "$MAIN" || exit 1
RC=0
for sv in $SCENARIOS; do
    version=(); [ "$sv" != "${sv#*:}" ] && version=(--version "${sv#*:}")
    step ixc merge --job-dir "$JOB_DIR" --scenario "${sv%%:*}" "${version[@]}" ${MODEL:+--model "$MODEL"} \
        --into "$PLATFORM" --apply || RC=1
done
echo "Exit: merges $RC  $(date -Is)"
exit $RC
