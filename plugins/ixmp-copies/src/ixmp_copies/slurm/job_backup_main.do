#!/bin/bash
#SBATCH --job-name=ixc_backup_main
#SBATCH --time=01:00:00
#SBATCH --mem=4G
#SBATCH --cpus-per-task=1
#SBATCH --output=%x_%j.out

# A checked backup of a results main <area>/mains/NAME/, taken on the cluster (the main lives
# on the share, which a workstation may reach only over CIFS) and verified from this node.
# The backup lands in <area>/backups/NAME/<time>/. Env: CODE, AREA, NAME.
: "${CODE:?}" "${AREA:?}" "${NAME:?}"
source "$CODE/.ixmp_copies/slurm/common.sh"
MAIN="$(ixc where --area "$AREA" mains)/$NAME"
[ -f "$MAIN/ixmp/config.json" ] || { echo "no results main at $MAIN"; exit 3; }
export IXMP_DATA="$MAIN/ixmp"
step ixc backup || exit $?
step ixc backup --apply || exit $?
DEST="$(ls -d "$(ixc where --area "$AREA" backups)/$NAME"/*/ | sort | tail -n 1)"
step ixc verify "$DEST"
