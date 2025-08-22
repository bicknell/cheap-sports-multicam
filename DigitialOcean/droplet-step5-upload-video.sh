#!/bin/bash

if [ "$#" -ne 1 ]; then
    echo "Error: directory not specified"
    echo "Usage: $0 <bucket-directory>"
    exit 1
fi

s3cmd put $1/final_video.mp4 s3://loudoun-25-26-2012g-silver/$1/

