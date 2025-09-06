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