#!/bin/bash

if [[ -z $IPV4ONLY ]];
then
    if [[ -f droplet.ipv6 ]];
    then
        ip="$(cat droplet.ipv6)"
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

ssh "root@${ip}"
