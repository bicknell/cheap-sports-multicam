#!/bin/bash

if [ "$#" -ne 7 ]; then
    echo "Error: Must specify directory and frame numbers"
    echo "Usage: $0 <bucket-directory> f1 f2 f2 f3 f3 f4"
    exit 1
fi

TEMP_FILE=$(mktemp /tmp/cheap-sports-multicam.XXXXXX)

./cheap-sports-multicam/generate-ffmpeg.py $1 $2 $3 $4 $5 $6 $7 > $TEMP_FILE

bash $TEMP_FILE
