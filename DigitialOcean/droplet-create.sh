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

echo "Creating droplet."
curl -s -X POST -H 'Content-Type: application/json' \
    -H "Authorization: Bearer ${DIGITAL_OCEAN_TOKEN}" \
    -d @"${spec_file}" \
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

echo "Droplet ${new_droplet_id} active at ${public_ipv4} (billing started $(date -u +%FT%TZ))."
