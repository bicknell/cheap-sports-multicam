#!/bin/bash

set -e

if [[ -z $DIGITAL_OCEAN_TOKEN ]];
then
    echo "Must set DIGITAL_OCEAN_TOKEN."
    exit 1
fi

if [ ! -f droplet-info.json ];
then
    echo "droplet-info.json not found -- no local record of a droplet to destroy."
    echo "If one exists, delete it manually with:"
    echo "  curl -X DELETE -H \"Authorization: Bearer \$DIGITAL_OCEAN_TOKEN\" https://api.digitalocean.com/v2/droplets/<id>"
    exit 1
fi

droplet_id=$(jq -r '.droplet.id' droplet-info.json)

if [[ -z $droplet_id || $droplet_id == "null" ]];
then
    echo "Could not read a droplet id out of droplet-info.json."
    exit 1
fi

echo "Destroying droplet ${droplet_id}."
http_status=$(curl -s -o /dev/stderr -w '%{http_code}' -X DELETE \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${DIGITAL_OCEAN_TOKEN}" \
    "https://api.digitalocean.com/v2/droplets/${droplet_id}")

if [[ $http_status != "204" ]];
then
    echo "Destroy request failed (HTTP ${http_status}). Droplet ${droplet_id} may still be running and billing."
    echo "Verify in the DigitalOcean UI before assuming it is gone."
    exit 1
fi

echo "Droplet ${droplet_id} destroyed. Verify in the DigitalOcean UI that billing has stopped."

if [ -f droplet.created_at ];
then
    created_at=$(cat droplet.created_at)
    elapsed_s=$(( $(date -u +%s) - created_at ))
    billed_hours=$(( (elapsed_s + 3599) / 3600 ))
    echo "Lifetime: ${elapsed_s}s (~$((elapsed_s / 60))m). Billed hours: ${billed_hours}."
fi

for file in droplet-info.json droplet-details.json droplet.ipv4 droplet.ipv6 droplet.created_at
do
    if [ -f "${file}" ];
    then
        rm "${file}"
    fi
done
