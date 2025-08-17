# cheap-sports-multicam

I wanted a fast, easy way to get multiple camera angles of a soccer game.

## Design Parameters:

- 4 cheap action cameras that can hopefully get two angles of most action.
- Record in 1080P, ideally 60fps.
- Output in 4K, a 4x4 tiled screen of all 4 cameras.

## Physical Parts

- 4 [Akaso Brave 7 LE](https://www.akasotech.com/support/Brave-7-LE) action cameras.
  These are often on sale on Amazon, and I bought mine bundled with 128G SD cards.
  They have sufficient battery life to record an entire full length soccer game with
  margin.  They are also water resistant, and so can be used without an enclosure in
  most conditions.
- 2 [Neewer 4M Light Stands](https://www.amazon.com/dp/B0BTL5XYN6), which were one
  of the more affordable ways to get the cameras up in the air.  Getting the cameras
  up makes a HUGE difference.
- 2 of my 3D printed brackets from Printables to attach the cameras to the tripods.
- 4 of my Akaso Brave 7 LE "GoPro" mount adapters from Printables to avoid using the
  plastic box that covers the microphone.
- Coming soon: A carry box on Printables.

## Software

In this repository is a script that will generate the necessary ffmpeg command line
to generate the output video from the input video.

## How To Use

Record the game.  See my notes below about a full sized soccer field in particular.

Create a directory I will call `GAMEDIR` for the rest of this document.  Create 4
subdirectories under `GAMEDIR` called `camera1`, `camera2`, `camera3`, and `camera4`.
Copy the footage from each camera to each directory.  They will be tiled like this:

```
camera1 camera2 
camera3 camera4
```

Open the first file of cameras 1 and 2 in a playback program which can display frame
numbers.  QuickTime player and VLC both work.  Find a moment in view of both cameras
to use for synchronization.  I like to use the moment a ball hits the ground.  The 
frame number from the first camera is `f1`, and from the second `f2`.

Repeat for cameras 2 and 3 (`f3`, `f4`), and then 3 and 4 (`f5`, `f6`).

Run generate-ffmpeg.py:

```
generate-ffmpeg.py /Path/to/GAMEDIR f1 f2 f3 f4 f5 f6
```

A command line will be generated to stdout.  Inspect it to be sure it does what you
want, then cut and paste into a bash shell terminal.  ffmpeg will run and build
the video as GAMEDIR/combined.mp4

## Soccer Specifc Notes

How I record a soccer game.

- Set one camera at an angle of 0, and the other camera at an angle of 55 degrees.
- Visualize where the extended 18 yard box line would hit the sideline.  Walk 3 paces
  down the side line towards the center of the field, then turn and walk 5 paces away
  from the field.  Set up the tripod.
- I use a 16 degree down angle on the 0 degree camera, and a 18 degree down angle on 
  the 55 degree camera.
- On the Brave 7 LE, I configure as follows:
   - H.265
   - 1080P60
   - Turn off auto-shutdown.
   - Set exposure to auto, and then set EV to -1 as they tend to overexpose.
   - Set the date stamp to appear on the screen.
   - Turn on Wifi and connect with the phone app, this syncs the time from the phone
     to the camera, turn off WiFi.
   - Set the zoom to 1.8.
- Start the cameras and raise the tripod to full height.  Align the 0 degree camera to
  look straight across the field.  The 55 degree camera should be looking roughly
  towards the center of the field.
- Repeat on the opposite end of the field.

