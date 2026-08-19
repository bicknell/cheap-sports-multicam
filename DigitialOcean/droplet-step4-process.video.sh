#!/bin/bash

set -e

_start=$(date +%s)
trap 'echo "[$(basename "$0")] total elapsed: $(( $(date +%s) - _start ))s"' EXIT

if [ "$#" -lt 1 ]; then
    echo "Error: game directory not specified"
    echo "Usage: $0 <bucket-directory> [csm sync/render flags...]"
    exit 1
fi

GAMEDIR="$1"
shift

if [ ! -d "$GAMEDIR" ]; then
    echo "Error: ${GAMEDIR} is not a directory (did step3's download finish?)"
    exit 1
fi

mkdir -p "$GAMEDIR/.csm"

csm probe  "$GAMEDIR"
csm sync   "$GAMEDIR" --write "$@"
csm render "$GAMEDIR" --dry-run "$@" | tee "$GAMEDIR/.csm/render.sh"   # audit trail

_encode_start=$(date +%s)
csm render "$GAMEDIR" --profile final-h265 "$@"
echo "[$(basename "$0")] encode wall time: $(( $(date +%s) - _encode_start ))s"

echo "Alert!  Encoding is finished.!!!"
for i in {1..10}
do
  echo -e "\a"
  sleep 1
done
