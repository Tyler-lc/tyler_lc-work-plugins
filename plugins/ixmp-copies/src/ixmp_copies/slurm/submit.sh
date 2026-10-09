#!/bin/bash
# Submit one ixmp-copies job template with the project's partition, from the login node:
#
#   CODE=<snapshot> bash $CODE/.ixmp_copies/slurm/submit.sh TEMPLATE [VAR=value ...] [-- sbatch options]
#
# e.g.  submit.sh job_seed.do AREA=live NAME=seed1 BACKUP=/hdrive/all_users/me/...
#       submit.sh job_merge_from_seed.do AREA=live SEED=... MAIN=... SCENARIOS="a b:3" -- --mem=64G
# VAR=value pairs reach the job through the environment (--export=ALL), so values may hold commas.
# The templates' variables that are not given are unset first: a value exported in the calling
# shell (e.g. BACKUP from an earlier command) never reaches the job. Relative values of the path
# variables (BACKUP SEED MAIN SRC_JOB) are made absolute; others (AREA, NAME, ...) pass as given.
# The partition is [cluster] partition; options after -- go to sbatch as they are (they override
# the template's #SBATCH lines). Logs go to <area>/runs/ when AREA is given, else to the current
# folder. Prints the job id.
set -euo pipefail
: "${CODE:?CODE: a snapshot made by ixmp-copies stage}"
TEMPLATE="${1:?TEMPLATE (e.g. job_seed.do)}"; shift
D="$CODE/.ixmp_copies/slurm"
[ -f "$D/$TEMPLATE" ] || { echo "no template $TEMPLATE in $D" >&2; exit 1; }
VARS=(); AREA_ARG=""
while [ $# -gt 0 ] && [ "$1" != "--" ]; do
    case "$1" in *=*) ;; *) echo "not VAR=value: $1" >&2; exit 1 ;; esac
    key="${1%%=*}"; value="${1#*=}"
    case "$key" in
        BACKUP|SEED|MAIN|SRC_JOB)
            case "$value" in ""|/*|"~"*) ;; *) value="$(realpath -m "$value")" ;; esac ;;
    esac
    [ "$key" = AREA ] && AREA_ARG="$value"
    VARS+=("$key=$value"); shift
done
[ "${1:-}" = "--" ] && shift
unset AREA BACKUP SRC_JOB SEED MAIN NAME SCENARIOS VERSION MODEL CMD MERGE_SCENARIO SCENARIO
CALLER="$PWD"
set +u; source "$D/common.sh"; set -u   # changes directory to CODE
LOGS="$CALLER"
if [ -n "$AREA_ARG" ]; then LOGS="$(ixc where --area "$AREA_ARG" runs)"; mkdir -p "$LOGS"; fi
cd "$LOGS"
env "${VARS[@]}" CODE="$CODE" sbatch --parsable --export=ALL --partition="$IXC_PARTITION" "$@" "$D/$TEMPLATE"
