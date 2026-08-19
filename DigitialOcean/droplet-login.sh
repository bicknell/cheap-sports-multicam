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

# Every droplet gets a fresh host key on create, and this one is destroyed
# in hours -- skip the interactive fingerprint prompt and don't persist the
# key to ~/.ssh/known_hosts, where it would be pure clutter (or worse, a
# scary "REMOTE HOST IDENTIFICATION HAS CHANGED" warning for a future
# unrelated droplet that happens to reuse the same IP).
ssh -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null "root@${ip}"
