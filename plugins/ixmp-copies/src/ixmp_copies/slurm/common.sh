# Sourced by every ixmp-copies job. CODE is a snapshot made by `ixmp-copies stage`; its
# .ixmp_copies/job.env carries the cluster settings from the project's ixmp_copies.toml.
: "${CODE:?CODE: a snapshot made by ixmp-copies stage}"
source "$CODE/.ixmp_copies/job.env"
source "$IXC_LMOD_INIT"
module purge
for m in $IXC_MODULES; do module load "$m"; done
source "$IXC_VENV/bin/activate"
# A job reaches databases only through the config it sets itself.
unset IXMP_DATA
PLATFORM="$IXC_PLATFORM"
step() { echo "== $(date -Is) $*"; "$@"; local rc=$?; echo "== exit $rc: $1 ${2:-}"; return $rc; }
ixc() { python -u -m ixmp_copies "$@"; }
# The tool and the project's code, from one snapshot folder (CODE, or a job's copy of it).
use_code() { cd "$1" || return 1; export PYTHONPATH="$1/.ixmp_copies/src:$1"; }
use_code "$CODE"
