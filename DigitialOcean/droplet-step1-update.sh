#!/bin/bash

set -e

sudo apt update && sudo apt upgrade -y
[ -f /var/run/reboot-required ] && shutdown -r now "rebooting after update"
