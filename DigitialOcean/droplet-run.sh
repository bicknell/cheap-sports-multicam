#!/bin/bash

# Full automation: create -> upload -> run steps 2-5 unattended -> destroy.
# Usage: ./droplet-run.sh [--keep-on-failure] GAMEDIR [csm sync/render flags...]
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
    echo "Usage: $0 [--keep-on-failure] GAMEDIR [csm sync/render flags...]"
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

./droplet-create.sh

# From here on, a failure must still reach the destroy-or-keep decision
# below -- set -e would abort the whole script on the first failing
# command, which is fine before create.sh (nothing to clean up yet) but not
# after (a droplet now exists and is billing).
set +e

./droplet-upload.sh
upload_status=$?

run_status=1
mkdir -p logs
log_file="logs/droplet-run-${GAMEDIR}-$(date -u +%Y%m%dT%H%M%SZ).log"

if [[ -z $IPV4ONLY ]];
then
    ssh_host=$(cat droplet.ipv6 2>/dev/null)
else
    ssh_host=$(cat droplet.ipv4 2>/dev/null)
fi

if [[ $upload_status -eq 0 && -n $ssh_host ]];
then
    remote_cmd="CSM_BUCKET=$(printf '%q' "$CSM_BUCKET") ./droplet-all-steps.sh"
    for arg in "$GAMEDIR" "${csm_flags[@]}"; do
        remote_cmd+=" $(printf '%q' "$arg")"
    done
    ssh "root@${ssh_host}" "$remote_cmd" 2>&1 | tee "$log_file"
    run_status=${PIPESTATUS[0]}
fi

if [[ $upload_status -ne 0 ]];
then
    overall_status=$upload_status
else
    overall_status=$run_status
fi

echo "Session log: ${log_file}"

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
