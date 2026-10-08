#!/bin/bash
#SBATCH --job-name=ixc_make_main
#SBATCH --time=00:30:00
#SBATCH --partition=generic
#SBATCH --mem=4G
#SBATCH --cpus-per-task=1
#SBATCH --output=%x_%j.out

# A results main <area>/mains/NAME/ from a seed: the database merges go into, with its own
# ixmp config naming it alone. Env: CODE, AREA, SEED, NAME.
: "${CODE:?}" "${AREA:?}" "${SEED:?}" "${NAME:?}"
source "$CODE/.ixmp_copies/slurm/common.sh"
step ixc job-copy --kind main --seed "$SEED" --job-dir "$(ixc where --area "$AREA" mains)/$NAME" --area "$AREA"
