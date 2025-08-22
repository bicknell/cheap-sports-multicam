#!/bin/bash

curl -X DELETE \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${DIGITAL_OCEAN_TOKEN}"
  "https://api.digitalocean.com/v2/droplets/video-encoding"

for file in droplet-info.json droplet-details.json droplet.ipv4 droplet.ipv6
do
    if [ -f "${file}" ];
    then
        rm "${file}"
    fi
done
