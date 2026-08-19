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

WARNING: If you enable the "CDN" option on your space, beware of the following.
         The first download will cost 1x bandwidth to transfer from the space
         to the CDN, and then 1x bandwidth from the CDN to the user.  If you
         plan to only download once it doubles your bandwidth for no reason.
         Also, if you use the feature to share a timed access URL with a user
         and have CDN enabled you will pay 2x for EVERY ACCESS, because the
         same thing happens and it can't be cached.  Only enable the CDN if
         you're going to be using it to serve up many, many copies of the same
         public file.

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
```

Run `ssh-add` for your droplet key before starting (`ssh-add ~/.ssh/id_whatever`) so you're
only prompted for its passphrase once, instead of once per `scp`/`ssh` call across
`droplet-upload.sh`/`droplet-login.sh`.

### Install Prereqs

The scripts below need the following installed on your local machine:

- [curl](https://curl.se)
- [jq](https://jqlang.org)
- [bash](https://www.gnu.org/software/bash/)

### Create the Droplet

In this repository is `droplet-spec.json` as a sample. Copy it to
`droplet-spec.local.json` (gitignored, so a real SSH key ID never gets committed) and
customize, or get a new spec by configuring a droplet the way you want in the UI and using
the "CREATE VIA COMMAND LINE" link at the bottom to get the JSON for it.
`droplet-create.sh` prefers `droplet-spec.local.json` when present.

DO only offers LTS releases as base images -- check `GET /v2/images?type=distribution` for
the current slugs (e.g. `ubuntu-26-04-x64`) rather than assuming a specific interim release
exists.

> Experimenting with different server types resulted in the following.
> Performance is vastly better on the "Premium Intel" CPUs over the
> regular CPUs, so it tends to be worth the cost to upgrade.
> When using 32 or 64 core servers stock ffmpeg appears to be able to
> use about 25-27 cores maximum.  Using a 32 core box will thus result
> in the absolute shortest time.

Run `./droplet-create.sh` to create a new droplet from the spec.

WARNING: USAGE CHARGES WILL START, YOU WILL BE BILLED HOURLY

### Upload Files

Set `CSM_BUCKET` to your Space's bucket name (e.g. `export CSM_BUCKET=my-space-name`), then
run `./droplet-upload.sh` to upload the necessary scripting and config to the droplet. It
also writes `CSM_BUCKET` into `/etc/profile.d/` on the droplet, so it's already set in the
shell `./droplet-login.sh` opens next -- no need to `export` it again on the droplet.

NOTE: The script uses the IPv6 address by default.  If your network is not
      IPv6 set `export IPV4ONLY=true`

NOTE: This copies ~/.s3cmd to the droplet which contains your Spaces credentials.

###

Log in to the droplet and run the scripts:

```
./droplet-login.sh
```

On the droplet, run:

1. `./droplet-step2-install-software.sh` installs the necessary software:
   - ffmpeg, s3cmd, and the Python packages `csm` needs, from stock Ubuntu packages
     (falls back to a static ffmpeg build only if the stock package lacks `libx265`/`libsvtav1`)
   - clones the cheap-sports-multicam repository and installs `csm` from it into its own venv
2. `./droplet-step3-download-video.sh GAMEDIR` will download the camera video from your
   Space for processing.
   - This can be started in a second window as soon as s3cmd is installed in the previous
     step.  This helps reduce the total time the droplet is in use.
3. `./droplet-step4-process.video.sh GAMEDIR [flags...] [-- render-only flags...]` runs
   `csm probe` / `csm sync --write` / `csm render` in sequence, printing the render's
   `--dry-run` output to `GAMEDIR/.csm/render.sh` as an audit trail before actually
   rendering. Flags before `--` are forwarded to both `csm sync` and `csm render` (e.g.
   `--offset`, `--nudge`); flags after `--` go to `csm render` only -- required for
   render-only flags like `--start`/`--duration`/`--out-size`/`--profile`, which `csm sync`
   does not recognize and would reject. See `../docs/REFERENCE.md` for the full flag list.
4. `./droplet-step5-upload-video.sh GAMEDIR` uploads the final video (`GAMEDIR/final_video.mp4`)
   back to the Space.
5. **OPTIONAL**  Upload the video to other sources.  The Droplet comes with a bunch of
   included bandwidth and generally has very good connectivity to the world.
   - [youtube-upload](https://github.com/tokland/youtube-upload) can send the video to YouTube.

Log out of the droplet.

**`droplet-step1-update.sh` is not part of this sequence.** A disposable droplet that lives
about an hour and gets destroyed has no persistent attack surface worth patching -- a full
`apt upgrade` costs real billed minutes (97 packages including kernel/grub/systemd measured
on a fresh droplet) plus a reboot, and can hit an interactive dpkg conffile prompt (observed:
openssh-server wanting to overwrite DO's customized `/etc/ssh/sshd_config`). Run it by hand
only if you specifically want a fully-patched box; it's non-interactive now
(`DEBIAN_FRONTEND=noninteractive`, keeps existing conffiles on conflict), but it will still
reboot the droplet, so re-run `./droplet-login.sh` afterward.

### Destroy the Droplet

Run `./droplet-destroy.sh` to destroy the droplet.

WARNING: ALL DATA ON THE DROPLET WILL BE LOST
WARNING: VERIFY DESTRUCTION IN THE DIGITAL OCEAN UI TO VERIFY BILLING CEASED

### Fully automated

Everything above in one command, no user input required once it starts:

```
./droplet-run.sh [--keep-on-failure] GAMEDIR [csm sync/render flags...]
```

Needs `DIGITAL_OCEAN_TOKEN`, `CSM_BUCKET`, and a key already loaded in `ssh-agent` (it checks
`ssh-add -l` up front and tells you to run `ssh-add` if not, rather than spending money before
finding out). Runs `droplet-find-size.sh` (picks the size/region, see below) →
`droplet-create.sh` → `droplet-upload.sh` → steps 2-5 unattended (via `droplet-all-steps.sh`,
over a direct non-interactive `ssh` call -- `droplet-login.sh` itself is untouched, still
useful for manual debugging) → `droplet-destroy.sh`.

**Size/region are chosen automatically, not read from `droplet-spec.local.json`.** Before
creating anything, `droplet-run.sh` runs `droplet-find-size.sh` against the priority list in
`droplet-size-priority.txt` (one slug per line, best first, `#` comments allowed -- edit
freely, no code changes needed) and picks the first slug available anywhere in
nyc1/nyc2/nyc3, preferring nyc3 as a tiebreaker (doesn't affect cost -- see below -- just a
mild default). The winning slug/region override `droplet-spec.local.json`'s own `size`/
`region` for that one request via `DROPLET_SIZE`/`DROPLET_REGION` env vars, without touching
the file on disk; a manual `./droplet-create.sh` run (outside `droplet-run.sh`) still uses
whatever's actually in the spec file, unchanged. If nothing on the list is available
anywhere, it fails before creating a droplet, not after.

Run `droplet-find-size.sh` standalone any time to just see the table without creating
anything: `./droplet-find-size.sh SLUG1 SLUG2 ...` -- prints availability (nyc1/nyc2/nyc3)
and price (`$/hr`, `$/core/hr`) for each slug, then its recommendation.

NYC1/NYC2/NYC3 are one DigitalOcean "region group" -- Spaces transfer between any of them is
free over the internal network (given the default VPC-local DNS resolver, which nothing here
changes), not just same-datacenter. So the nyc3 preference above is just a tiebreaker, not a
cost optimization; region choice among the three doesn't otherwise matter for this workload.

**Default is to always destroy when the run finishes, success or failure** -- a droplet
nobody destroys is the expensive failure (~$950/month at $1.30/hr if forgotten). Pass
`--keep-on-failure` to leave a failed droplet running instead, for debugging a particularly
tricky problem; remember to `./droplet-destroy.sh` yourself when done with it.

The whole SSH session is streamed to your terminal live *and* saved to
`logs/droplet-run-GAMEDIR-TIMESTAMP.log` (gitignored) -- captured locally rather than
uploaded from the droplet, since `s3cmd` itself is only installed partway through step2; an
early failure would have no way to upload its own log.

Not included: `droplet-step1-update.sh` (see above -- can't survive its own reboot inside an
unattended chain anyway) and `droplet-step6-youtube.sh` (needs a real rewrite to run
unattended instead of printing a fill-in-the-blanks command; deferred).

