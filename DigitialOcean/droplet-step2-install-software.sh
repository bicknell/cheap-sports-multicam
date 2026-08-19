#!/bin/bash

set -e

_start=$(date +%s)
trap 'echo "[$(basename "$0")] elapsed: $(( $(date +%s) - _start ))s"' EXIT

cd ~

# The cheap 2vCPU/4GB testing tier can OOM-kill a real final-h265 encode --
# observed for real (~3.55GB peak RSS against 4GB total, matching DESIGN.md
# Sec 3.3's synthetic-benchmark prediction of 3.66GB for 4K 10-bit x265).
# Add swap so functional testing stays on the cheap tier instead of needing
# a pricier RAM size just to complete a render. Idempotent.
if [[ -z $(swapon --show 2>/dev/null) ]];
then
    fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo "Swap enabled: $(swapon --show)"
else
    echo "Swap already configured, skipping."
fi

# Clone cheap-sports-multicam so we have the csm package. main doesn't have
# it yet (DESIGN.md Sec 15: droplet work lands on csm-rewrite pre-merge).
git clone --branch csm-rewrite https://github.com/bicknell/cheap-sports-multicam.git

apt-get update -qq
apt-get install -y ffmpeg python3-numpy python3-scipy python3-yaml python3-venv s3cmd git time

# DESIGN.md Sec 14.1/14.3: the old from-source ffmpeg build existed solely for
# --enable-libfdk-aac, and commit 71d1585 ("Switch to libaac") removed the
# only reason for it. Check rather than assume the stock package has what we
# need -- fall back to a static build only if it doesn't.
missing_codec=""
for codec in libx265 libsvtav1; do
    if ! ffmpeg -hide_banner -encoders 2>/dev/null | grep -qE "\b${codec}\b"; then
        missing_codec="${missing_codec} ${codec}"
    fi
done

if [[ -n $missing_codec ]];
then
    echo "Stock ffmpeg is missing:${missing_codec} -- falling back to a BtbN static build."
    curl -sL "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz" -o /tmp/ffmpeg-static.tar.xz
    tar -xJf /tmp/ffmpeg-static.tar.xz -C /tmp
    cp /tmp/ffmpeg-master-latest-linux64-gpl/bin/ffmpeg /tmp/ffmpeg-master-latest-linux64-gpl/bin/ffprobe /usr/local/bin/
    echo "Using static build: $(/usr/local/bin/ffmpeg -version | head -1)"
else
    echo "Stock ffmpeg has libx265 and libsvtav1: $(ffmpeg -version | head -1)"
fi

# Install csm itself into its own venv, then symlink the entry point into
# /usr/local/bin -- already on PATH for every shell (login or not) with no
# ~/.bashrc-sourcing dependency, unlike ~/bin (a script's own `export PATH`
# or ~/.bashrc append only takes effect in shells started *after* it runs,
# not the already-open interactive session step3/step4 run in next).
cd ~/cheap-sports-multicam
python3 -m venv ~/.csm-venv
~/.csm-venv/bin/pip install -e .
ln -sf ~/.csm-venv/bin/csm /usr/local/bin/csm

if csm --help > /dev/null 2>&1;
then
    echo "csm installed and runnable."
else
    echo "csm install failed -- 'csm --help' did not run cleanly."
    exit 1
fi
