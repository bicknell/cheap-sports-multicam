# Using Digital Ocean for Hosting and Encoding

The video files are quite large and take a lot of CPU to process.
I've developed a system to offload that to Digital Ocean using their
Spaces to store and serve the video and a Droplet to process the 
video.

## Managing Data in Spaces

### Create a Digital Ocean Space

Use the Digital Ocean console to create a [Space](https://www.digitalocean.com/products/spaces)
to store the video.  Cost is $5/month for 250G of storage and 1TiB of transfer.  The result
will be a space name like:

```
s3://my-space-name.nyc3.digitialoceanspaces.com/
```

### Use s3cmd to Upload

Set up [s3cmd](https://s3tools.org/s3cmd) following instructions for your platform.

If the game footage has been downloaded off the camera into GAMEDIR/camera{1,2,3,4}
then the following command will upload it to the space:

```
s3cmd put GAMEDIR s3://my-space-name --recursive
```

This may take a while.

### Serving from Spaces

If you want to also serve the files via Spaces (e.g. allow http downloads
without credentials) the files need to be made public.  Either add `--acl-public`
on the s3cmd above to make that happen while uploading, or do it after the fact
with a command like this:

```
s3cmd setacl --acl-public --recursive s3://my-space-name/GAMEDIR
```

## Using a Droplet to Encode

### Digital Ocean Setup

Create a Digital Ocean account if you do not have one.  Then:

- [Create a Personal Access Token](https://docs.digitalocean.com/reference/api/create-personal-access-token/)
- [Add SSH Keys to New or Existing Droplets](https://docs.digitalocean.com/products/droplets/how-to/add-ssh-keys/)

The rest of the scripts assume the Access Token is in `DIGITAL_OCEAN_TOKEN`:

```
$ export DIGITAL_OCEAN_TOKEN="thetokenyoudownloaded"

### Install Prereqs

The scripts below need the following installed on your local machine:

- [curl](https://curl.se)
- [jq](https://jqlang.org)
- [bash](https://www.gnu.org/software/bash/)

### Create the Droplet

In this respository is a droplet-spec.json as a sample.  Customize with
your SSH key ID, or get a  new spec by configuring a droplet the way you
want in the UI and using the "CREATE VIA COMMAND LINE" link at the bottom
to get the JSON for it.

> Experimenting with different server types resulted in the following.
> Performance is vastly better on the "Premium Intel" CPUs over the
> regular CPUs, so it tends to be worth the cost to upgrade.
> When using 32 or 64 core servers stock ffmpeg appears to be able to
> use about 25-27 cores maximum.  Using a 32 core box will thus result
> in the absolute shortest time.

Run `./droplet-create.sh` to create a new droplet from the spec.

WARNING: USAGE CHARGES WILL START, YOU WILL BE BILLED HOURLY

### Upload Files

Run `./droplet-upload.sh` to upload the necessary scripting and config to the
droplet.  You will be promited for your SSH key passprhase, if it is set.

NOTE: The script uses the IPv6 address by default.  If your network is not
      IPv6 set `export IPV4ONLY=true`

NOTE: This copies ~/.s3cmd to the droplet which contains your Spaces credentials.

###

Log in to the droplet and run the scripts:

```
./droplet-login.sh
```

On the droplet, run the scripts:

1. `./droplet-step1-update.sh` will update the droplet and most likely have to reboot it.
   - After it has rebooted run `./droplet-login.sh` to get back in before continuing.
2. `./droplet-step2-install-software.sh` installs the necessary software:
   - s3cmd
   - clone the cheap-sports-multicam repository
   - Prereqs to build ffmpeg.
   - Build ffmpeg.
3. `./droplet-step3-download-video.sh GAMEDIR` will download the camera video from your
   Space for processing.
   - This can be started in a second window as soon as s3cmd is installed in the previous
     step.  This helps reduce the total time the droplet is in use.
4. `./droplet-step4--process-video.sh GAMEDIR f1 f2 f3 f4 f5 f6` will run the generate-ffmpeg.py
   script to build the ffmpeg command lines, and will then execute the ffmpeg command lines.
   This process will likely take about 2 hours.  The f1-f6 arguments are the frames to sync,
   see the documentation up a directory.
5. `./droplet-step5-upload-video.sh GAMEDIR` uploads the final video back to the Space.
6. **OPTIONAL**  Upload the video to other sources.  The Droplet comes with a bunch of
   included bandwidth and generally has very good connectivity to the world.
   - [youtube-upload](https://github.com/tokland/youtube-upload) can send the video to YouTube.

Log out of the droplet.

### Destroy the Droplet

Run `./droplet-destroy.sh` to destroy the droplet.

WARNING: ALL DATA ON THE DROPLET WILL BE LOST
WARNING: VERIFY DESTRUCTION IN THE DIGITAL OCEAN UI TO VERIFY BILLING CEASED

