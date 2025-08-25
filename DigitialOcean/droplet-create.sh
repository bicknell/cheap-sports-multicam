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

if [[ -z $DIGITAL_OCEAN_TOKEN ]];
then
    echo "Must set DIGITAL_OCEAN_TOKEN."
    exit 1
fi

echo "Creating droplet."
curl -s -X POST -H 'Content-Type: application/json' \
    -H "Authorization: Bearer ${DIGITAL_OCEAN_TOKEN}" \
    -d @droplet-spec.json \
    https://api.digitalocean.com/v2/droplets > droplet-info.json

echo "Waiting for droplet..."
sleep 15

new_droplet_id=$(jq .droplet.id droplet-info.json)

echo "Retrieving droplet details."
curl -s -X GET \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${DIGITAL_OCEAN_TOKEN}" \
    "https://api.digitalocean.com/v2/droplets/${new_droplet_id}" > droplet-details.json

####
#### NOT IN FILE
####
new_droplet_ipv4=$(jq -r '.droplets[].networks.v4[] | select(.type=="public") | .ip_address' droplet-details.json)
new_droplet_ipv6=$(jq -r '.droplets[].networks.v6[] | select(.type=="public") | .ip_address' droplet-details.json)

echo "Writing out IP addresses."
echo "$new_droplet_ipv4" > droplet.ipv4
echo "$new_droplet_ipv6" > droplet.ipv6

