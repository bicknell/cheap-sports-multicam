#!/bin/bash

set -e

_start=$(date +%s)
trap 'echo "[$(basename "$0")] elapsed: $(( $(date +%s) - _start ))s"' EXIT

if [ "$#" -ne 1 ]; then
    echo "Error: directory not specified"
    echo "Usage: $0 <bucket-directory>"
    exit 1
fi

if [[ -z $CSM_BUCKET ]]; then
    echo "Must set CSM_BUCKET (e.g. export CSM_BUCKET=loudoun-25-26-2012g-silver)."
    exit 1
fi

s3cmd put "$1"/final_video.mp4 "s3://${CSM_BUCKET}/$1/"
