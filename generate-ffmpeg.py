#!/usr/bin/env python3

import os
import argparse
import glob
from typing import List, Tuple

def collect_mp4_files(base_directory: str) -> List[List[str]]:
    """
    Collects MP4 files from subdirectories named 'cameraX' within a base directory,
    and returns them as a sorted list of lists of MP4 file paths.

    Args:
        base_directory (str): The root directory to search within.

    Returns:
        List[List[str]]: A list where each inner list contains full paths to MP4 files
                         found in a 'cameraX' subdirectory, sorted alphabetically.
                         The outer list is ordered by camera number (e.g., camera1's files first,
                         then camera2's files, and so on).
    """
    # List to store tuples of (camera_number, camera_name, full_path_to_dir) for sorting
    camera_dirs_info: List[Tuple[int, str, str]] = []

    # Check if the base directory exists
    if not os.path.isdir(base_directory):
        print(f"Error: Directory '{base_directory}' not found.")
        return []

    # Iterate through items in the base directory to find camera subdirectories
    for item_name in os.listdir(base_directory):
        item_path: str = os.path.join(base_directory, item_name)

        # Check if it's a directory and matches the 'cameraX' pattern
        if os.path.isdir(item_path) and item_name.startswith("camera"):
            camera_num_str: str = item_name[6:] # Extract the number part (e.g., "1", "2")
            if camera_num_str.isdigit():
                camera_num: int = int(camera_num_str)
                camera_dirs_info.append((camera_num, item_name, item_path))

    # Sort the camera directories by their numerical suffix
    camera_dirs_info.sort(key=lambda x: x[0])

    # List to store the final result: an array of arrays (List[List[str]])
    all_camera_mp4s: List[List[str]] = []

    # Process each sorted camera directory
    for camera_num, camera_name, camera_path in camera_dirs_info:
        # Find all .mp4 files within this camera directory
        mp4_files: List[str] = glob.glob(os.path.join(camera_path, "*.[Mm][Oo][Vv]"))
        # Sort the files alphabetically for consistent order
        sorted_mp4_files: List[str] = sorted(mp4_files)
        all_camera_mp4s.append(sorted_mp4_files)

    return all_camera_mp4s


def construct_ffmpeg_args(files: List[List[str]], one_audio: bool, output: str, encode: str = "YouTube") -> str:
    cmd: str = "ffmpeg \\\n"

    first = True
    for cam_list in files:
        if not first:
            cmd += '-itsoffset 0.0 '
        first = False
        for f in cam_list:
            cmd += f'-i {f} '
        cmd += '\\\n'

    cmd += '-filter_complex \\\n'

    cmd += '    "'
    cam = 0
    n = 0
    first = True
    for cam_list in files:
        if not first:
            cmd += '     '
        first = False
        video = n
        for f in cam_list:
            cmd += f'[{video},v]'
            video += 1
        cmd += f'concat=n={len(cam_list)}:v=1:a=0[v{cam}_concat]; \\\n'
        cmd += '     '
        video = n
        for f in cam_list:
            cmd += f'[{video},a]'
            video += 1
        n = video
        cmd += f'concat=n={len(cam_list)}:v=0:a=1[a{cam}_concat]; \\\n'
        cam += 1

    cam = 0
    for cam_list in files:
        cmd += f'     [v{cam}_concat]scale=1920x1080,setpts=PTS-STARTPTS[scaled{cam}]; \\\n'
        cam += 1

    if one_audio:
        cam = 0
        for cam_list in files:
            cmd += f'     [a{cam}_concat]volume=1.0[a{cam}_adj]; \\\n';
            cam += 1
        cam = 0
        cmd += '     '
        for cam_list in files:
            cmd += f'[a{cam}_adj]'
            cam += 1
        cmd += f'amix=inputs={len(files)}:duration=longest:dropout_transition=2[aud_mix]; \\\n';
#    else:
#        cam = 0
#        cmd += '     '
#        for cam_list in files:
#            cmd += f'[a{cam}_adj]'
#            cam += 1
#        cmd += 'amix=inputs=4:duration=longest:dropout_transition=2[aud_mix]; \\\n'

    cmd += '     [scaled0][scaled1]hstack[top]; \\\n'
    cmd += '     [scaled2][scaled3]hstack[bottom]; \\\n'
    cmd += '     [top][bottom]vstack[outv]" \\\n'

    cmd += '-map "[outv]" \\\n'
    
    if one_audio:
        cmd += '-map "[aud_mix]" \\\n'
    else:
        cam = 0
        first = True
        for cam_list in files:
            if not first:
                cmd += ' '
            first = False
            cmd += f'-map "[a{cam}_concat]"'
            cam += 1
        cmd += ' \\\n'


    if encode == 'YouTube':
        # YouTube encoding settings provided by Gemini.
#        cmd += '-c:v libx264 -preset slow -crf 18 -profile:v high -g 60 -bf 2 -movflags faststart \\\n'
        cmd += '-c:v libx264 -preset veryfast -crf 21 -refs 4 -profile:v high -g 60 -bf 2 -movflags faststart \\\n'
        cmd += '-c:a aac -b:a 384k -ar 48000 -ac 2 \\\n'
    else:
        cmd += '-c:v libx264 -preset veryfast -crf 29 -qp 10 -profile:v main -vf format=yuv420p \\\n'
        cmd += '-c:a aac -b:a 192k \\\n'
    cmd += f'{output}\n'

    return cmd


if __name__ == "__main__":
    # Set up argument parser
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Collects MP4 file paths from 'cameraX' subdirectories."
    )
    parser.add_argument(
        "directory",
        type=str,
        help="The base directory to search for 'cameraX' subdirectories."
    )

    # Parse command-line arguments
    args: argparse.Namespace = parser.parse_args()
    input_directory: str = args.directory

    if len(input_directory) == 0:
        print("No input directory given.")
        os.exit(1)

    if not os.path.exists(input_directory):
        print(f"Directory {input_directory} does not exist.")
        os.exit(1)

    # Call the function to collect files
    # The return type is now List[List[str]]
    all_camera_mp4s: List[List[str]] = collect_mp4_files(input_directory)

    if all_camera_mp4s:
        command = construct_ffmpeg_args(all_camera_mp4s, True, input_directory + "/combined.mp4")
        print(command)
