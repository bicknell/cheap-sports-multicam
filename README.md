# cheap-sports-multicam

I wanted a fast, cheap, easy way to get multiple camera angles of a soccer game
with minimal post-processing effort.  My solution consists of three parts:

1. Purchased components, documented in [Purchased](#Purchased).
2. 3D printed components designed by me for this project and hosted on
   [Printables](https://www.printables.com) under [My Profile](https://www.printables.com/@LeoBicknell_3382814).
3. The software in this repository to assist in post processinging.

My Design Parameters:

- 4 cheap action cameras that can hopefully get two angles of most action.
- Record in 1080P30 so the resulting video files are not too large.
- Output in 4K30, as 2x2 tiled screen of all 4 cameras.
- Programatic post-processing.
- Get the cameras up in the air for a better view.

***This software should be considered BETA quality at this time.  This is the first season
I have used it, and frequent improvements are being tested.  Feedback from others is
welcome.***

## Bill of Materials

### Purchased

- 4 Action Cameras.  I am currently using 
  [Akaso Brave 7 LE](https://www.akasotech.com/support/Brave-7-LE) cameras.
  **Please read** [cameras.md](cameras.md) before purchasing cameras.
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

The essential steps are:

1. Concatinaing the multiple files from the camera into a single video.
1. Trimming the start of 3 of the 4 videos so that they start at the same
   moment in time.
1. Combining the videos into a 2x2 matrix.

[ffmpeg](https://ffmpeg.org) is used to perform the video processing.  The
latest version is highly recommended.

The python scripts (requires python3 and pyyaml) in this repository generate
the necessary ffmpeg command lines to perform the processing.

## My Workflow

1. Record the game.  Tips/tricks/experiences with specific sports:
   - [Soccer](soccer.md)
1. Create a directory referenced as `GAMEDIR` for the rest of this document.  Create 4
   subdirectories under `GAMEDIR` called `camera1`, `camera2`, `camera3`, and `camera4`.
   Copy the footage from each camera to each directory.  They will be tiled like this:

   ```
   camera1 camera2 
   camera3 camera4
   ```
1. Copy the footage from each camera into the matching directory.
1. Manually syncronize the video:
   - Open the first file of cameras 1 and 2 in a playback program which can display frame
     numbers.  QuickTime player and VLC both work.  
   - Find a moment in view of both cameras to use for synchronization.  The moment a ball
     hits the ground, the moment someone lands from jumping, things like that.
   - Record the frame number from the first camera (`f1` below), and from the second (`f2` below).
   - Repeat these steps for cameras 2 to 3 (`f3`, `f4`), and 3 to 4 (`f5`, `f6`).
1. **Optional:** To add metadata, create a file called `GAMEDIR/metadata.yaml`.  Each
   line should be a string to string mapping of a value in the
   [Public Metadata API](https://ffmpeg.org/doxygen/7.0/group__metadata__api.html)
1. Run [generate-ffmpeg.py](generate-ffmpeg.py):

   ```
   generate-ffmpeg.py /Path/to/GAMEDIR f1 f2 f3 f4 f5 f6 > /tmp/runme.sh
   ```

   A command line will be generated into /tmnp/runme.sh.  Inspect the script, and if necessary
   modify the results for any custom processing you need to do on a specific game.
1. Run `bash /tmp/runme.sh` to star the processing.  When done the final video will be
   in `GAMEDIR/final_video.mp4`.

**WARNING:** These video files are extremely large.  Expect a 90 minute soccer game to be
around 5G per camera, or 20G of total video.  The resulting output file of them
tiled together will be 10-14G.  Even on a fast box processing may take 2x-4x realtime.

See [DigitalOceanVideo](DigitalOceanVideo/) for a workflow to store the files in a Digital
Ocean Space and process them on a Digitial Ocean Droplet.

## Challenges

These cheap cameras are not time syncronized like professional cameras.  The methods I
have used to synchronize them above are really only about +-30ms.  Visually it tends to
be fine, but the audio can be more noticeable.  Additionally, some cameras drop or add
frames when they roll over to a new file which puts them futher off sync later in the
video stream.

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
