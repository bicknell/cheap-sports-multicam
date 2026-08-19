#!/bin/bash

set -e

_start=$(date +%s)
trap 'echo "[$(basename "$0")] elapsed: $(( $(date +%s) - _start ))s"' EXIT

sudo apt update && sudo apt upgrade -y
[ -f /var/run/reboot-required ] && shutdown -r now "rebooting after update"
