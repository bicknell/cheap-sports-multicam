#!/bin/bash

# Runs on the droplet: steps 2-5 unattended, no user input. Invoked by
# ../droplet-run.sh over a non-interactive ssh command (CSM_BUCKET passed
# explicitly in that command, not relied on from any rc file -- a
# non-interactive `ssh host 'cmd'` session sources neither ~/.bashrc nor
# /etc/profile.d).
#
# step1 (apt upgrade) is deliberately not included: it can trigger a
# reboot, which would kill the very SSH session this script is running in,
# so steps after it would silently never run. See README.md.
#
# step6 (YouTube) is deliberately not included yet -- it needs a real
# rewrite to run unattended (read title/description from a metadata file
# instead of printing a fill-in-the-blanks command). Deferred.

set -e

_start=$(date +%s)
trap 'echo "[$(basename "$0")] elapsed: $(( $(date +%s) - _start ))s"' EXIT

if [ "$#" -lt 1 ]; then
    echo "Error: game directory not specified"
    echo "Usage: $0 <bucket-directory> [sync/render flags...] [-- render-only flags...]"
    exit 1
fi

GAMEDIR="$1"
shift

echo "=== step2: install software ==="
date
./droplet-step2-install-software.sh

echo "=== step3: download video ==="
date
./droplet-step3-download-video.sh "$GAMEDIR"

echo "=== step4: process video ==="
date
./droplet-step4-process.video.sh "$GAMEDIR" "$@"

echo "=== step5: upload video ==="
date
./droplet-step5-upload-video.sh "$GAMEDIR"

echo "=== all steps complete ==="
date
