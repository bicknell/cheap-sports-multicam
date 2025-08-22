#!/bin/bash

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

curl -X POST -H 'Content-Type: application/json' \
    -H "Authorization: Bearer ${DIGITAL_OCEAN_TOKEN}"
    -d @droplet-spec.json \
    https://api.digitalocean.com/v2/droplets > droplet-info.json


new_droplet_id=$(jq .droplet.id droplet-info.json)

curl -X GET \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${DIGITAL_OCEAN_TOKEN}" \
    "https://api.digitalocean.com/v2/droplets/${new_droplet_id}" > droplet-details.json

new_droplet_ipv4=$(jq .droplet.networks.v4[].ip_address droplet-details.json)
new_droplet_ipv6=$(jq .droplet.networks.v6[].ip_address droplet-details.json)

echo "$new_droplet_ipv4" > droplet.ipv4
echo "$new_droplet_ipv6" > droplet.ipv6

