#!/bin/bash

# Full automation: find size -> create -> upload -> run steps 2-5 unattended
# -> destroy. Size/region come from droplet-find-size.sh against
# droplet-size-priority.txt, not from droplet-spec.local.json.
# Usage: ./droplet-run.sh [--keep-on-failure] GAMEDIR [sync/render flags...] [-- render-only flags...]
# render-only flags (e.g. --start, --duration, --out-size, --profile) must go
# after a literal "--" -- see droplet-step4-process.video.sh.
#
# Default is to always destroy the droplet when this script exits, success
# or failure -- a droplet nobody destroys is the expensive failure
# (DESIGN.md Sec 14.2: ~$950/month at $1.30/hr if forgotten). Pass
# --keep-on-failure to leave it running instead, for debugging a
# particularly tricky failure -- remember to ./droplet-destroy.sh yourself
# when done.

set -e

keep_on_failure=false
positional=()
for arg in "$@"; do
    if [[ $arg == "--keep-on-failure" ]];
    then
        keep_on_failure=true
    else
        positional+=("$arg")
    fi
done

if [[ ${#positional[@]} -lt 1 ]];
then
    echo "Usage: $0 [--keep-on-failure] GAMEDIR [sync/render flags...] [-- render-only flags...]"
    exit 1
fi

GAMEDIR="${positional[0]}"
csm_flags=("${positional[@]:1}")

if [[ -z $DIGITAL_OCEAN_TOKEN ]];
then
    echo "Must set DIGITAL_OCEAN_TOKEN."
    exit 1
fi

if [[ -z $CSM_BUCKET ]];
then
    echo "Must set CSM_BUCKET (e.g. export CSM_BUCKET=loudoun-25-26-2012g-silver)."
    exit 1
fi

# Fail fast, before spending any money, rather than discovering mid-run that
# no key is available for the ssh calls below.
if ! ssh-add -l > /dev/null 2>&1;
then
    echo "No SSH key loaded in ssh-agent (or no agent running)."
    echo "Run: ssh-add ~/.ssh/id_whatever"
    exit 1
fi

mkdir -p logs

# Pick a size/region before creating anything -- droplet-find-size.sh checks
# real-time availability across nyc1/nyc2/nyc3, so a stale/sold-out slug in
# droplet-spec.local.json doesn't silently fail create.sh after billing has
# already started.
priority_file="droplet-size-priority.txt"
if [[ ! -f $priority_file ]];
then
    echo "Missing ${priority_file} -- see droplet-find-size.sh for the expected format."
    exit 1
fi

priority_slugs=()
while read -r line;
do
    line="${line%%#*}"    # strip comments (whole-line or trailing)
    [[ -n $line ]] && priority_slugs+=("$line")
done < "$priority_file"

if [[ ${#priority_slugs[@]} -eq 0 ]];
then
    echo "${priority_file} has no slugs listed."
    exit 1
fi

size_log="logs/droplet-find-size-$(date -u +%Y%m%dT%H%M%SZ).log"

echo "=== find size ==="
date
./droplet-find-size.sh "${priority_slugs[@]}" | tee "$size_log"
find_size_status=${PIPESTATUS[0]}

if [[ $find_size_status -ne 0 ]];
then
    echo "No candidate size available in nyc1/nyc2/nyc3 -- see ${size_log}. Not creating a droplet."
    exit 1
fi

export DROPLET_SIZE=$(grep '^SLUG=' "$size_log" | cut -d= -f2)
export DROPLET_REGION=$(grep '^REGION=' "$size_log" | cut -d= -f2)

if [[ -z $DROPLET_SIZE || -z $DROPLET_REGION ]];
then
    echo "Could not parse a recommendation out of droplet-find-size.sh's output -- see ${size_log}."
    exit 1
fi

echo "=== create ==="
date
./droplet-create.sh

# From here on, a failure must still reach the destroy-or-keep decision
# below -- set -e would abort the whole script on the first failing
# command, which is fine before create.sh (nothing to clean up yet) but not
# after (a droplet now exists and is billing).
set +e

echo "=== upload ==="
date
./droplet-upload.sh
upload_status=$?

run_status=1
log_file="logs/droplet-run-${GAMEDIR}-$(date -u +%Y%m%dT%H%M%SZ).log"

if [[ -z $IPV4ONLY ]];
then
    ssh_host=$(cat droplet.ipv6 2>/dev/null)
else
    ssh_host=$(cat droplet.ipv4 2>/dev/null)
fi

if [[ $upload_status -eq 0 && -n $ssh_host ]];
then
    echo "=== run (droplet-all-steps.sh) ==="
    date
    remote_cmd="CSM_BUCKET=$(printf '%q' "$CSM_BUCKET") ./droplet-all-steps.sh"
    for arg in "$GAMEDIR" "${csm_flags[@]}"; do
        remote_cmd+=" $(printf '%q' "$arg")"
    done
    # See droplet-upload.sh: fresh host key every create, destroyed in
    # hours -- don't prompt, don't persist it.
    ssh -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null \
        "root@${ssh_host}" "$remote_cmd" 2>&1 | tee "$log_file"
    run_status=${PIPESTATUS[0]}
fi

if [[ $upload_status -ne 0 ]];
then
    overall_status=$upload_status
else
    overall_status=$run_status
fi

echo "Session log: ${log_file}"

echo "=== destroy-or-keep decision ==="
date

if [[ $overall_status -eq 0 ]];
then
    echo "Run succeeded. Destroying droplet."
    ./droplet-destroy.sh
elif [[ $keep_on_failure == true ]];
then
    created_at=$(cat droplet.created_at 2>/dev/null)
    if [[ -n $created_at ]];
    then
        elapsed_s=$(( $(date -u +%s) - created_at ))
        echo "Run failed (exit ${overall_status}). Droplet left running for debugging" \
             "(~$((elapsed_s / 60))m and counting, still billing)."
    else
        echo "Run failed (exit ${overall_status}). Droplet left running for debugging (still billing)."
    fi
    echo "Log in via ./droplet-login.sh; when done, ./droplet-destroy.sh."
else
    echo "Run failed (exit ${overall_status}). Destroying droplet anyway" \
         "(pass --keep-on-failure to leave it running next time)."
    ./droplet-destroy.sh
fi

exit "$overall_status"
