#!/usr/bin/env python3

import argparse
import glob
import os
import tempfile
import yaml
import sys
from typing import Dict, Optional, Tuple

def load_metadata(directory_path: str) -> Dict[str, str]:
    """
    Looks for a 'metadata.yaml' file in the specified directory.

    If the file exists, it loads the YAML content into a dictionary and
    returns it. If the file does not exist, it returns an empty dictionary.

    Args:
        directory_path (str): The path to the directory to search.

    Returns:
        Dict[str, str]: A dictionary containing the metadata from the file,
                        or an empty dictionary if the file is not found.
    """
    # Construct the full path to the metadata.yaml file
    file_path: str = os.path.join(directory_path, 'metadata.yaml')

    # Check if the file exists at the constructed path
    if os.path.exists(file_path):
        try:
            # Open and load the YAML file
            with open(file_path, 'r', encoding='utf-8') as file:
                # Use safe_load for security reasons
                metadata_dict = yaml.safe_load(file)

            # Ensure the loaded data is a dictionary. If it's not, return an empty one.
            if isinstance(metadata_dict, dict):
                # The user requested a dictionary of string to string.
                # We can perform a check to ensure all keys and values are strings.
                # If they are not, we will log a warning and return the dictionary as is,
                # or a filtered dictionary to best match the type hint.
                # For this simple function, we'll assume the YAML content is correctly formatted.
                for key, value in metadata_dict.items():
                    if not isinstance(key, str) or not isinstance(value, str):
                        print(f"Error: 'metadata.yaml' entry {key} with value {value} is not a string to string.")
                        return {}
                return metadata_dict
            else:
                print("Warning: 'metadata.yaml' content is not a dictionary. Returning empty dictionary.")

        except yaml.YAMLError as e:
            # Handle potential YAML parsing errors
            print(f"Error parsing YAML file at '{file_path}': {e}")

    # Any error drops through to an empty dictionary.
    return {}

def collect_mp4_files(base_directory: str) -> list[list[str]]:
    """
    Collects MP4 files from subdirectories named 'cameraX' within a base directory,
    and returns them as a sorted list of lists of MP4 file paths.

    Args:
        base_directory (str): The root directory to search.

    Returns:
        list[list[str]]: A list where each inner list contains full paths to MP4 files
                         found in a 'cameraX' subdirectory, sorted alphabetically.
                         The outer list is ordered by camera number (e.g., camera1's files first,
                         then camera2's files, and so on).
    """
    # List to store tuples of (camera_number, camera_name, full_path_to_dir) for sorting
    camera_dirs_info: list[tuple[int, str, str]] = []

    # Check if the base directory exists
    if not os.path.isdir(base_directory):
        print(f"Error: Directory '{base_directory}' not found.")
        return []

    # Iterate through items in the base directory to find camera subdirectories
    for item_name in os.listdir(base_directory):
        item_path: str = os.path.abspath(os.path.join(base_directory, item_name))

        # Check if it's a directory and matches the 'cameraX' pattern
        if os.path.isdir(item_path) and item_name.startswith("camera"):
            camera_num_str: str = item_name[6:] # Extract the number part (e.g., "1", "2")
            if camera_num_str.isdigit():
                camera_num: int = int(camera_num_str)
                camera_dirs_info.append((camera_num, item_name, item_path))

    # Sort the camera directories by their numerical suffix
    camera_dirs_info.sort(key=lambda x: x[0])

    if len(camera_dirs_info) != 4:
        print("Could not find 4 camera directories.")
        sys.exit(1)

    # List to store the final result: an array of arrays (list[list[str]])
    all_camera_mp4s: list[list[str]] = []

    # Process each sorted camera directory
    for camera_num, camera_name, camera_path in camera_dirs_info:
        # Find all .mp4 files within this camera directory
        mp4_files: list[str] = glob.glob(os.path.join(camera_path, "*.[Mm][Pp]4"))
        # Sort the files alphabetically for consistent order
        sorted_mp4_files: list[str] = sorted(mp4_files)
        if len(sorted_mp4_files) == 0:
            print(f"No video files found in {camera_path}.")
            sys.exit(1)
        all_camera_mp4s.append(sorted_mp4_files)

    return all_camera_mp4s


def concatinate_camera_files(files: list[list[str]], directory: str) -> Tuple[list[list[str]], str]:
    """
    Stage 1: Concatinate (if required)

    The cameras are limited to FAT32 file systems and can't produce a single file
    for longer durations.  If there are multiple files per camera concatinate 
    them into a single file.

    Args:
        - files     list[list[str]] Per-camera list of a list of filenames

    Returns:
        - newfiles  list[list[str]] Per-camera list of a list of filenames after
        - cmd       str The command to generate the concatinated files.
    """
    cmd: str = ""
    newfiles: list[list[str]] = []
    for cam, cam_list in enumerate(files):
        # Do we need to concatinate multiple?
        if len(cam_list) > 1:
            cmd += f"# Stage 1: Concatinate camera {cam+1}\n"
            fp = tempfile.NamedTemporaryFile(dir=f"{directory}/camera{cam+1}", prefix="filelist-", delete=False, delete_on_close=False)
            for f in cam_list:
                fp.write(b"file '")
                fp.write(bytes(f, 'ascii'))
                fp.write(b"'\n")
            # This is the super-fast way to concatinate
            cmd += f"ffmpeg -f concat -safe 0 -i {fp.name} -c copy {directory}/camera{cam+1}/concatinated.mp4\n"
            newfiles.append([f'{directory}/camera{cam+1}/concatinated.mp4'])
            fp.close()
        else:
            cmd += f'# Camera {cam+1} had only a single file, no need to concatinate\n'
            newfiles.append(cam_list)
    files = newfiles

    cmd += "\n"

    return (files, cmd)

class NoFile(Exception):
    pass

class ExpectedOneFile(Exception):
    pass

def align_camera_files(files: list[list[str]], offsets: tuple[int, int, int, int], directory: str) -> Tuple[list[list[str]], str]:

    max_offset = max(offsets)
    cmd: str = ""
    newfiles: list[list[str]] = []
    for cam, cam_list in enumerate(files):
        # Do we need to concatinate multiple?
        if len(cam_list) == 0:
            raise NoFile(f"No file for camera {cam} in list")

        if len(cam_list) > 1:
            raise ExpectedOneFile("Expected a single file")

        cmd += f"# Stage 2: Align Camera Files {cam+1}\n"
        trim_time = max_offset - offsets[cam]
        if trim_time > 0:
            cmd += f"ffmpeg -ss {trim_time:0.3f} -i {cam_list[0]} -c copy {directory}/camera{cam+1}/combined.mp4\n"
        else:
            cmd += f"ffmpeg -i {cam_list[0]} -c copy {directory}/camera{cam+1}/combined.mp4\n"
        newfiles.append([f'{directory}/camera{cam+1}/combined.mp4'])

    cmd += "\n"
    return (newfiles, cmd)


def video_encoding(encode: str) -> str:
    """
    Generate the parameter string to encode a video.

    Args:
        encode      str A keyword with the type of encoding

    Returns:
        cmd         str The command as a string
    """

    cmd: str = ""

    if encode == 'YouTube':
        """
        I asked gemini.google.com for the best settings for uploading to YouTube.

        - -c:v libx264, aka H.264/MPEG-4 AVC
        - -preset veryfast
            - Other options: ultrafast, superfast, veryfast, faster, fast, medium, slow, slower, veryslow
            - Controls encoding speed, and resulting output file size.  Tweak down
                on faster / hardware accelerated computers.
        - -crf 21, aka Constant Rate Factor
            - 18-28 is generally acceptable.
            - Smaller values == larger files and more "quality"
        - -refs 4, aka Reference Frames
            - How many frames the encoder can look at, higher == smaller files but more CPU.
        - H.264 Profile "high"
            - Other options: baseline, main, high, high10, high442, high444
        - GOP size
            - I'm told 60 should work everywhere, and more may not work with some players.
        - B-frames between i/p-frames
            - I'm told 2 should work everywhere, and more may not work with some players.
        - Move the moov atom to the start of the file to allow immediate playback when streaming
        """
        cmd += '-c:v libx264 -preset veryfast -crf 21 -refs 4 -profile:v high -g 60 -bf 2 -movflags faststart \\\n'
    elif encode == 'H265':
        """
        - -c:v libx265, aka H.265/HEVC
        - -preset medium
            - Other options: ultrafast, superfast, veryfast, faster, fast, medium, slow, slower, veryslow
            - Controls encoding speed, and resulting output file size.  Tweak down
                on faster / hardware accelerated computers.
        - -crf 26, aka Constant Rate Factor
            - 18-28 is generally acceptable.
            - Smaller values == larger files and more "quality"
        - H.265 Profile "main"
            - Other options:
        - B-frames between i/p-frames
            - 4 for this codec
        - x265-params
            - fast-intra=1 Helps reduce encoding time, works better for streaming
            - no-open-gop=1 Improves compatability
            - refs=4 Look at 4 frames before and 4 frames after for encoding.
        - Move the moov atom to the start of the file to allow immediate playback when streaming
        """
        cmd += '-c:v libx265 -preset medium -crf 26 -profile:v main -g 60 -bf 4 -x265-params "fast-intra=1:no-open-gop=1:ref=4" -tag:v hvc1 -movflags faststart \\\n'
    elif encode == 'H265-nv':
        """
        - -c:v hevc_nvec NVIDA Hardware Accelerated H.265/HEVC
        - -preset p4 Quality setting, from p1 (best) to p9 (worst)
        - -cq:v 28 An additional quality setting
        - H.265 Profile "main"
            - Other options:
        - -strict-gop 1 same as no-opengop=1
        """
        cmd += '-c:v hevc_nvenc -preset p4 -cq:v 28 -profile:v main -strict_gop 1 -rgb_mode yuv420 -tag:v hvc1 -movflags faststart \\\n'
    elif encode == 'videotoolbox':
        """
        - -c:v hevc_videotoolbox Apple's accelerated H.265/HVEC
        - -b:v Set the bit-rate of the video.
        """
        cmd += '-c:v hevc_videotoolbox -b:v 35000K -tag:v hvc1 -movflags faststart \\\n'
    else:
        """
        A low quality but very fast catchall.
        - -c:v libx264 H264
        - -preset veryfast
            - Other options: ultrafast, superfast, veryfast, faster, fast, medium, slow, slower, veryslow
            - Controls encoding speed, and resulting output file size.  Tweak down
                on faster / hardware accelerated computers.
        - -crf 29, aka Constant Rate Factor
            - 20-32 is generally acceptable.
            - Smaller values == larger files and more "quality"
        - -qp 10
        - H.264 Profile "main"
            - Other options: baseline, main, high, high10, high442, high444
        """
        cmd += '-c:v libx264 -preset veryfast -crf 29 -qp 10 -profile:v main -vf format=yuv420p \\\n'

    return cmd


def tiled_video(files: list[list[str]], offsets: tuple[int, int, int, int], directory: str, metadata: Dict[str, str], encode: str) -> str:
    """
    Combines the video from the 4 inputs into a 2x2 matrix.

    Args:

    Returns:

    """
    cmd: str = "# Stage 3: Combine\n"
    cmd += "ffmpeg \\\n"

    """
    ffmpeg wants the input files as a series of -i <filename> arguments.
    """
    for n, cam_list in enumerate(files):
        # Add -i for each file in the current list
        for f in cam_list:
            cmd += f'-i {f} '

        cmd += '\\\n'

    """
    Construct a filter_complex filter that will combine the video.
    """
    cmd += '-filter_complex " \\\n'

    """
    PTS-STARTPTS calculates the new timestamp for each frame by subtracting the initial
    timestamp from its current timestamp. This effectively resets the video's timeline
    to start at t=0, which is crucial for ensuring that streams align correctly, especially
    after concatenating multiple clips.

    This is also used to adjust the delay between the two cameras, note that although
    the previous code made all the delays positive, this code needs them as negative!
    """
    for cam, cam_list in enumerate(files):
        cmd += f'     [{cam},v]fps=fps=30[input{cam}]; \\\n'

    """
    Tile the video in a 2x2 matrix.
    """
    cmd += '     [input0][input1][input2][input3]xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0:shortest=1,fps=fps=30[outv]; \\\n'

    """
    Each camera records mono audio, and some have the microphones facing the wrong way.
    Take camera 2 and camera 4's audio and turn it into a single stereo stream.
    """
    cmd += '     [1,a][3,a]amerge=inputs=2[aud_mix];" \\\n'

    """
    Send the final video and audio to the output.
    """
    cmd += '-map "[outv]" -map "[aud_mix]" \\\n'

    """
    Define how the output should be encoded
    """
    cmd += video_encoding(encode)
    cmd += '-c:a libfdk_aac -b:a 96k -ar 32000 \\\n'

    """
    Set Metadata
    """
    for k, v in metadata.items():
        if v is not None and v != '':
            cmd += f'-metadata {k}="{v}" \\\n'

    cmd += "-movflags faststart \\\n"

    cmd += f'{directory}/final_video.mp4\n'

    return cmd


def construct_ffmpeg_args(files: list[list[str]], offsets: tuple[float, float, float, float], one_audio: bool, directory: str, encode: str, metadata: Dict[str, str]) -> str:
    """
    construct_ffmpeg_args

    Build the ffmpeg arguments to process the videos.  I tried doing this all in one stage,
    but it caused far too many memory leaks and slowdowns.

    Steps:
        1. If there are multiple video files from a camera, concatinate them with fast copy.
        2. Remove segments from the start of the videos so they all start at the same moment.
        3. Generate tiled video.
    """

    commands: str = ""
    (files, cmd) = concatinate_camera_files(files, directory)
    commands += cmd
    (files, cmd) = align_camera_files(files, offsets, directory)
    commands += cmd
    commands += tiled_video(files, offsets, directory, metadata, encode)

    return commands


def compute_offsets(frames: list[int]) -> tuple[float, float, float, float]:
    """
    Takes a list of corresponding frames and returns time offsets.

    NOTE: Assumes 30FPS

    Args:
        frames      list[int] 6 integers indicating the following
                        1. Frame number from camera1 where camera1 and camera2 are in sync.
                        2. Frame number from camera2 where camera1 and camera2 are in sync.
                        3. Frame number from camera2 where camera2 and camera3 are in sync.
                        4. Frame number from camera3 where camera2 and camera3 are in sync.
                        5. Frame number from camera3 where camera3 and camera4 are in sync.
                        6. Frame number from camera4 where camera3 and camera4 are in sync.

    Returns:
        offsets     Tuple(float, float, float, float) Offsets for camera1, 2, 3, and 4.
                        One of the offsets is guaranteed to be 0.
    """
    if len(frames) != 6:
        print("Wrong number of frame arguments.")
        sys.exit(1)

    # Compute deltas between videos, camera1 is at offset 0.0 by convention, so:
    cam2 = frames[0] - frames[1]  # camera2 is this offset from camera1
    cam3 = frames[2] - frames[3]  # camera3 is this offset from camera2
    cam4 = frames[4] - frames[5]  # camera4 is this offset from camera3

    cam1cam2 = cam2
    cam1cam3 = cam2 + cam3
    cam1cam4 = cam2 + cam3 + cam4

    smallest = min(cam1cam2, cam1cam3, cam1cam4)
    base = 0.0
    # If one or more times was negative, shift them all so one is zero and the rest
    # are positive.
    if smallest < 0:
        base -= smallest
        cam1cam2 -= smallest
        cam1cam3 -= smallest
        cam1cam4 -= smallest

    return (base / 30.0, cam1cam2 / 30.0, cam1cam3 / 30.0, cam1cam4 / 30.0)

if __name__ == "__main__":
    my_name = os.path.basename(sys.argv[0])

    # Set up argument parser
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""\
Process multiple 1080P video files into a 4K 4x4 matrix.

Takes an input directory tree like this:

    /path/to/directory/camera1/
    /path/to/directory/camera2/
    /path/to/directory/camera3/
    /path/to/directory/camera4/

Each directory should contain one or more 1080P input movie files,
typically copied straight off the camera.

The user then needs to use other software to find frames that are
in sync between the videos, for example:

   camera1 frame 67 is in sync with camera2 frame 102
   camera2 frame 87 is in sync with camera3 frame 95
   camera3 frame 124 is in sync with camera4 frame 87

If the frames are omitted they are all treated as zero.
""",
        epilog=f"Example: {my_name} /path/to/directory 67 102 87 95 124 87",
    )
    parser.add_argument(
        "directory",
        type=str,
        help="The base directory to search for 'cameraX' subdirectories."
    )
    # Argument for the four frame integers
    parser.add_argument(
        "frame",
        type=int,
        nargs="*",
        default=[0, 0, 0, 0, 0, 0],
        help="Six integers representing cam1 cam2 cam2 cam3 cam3 cam4"
    )

    # Parse command-line arguments
    args: argparse.Namespace = parser.parse_args()
    input_directory: str = args.directory

    if len(input_directory) == 0:
        print("No input directory given.")
        sys.exit(1)

    if not os.path.exists(input_directory):
        print(f"Directory {input_directory} does not exist.")
        sys.exit(1)

    offsets = compute_offsets(args.frame)

    # Call the function to collect files
    # The return type is now list[list[str]]
    all_camera_mp4s: list[list[str]] = collect_mp4_files(input_directory)
    metadata = load_metadata(input_directory)

    if all_camera_mp4s:
        command = construct_ffmpeg_args(all_camera_mp4s, offsets, True, input_directory, "H265", metadata)
        print(command)
