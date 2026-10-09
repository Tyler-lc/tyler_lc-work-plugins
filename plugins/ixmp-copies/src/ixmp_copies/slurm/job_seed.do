#!/bin/bash
#SBATCH --job-name=ixc_seed
#SBATCH --time=00:30:00
#SBATCH --mem=4G
#SBATCH --cpus-per-task=1
#SBATCH --output=%x_%j.out

# A read-only seed <area>/seeds/NAME/ made on the cluster (chmod takes on the share's NFS
# side, not over CIFS), from a backup (BACKUP) or from a job copy its job closed (SRC_JOB).
# Env: CODE, AREA, NAME, and BACKUP or SRC_JOB.
: "${CODE:?}" "${AREA:?}" "${NAME:?}"
source "$CODE/.ixmp_copies/slurm/common.sh"
if [ -n "${BACKUP:-}" ]; then SRC=(--from "$BACKUP"); else SRC=(--from-job "${SRC_JOB:?BACKUP or SRC_JOB}"); fi
step ixc seed "${SRC[@]}" --area "$AREA" --name "$NAME" --apply
