#!/bin/bash

set -e

_start=$(date +%s)
trap 'echo "[$(basename "$0")] elapsed: $(( $(date +%s) - _start ))s"' EXIT

# Not part of the default workflow (see README) -- a disposable droplet that
# lives ~1h and gets destroyed has no persistent attack surface to patch, and
# a full apt upgrade costs real billed minutes (97 packages including
# kernel/grub/systemd measured on a fresh droplet) plus a reboot, and can hit
# an interactive dpkg conffile prompt (observed: openssh-server wants to
# overwrite DO's customized /etc/ssh/sshd_config). Kept as an opt-in script
# for anyone who wants a fully-patched box anyway, made non-interactive and
# defaulting to the existing (DO-customized) conffiles on conflict.
export DEBIAN_FRONTEND=noninteractive
sudo apt update && sudo apt -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" upgrade -y
[ -f /var/run/reboot-required ] && shutdown -r now "rebooting after update"
