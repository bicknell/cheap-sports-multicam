# Camera Quirks

This page documents quirks about cameras that may affact the video output.

## Akaso Brave 7 LE

1. When the camera rolls over to a new file sometimes the 3rd frame is repeated
   twice.  Firmware 1.3.D\_T, reported to Akaso.

   Workaround: None at this time, effect is usually minimal.

1. When the camera rolls over to a new file sometimes ~15 frames are dropped.
   This appears to happen approximately 1 in 8 files.  Firmware 1.3.D\_T.

   Workaround: 

   Grab the last frame of a video: 

        ffmpeg -sseof -3 -i inputfile.MP4 -update 1 -q:v 1 last.jpg

   This command seeks to -3 seconds from the end of the clip and writes each frame to last.jpg.
   The -update 1 tells it to overwrite each file, so when complete there is only one last.jpg
   with the final frame in it.

   Make a short clip using the frame: 

        ffmpeg -loop 1 -i last.jpg -c:v libx265 -t 0.5 -r 30 -pix_fmt yuvj420p YYYYMMDDHHMMSS_SEQ.mp4

   This command takes the last frame, repeats it (loop 1) for 0.5 seconds at 30fps, or 15 
   frames.  It writes it out in -pix\_fmt yuvj420p, which is the same as the camera.  The
   filename should be chosen so the timestamp falls between the two files where the gap
   is located.  For example:

        % ls -1
        20250828134834_000002.MP4
        20250828140000_PAD.MP4
        20250828141334_000003.MP4
        20250828143834_000004.MP4
        20250828150334_000005.MP4
