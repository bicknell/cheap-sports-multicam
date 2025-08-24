#!/bin/bash

set -e

apt-get install -y python3 python-is-python3
pip3 install --upgrade google-api-python-client oauth2client progressbar2

cd ~
git clone https://github.com/tokland/youtube-upload.git
cd youtube-upload/
python3 setup.py install

cat << EOF
Documentation is at https://github.com/tokland/youtube-upload

Place my_client_secrets.json and my_credentials.json in this diretory.
Command line will be something like:
./youtube-upload \
    --privacy unlisted \
    --location latitude=VAL,longitude=VAL \
    --publish-at YYYY-MM-DDThh:mm:ss.sZ \
    --title "2025-08-20 Team A vrs Team" \
    --description="Wednesday August 20th 2025 7:00PM Team A vrs Team B at Foo park." \
    --recording-date="2025-08-20T11:00:00.0Z" \
    --default-language="en" \
    --default-audio-language="en" \
    --client-secrets="my_client_secrets.json" \
    --playlist="My Team 25-26"
EOF
