#!/bin/bash

set -e

if [ -f droplet-info.json ];
then
    echo "droplet-info.json exists, do you already have a droplet running?"
    exit 1
fi
if [ -f droplet-details.json ];
then
    echo "droplet-details.json exists, do you already have a droplet running?"
    exit 1
fi

if [[ -z $DIGITAL_OCEAN_TOKEN ]];
then
    echo "Must set DIGITAL_OCEAN_TOKEN."
    exit 1
fi

spec_file="droplet-spec.json"
if [ -f droplet-spec.local.json ];
then
    spec_file="droplet-spec.local.json"
fi
echo "Using ${spec_file}."

# DROPLET_SIZE/DROPLET_REGION, if set, override the spec file's own
# size/region for this one request without touching the file on disk --
# lets droplet-run.sh inject a dynamically-chosen size/region (see
# droplet-find-size.sh) while manual standalone runs keep using whatever's
# actually in the spec file, unchanged.
spec_json=$(cat "$spec_file")
if [[ -n $DROPLET_SIZE ]];
then
    echo "Overriding size -> ${DROPLET_SIZE}"
    spec_json=$(echo "$spec_json" | jq --arg size "$DROPLET_SIZE" '.size = $size')
fi
if [[ -n $DROPLET_REGION ]];
then
    echo "Overriding region -> ${DROPLET_REGION}"
    spec_json=$(echo "$spec_json" | jq --arg region "$DROPLET_REGION" '.region = $region')
fi

echo "Creating droplet."
curl -s -X POST -H 'Content-Type: application/json' \
    -H "Authorization: Bearer ${DIGITAL_OCEAN_TOKEN}" \
    -d "$spec_json" \
    https://api.digitalocean.com/v2/droplets > droplet-info.json

new_droplet_id=$(jq -r '.droplet.id' droplet-info.json)

if [[ -z $new_droplet_id || $new_droplet_id == "null" ]];
then
    echo "Create request did not return a droplet id. Response:"
    cat droplet-info.json
    rm -f droplet-info.json
    exit 1
fi

echo "Waiting for droplet ${new_droplet_id} to become active..."
max_wait_s=180
poll_interval_s=5
elapsed_s=0
status=""
public_ipv4=""

while [[ $elapsed_s -lt $max_wait_s ]];
do
    curl -s -X GET \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer ${DIGITAL_OCEAN_TOKEN}" \
        "https://api.digitalocean.com/v2/droplets/${new_droplet_id}" > droplet-details.json

    status=$(jq -r '.droplet.status' droplet-details.json)
    public_ipv4=$(jq -r '.droplet.networks.v4[]? | select(.type=="public") | .ip_address' droplet-details.json)

    if [[ $status == "active" && -n $public_ipv4 ]];
    then
        break
    fi

    sleep "${poll_interval_s}"
    elapsed_s=$((elapsed_s + poll_interval_s))
done

if [[ $status != "active" || -z $public_ipv4 ]];
then
    echo "Droplet ${new_droplet_id} did not become active with a public IP within ${max_wait_s}s (status: ${status})."
    echo "It may still be billing -- check the DigitalOcean UI, and run ./droplet-destroy.sh if needed."
    exit 1
fi

public_ipv6=$(jq -r '.droplet.networks.v6[]? | select(.type=="public") | .ip_address' droplet-details.json)

echo "Writing out IP addresses."
echo "$public_ipv4" > droplet.ipv4
echo "$public_ipv6" > droplet.ipv6
date -u +%s > droplet.created_at

# The DO API reports status=active as soon as the VM is up, well before
# sshd has finished starting -- droplet-upload.sh/droplet-login.sh will hit
# "Connection refused" if they run immediately after this script returns.
# Wait for port 22 to actually answer (raw TCP check via bash's /dev/tcp,
# not a full SSH handshake, so it doesn't depend on key/agent setup).
if [[ -z $IPV4ONLY ]];
then
    ssh_check_ip="$public_ipv6"
else
    ssh_check_ip="$public_ipv4"
fi

echo "Waiting for sshd on ${ssh_check_ip}..."
ssh_max_wait_s=120
ssh_poll_interval_s=5
ssh_elapsed_s=0
ssh_ready=false

while [[ $ssh_elapsed_s -lt $ssh_max_wait_s ]];
do
    if (exec 3<>"/dev/tcp/${ssh_check_ip}/22") 2>/dev/null;
    then
        exec 3<&- 2>/dev/null
        exec 3>&- 2>/dev/null
        ssh_ready=true
        break
    fi
    sleep "${ssh_poll_interval_s}"
    ssh_elapsed_s=$((ssh_elapsed_s + ssh_poll_interval_s))
done

if [[ $ssh_ready != true ]];
then
    echo "sshd still not answering after ${ssh_max_wait_s}s -- it may just need more time."
    echo "Try ./droplet-upload.sh or ./droplet-login.sh again shortly."
fi

echo "Droplet ${new_droplet_id} active at ${public_ipv4} (billing started $(date -u +%FT%TZ))."
