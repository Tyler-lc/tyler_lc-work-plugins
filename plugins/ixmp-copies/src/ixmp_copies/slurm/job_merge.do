#!/bin/bash
#SBATCH --job-name=ixc_merge
#SBATCH --time=01:00:00
#SBATCH --mem=48G
#SBATCH --cpus-per-task=2
#SBATCH --output=%x_%j.out

# Merge SCENARIO from a closed job copy (SRC_JOB) into a results main (MAIN). Both platforms
# sit in one JVM: a 16 GB heap peaked at 21 GB RSS on a 300 MB database and 33.5 GB on a
# 1.1 GB one, hence 48 GB. Submit merges into one main under one job name with
# --dependency=singleton so SLURM runs them one at a time (the tool refuses an open target).
# Env: CODE, MAIN, SRC_JOB, SCENARIO; optional VERSION (default: the job copy's default
# version) and MODEL (default: [project] model).
: "${CODE:?}" "${MAIN:?}" "${SRC_JOB:?}" "${SCENARIO:?}"
source "$CODE/.ixmp_copies/slurm/common.sh"
export JAVA_TOOL_OPTIONS="-Xmx$IXC_HEAP_MERGE"
export IXMP_DATA="$MAIN/ixmp"
step ixc job-check --job-dir "$MAIN" || exit 1
step ixc merge --job-dir "$SRC_JOB" --scenario "$SCENARIO" ${VERSION:+--version "$VERSION"} \
    ${MODEL:+--model "$MODEL"} --into "$PLATFORM" --apply
