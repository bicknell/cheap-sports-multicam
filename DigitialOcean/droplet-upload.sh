#!/bin/bash

if [[ -z $IPV4ONLY ]];
then
    if [[ -f droplet.ipv6 ]];
    then
        ip="[$(cat droplet.ipv6)]"
    fi
else
    if [[ -f droplet.ipv4 ]];
    then
        ip=$(cat droplet.ipv4)
    fi
fi

if [[ -z $ip ]];
then
    echo "Unable to determine IP address."
    exit 1
fi

FILES=(droplet-step*)

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

scp ${FILES[@]} "root@${ip}:"
