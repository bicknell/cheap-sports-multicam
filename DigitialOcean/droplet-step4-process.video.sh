#!/bin/bash

set -e

_start=$(date +%s)
trap 'echo "[$(basename "$0")] total elapsed: $(( $(date +%s) - _start ))s"' EXIT

if [ "$#" -lt 1 ]; then
    echo "Error: game directory not specified"
    echo "Usage: $0 <bucket-directory> [sync/render flags...] [-- render-only flags...]"
    exit 1
fi

GAMEDIR="$1"
shift

if [ ! -d "$GAMEDIR" ]; then
    echo "Error: ${GAMEDIR} is not a directory (did step3's download finish?)"
    exit 1
fi

mkdir -p "$GAMEDIR/.csm"

# Flags before "--" go to both sync and render (e.g. --offset, --nudge --
# sync-tuning flags render also accepts for override). Flags after "--" go
# to render only -- required for render-only flags like --start/--duration/
# --out-size/--profile, which csm sync does not recognize and would reject.
shared_flags=()
render_only_flags=()
seen_dashdash=false
for arg in "$@"; do
    if [[ $seen_dashdash == false && $arg == "--" ]];
    then
        seen_dashdash=true
    elif [[ $seen_dashdash == true ]];
    then
        render_only_flags+=("$arg")
    else
        shared_flags+=("$arg")
    fi
done

csm probe  "$GAMEDIR"
csm sync   "$GAMEDIR" --write "${shared_flags[@]}"
csm render "$GAMEDIR" --dry-run "${shared_flags[@]}" "${render_only_flags[@]}" | tee "$GAMEDIR/.csm/render.sh"   # audit trail

_encode_start=$(date +%s)
csm render "$GAMEDIR" --profile final-h265 "${shared_flags[@]}" "${render_only_flags[@]}"
echo "[$(basename "$0")] encode wall time: $(( $(date +%s) - _encode_start ))s"

echo "Alert!  Encoding is finished.!!!"
for i in {1..10}
do
  echo -e "\a"
  sleep 1
done
