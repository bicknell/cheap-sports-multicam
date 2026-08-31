# cheap-sports-multicam

Software to process 4 cheap "GoPro" style cameras into a 2x2 tiled video of an
entire soccer field for coaching and educational purposes.

## History
There are a number of commerically available "tracking cameras" to video a
soccer game.  These solutions get a nice image of the action on the field,
but are also quite expensive.

I wanted a fast, cheap, easy way to get multiple camera angles of a soccer game
with minimal post-processing effort.  My solution consists of three parts:

1. Purchased components, documented in [Purchased](#Purchased).
2. 3D printed components designed by me for this project and hosted on
   [Printables](https://www.printables.com) under [My Profile](https://www.printables.com/@LeoBicknell_3382814).
3. The software in this repository to assist in post processing.

My Initial Design Parameters:

- 4 cheap action cameras that can hopefully get two angles of most action.
- Record in 1080P30 so the resulting video files are not too large.
- Output in 4K30, as 2x2 tiled screen of all 4 cameras.
- Programatic post-processing.
- Get the cameras up in the air for a better view.

***This software should be considered BETA quality at this time.  This is
the second season I have used it and it received a major rewrite from the
previous season.  Feedback from others is welcome.***

## Bill of Materials

### Purchased

- 4 Action Cameras.  I am currently using 
  [Akaso Brave 7](https://www.akasotech.com/brave-7) cameras.
  **Please read** [docs/cameras.md](docs/cameras.md) before purchasing cameras.
  - The parts are built around a "GoPro" style mount, so pretty much any action cam
    should work.
- 2 [Neewer 4M Light Stands](https://www.amazon.com/dp/B0BTL5XYN6), which were one
  of the more affordable ways to get the cameras up in the air.  Getting the cameras
  up makes a HUGE difference.
- 2 [Mordx Canopy Weights](https://www.amazon.com/dp/B0CZ6JP45B), sandbags to steady
  the light stands in the wind.  They do not need to be full, I use 4 lbs in each
  compartment, 8 pounds per tripod leg.  6 can be filled from a 50lb bag.

### 3D Printed

- 2 sets of 
  [Neewer 4M Light Stands Dual "GoPro" Mount](https://www.printables.com/model/1387522-neewer-4m-light-stands-dual-gopro-mount)
  brackets.  Allows mounting 2 cameras to a single Neewer 4M Tripod.
- 4 [Akaso Brave 7 LE "GoPro" Mount](https://www.printables.com/model/1387497-akaso-brave-7-le-gopro-mount) 
  brackets - optional.  These allow using the cameras without the 
  plastic box, which in turn allows the microphones to be functional.
- Coming soon! A carry box on Printables.

## Software

The software should work on any modern unix-like operating system.

[ffmpeg](https://ffmpeg.org) (7.0+, but see the warning below) does the actual
video processing.  Please install it via the correct mechanism for your 
operating system.

This repository builds `csm`, a Python tool, which figures out how
each camera lines up in time and generates the right ffmpeg command for you.
Quickstart, with every default left as-is:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

export GAMEDIR=/Path/To/Game/Directory

csm probe   "$GAMEDIR"              # sanity-check the footage
csm sync    "$GAMEDIR" --write      # detect sync offsets, writes GAMEDIR/sync.yaml
csm preview "$GAMEDIR"              # OPTIONAL: render a 60s sample first, watch it
csm render  "$GAMEDIR"              # writes GAMEDIR/final_video.mp4
```

**⚠️ ffmpeg 9.0.1 silently truncates audio on long renders** (confirmed via
live reproduction 2026-08-30): unbounded memory growth once encoding starts,
video completes normally, audio just stops partway through -- nothing
crashes, so it's easy to ship a broken render without noticing. `csm` warns
at runtime if it detects ffmpeg ≥8.0, but until 8.x is retested, **use ffmpeg
7.1.x** (confirmed clean, e.g. `brew install ffmpeg@7`, pass
`--ffmpeg`/`--ffprobe` at its keg-only binaries) and always check a finished
render's actual audio duration, not just that the process exited 0. See
`docs/DESIGN.md` Sec 11 ("ffmpeg 9.0.1 audio-truncation bug") for the full
writeup.

`GAMEDIR` is a directory with `camera1`/`camera2`/`camera3`/`camera4`
subdirectories, each holding that camera's footage — see
[docs/REFERENCE.md](docs/REFERENCE.md) for the full layout. That document
covers everything past the quickstart: every command's flags, how to run a
quality sweep against your own footage before committing to hours of
encoding, and what to do if automatic sync can't find the sync point on its
own. `csm/SYNCHRONIZATION.md` explains how the automatic sync actually works,
if you're curious.

The old `legacy/generate-ffmpeg.py` script (manual frame-number sync, no
automatic detection) still exists and still works, in case you hit a `csm`
bug mid-season and need a fallback — but it isn't the recommended path
anymore.

## My Workflow

1. Record the game.  Tips/tricks/experiences with specific sports:
   - [Soccer](docs/soccer.md)
1. Create a directory referenced as `GAMEDIR` for the rest of this document.  Create 4
   subdirectories under `GAMEDIR` called `camera1`, `camera2`, `camera3`, and `camera4`.
   Copy the footage from each camera to each directory.  They will be tiled like this:

   ```
   camera1 camera2 
   camera3 camera4
   ```
1. **Optional:** To add metadata, create a file called `GAMEDIR/metadata.yaml`.  Each
   line should be a string to string mapping of a value in the
   [Public Metadata API](https://ffmpeg.org/doxygen/7.0/group__metadata__api.html)

   A warning will be printed if you do not create a metadata.yaml file to help prevent
   forgetting one.  It can be ignored if you really intend to have no metadata.

1. Run the tool, per the [Software](#software) quickstart above:

   ```bash
   export GAMEDIR=/Path/To/Game/Directory
   
   vi $GAMEDIR/metadata.yaml
   
   # Populate metadata.yaml with any ffmpeg metadata fields.
   
   open $GAMEDIR/camera1/*.mp4
   
   # View the first camera in your favorite tool and generate the offset at which
   # to start and the duration of the final video.
   export TIMES=(--start 1@06:37 --duration 65:00)
   
   csm probe   "$GAMEDIR" --frame --frame-at 90 # Write out frames from each camera
   open "$GAMEDIR/.csm/frames/*"                # View them to determine cutoff lines
   
   # The video encoding will greately degrade the quality of anything above
   # these pixel lines from the BOTTOM of the frame to reduce file size.  I typically
   # set these to most of the sky above the highest point I want to be clear.
   
   export FIELD_CUTOFF=(--field-cutoff 1=930 --field-cutoff 2=930 --field-cutoff 3=930 --field-cutoff 4=980)
   
   csm sync    "$GAMEDIR" --write               # Synchronize the video based on audio
   
   csm preview "$GAMEDIR" "${TIMES[@]}" "${FIELD_CUTOFF[@]}" \
    --preview-seconds 30 --preview-at 0% --preview-at middle --preview-at 100%
   
   open "$GAMEDIR/.csm/preview/*"               # Review the previews for QA
    
   csm render "$GAMEDIR" "${TIMES[@]}" "${FIELD_CUTOFF[@]}"
   ```

   The final video ends up at `GAMEDIR/final_video.mp4`. See
   [docs/REFERENCE.md](docs/REFERENCE.md) for every flag, and its "when automatic sync
   can't find the sync point" section if `csm sync` reports a disconnected camera or a
   low-confidence link.

**WARNING:** These video files are extremely large.  Expect a ~90 minute soccer game to be
around **~76 GB of raw footage across all four cameras** (measured, not estimated — see
`docs/DESIGN.md` Sec 2.1). The default 3840×2160 output comes out around 15-30 GB depending
on the settings you land on after running a quality sweep (`csm preview GAMEDIR --sweep
crf=...` — see `docs/REFERENCE.md` Sec 4). A full render can take several times realtime
even on capable desktop hardware.

See [DigitialOcean](DigitialOcean/) for a workflow to store the files in a Digital
Ocean Space and process them on a Digital Ocean Droplet.

## Challenges

These cheap cameras are not time syncronized like professional cameras. `csm sync`'s
automatic audio-correlation approach gets within about a frame of true sync (validated
against known-good ground truth — see `docs/DESIGN.md` Sec 2.4-2.5 and
`csm/SYNCHRONIZATION.md`), well better than the old manual frame-scrubbing method's
±30ms. Additionally, some cameras drop or add frames when they roll over to a new file
which puts them futher off sync later in the video stream — sync detection doesn't fix
this, since it only measures the offset at the start.

Here's tips and tricks and works in progress:

- Place cameras on the "team" side of the field, not the "parents" side.  This greatly
  reduces the chance of a person talking right under the camera and dominating the
  audio.
- The current code only uses 2 of the 4 mono microphones, with the main goal to be able
  to hear a whistle.  It is possible to mix all 4, or only use one.  The quality will
  depend a lot on the microphones on the camera.
- Professionals would set up shutgun microphones around the field and record those for
  the sound.  It would be possible to do a separate audio recording and use that 
  for the audio track, but it greatly complicates the post-processing.
- Cameras need enough down-tilt that most of the sky is out of frame.  This helps with
  exposure, but also gets more of the pixels used for action.
- When cameras drop frames on file switch it is possible to insert a short buffer, either
  of the last frame of the previous file repeated or just black to sync up to the other cameras.
  This hasn't been coded because to do this properly requires fairly complex changes
  to the fitlergraph, or re-encoding the whole input camera stream.  I'm trying to
  find a better way.
- When cameras duplicate frames on file switch it's easier.  The source file can be 
  -c copy with a -ss 0.5 (skip 0.5 seconds, or 15 frames) to a new file prior to feeding
  it into the program.
- Cameras that write timecode, like the GoPro Hero 12/13 should allow syncing the video
  by that timecode, but I don't know how to do that when tiling video with ffmpeg.
- Some cameras may exhibit better behavior if the duration of a file is reduced.  Akaso
  has recommended that "loop mode" set to 5 or 10 minutes is better than letting the
  camera auto roll over at 25 minutes.  So far that has not been my experience, but
  I am still working with their support.  Other cameras may be different.
