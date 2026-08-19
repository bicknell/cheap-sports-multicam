#!/bin/bash

set -e

if [[ -z $CSM_BUCKET ]];
then
    echo "Must set CSM_BUCKET (e.g. export CSM_BUCKET=loudoun-25-26-2012g-silver)."
    exit 1
fi

if [[ -z $IPV4ONLY ]];
then
    if [[ -f droplet.ipv6 ]];
    then
        ssh_host=$(cat droplet.ipv6)
        scp_host="[${ssh_host}]"  # scp's legacy host:path syntax needs brackets to
                                  # disambiguate from the IPv6 address's own colons; plain
                                  # ssh wants the bare address instead
    fi
else
    if [[ -f droplet.ipv4 ]];
    then
        ssh_host=$(cat droplet.ipv4)
        scp_host="$ssh_host"
    fi
fi

if [[ -z $ssh_host ]];
then
    echo "Unable to determine IP address."
    exit 1
fi

# Every droplet gets a fresh host key on create, and this one is destroyed
# in hours -- skip the interactive fingerprint prompt and don't persist the
# key to ~/.ssh/known_hosts, where it would be pure clutter (or worse, a
# scary "REMOTE HOST IDENTIFICATION HAS CHANGED" warning for a future
# unrelated droplet that happens to reuse the same IP).
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null)

FILES=(droplet-step* droplet-all-steps.sh)

if [[ -f ~/.s3cfg ]]; then
    FILES+=(~/.s3cfg)
else
    echo "~/.s3cfg not found, you must place your credentials manually."
fi

if [[ -f ~/.client_secrets.json ]]; then
    FILES+=(~/.client_secrets.json)
else
    echo "~/.client_secrets.json not found, you must place your credentials manually."
fi

if [[ -f ~/.youtube-upload-credentials.json ]]; then
    FILES+=(~/.youtube-upload-credentials.json)
else
    echo "~/.youtube-upload-credentials.json not found, you must place your credentials manually."
fi

scp "${SSH_OPTS[@]}" ${FILES[@]} "root@${scp_host}:"

# /etc/profile.d/ (not ~/.bashrc) so it's picked up by any login shell,
# including the one ./droplet-login.sh is about to open -- unlike a script
# exporting PATH/vars for its own already-running session (see step2's
# csm-not-in-PATH fix), this genuinely works because upload happens *before*
# the session that needs it starts.
printf 'export CSM_BUCKET=%q\n' "$CSM_BUCKET" | ssh "${SSH_OPTS[@]}" "root@${ssh_host}" "cat > /etc/profile.d/csm-bucket.sh"
echo "CSM_BUCKET=${CSM_BUCKET} will be set automatically once you ./droplet-login.sh."
