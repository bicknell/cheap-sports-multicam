"""Locate ffmpeg/ffprobe, run them safely, and gate on version.

See DESIGN.md Sec 10.1 and Sec 11. `run()` never uses a shell, so quoting
bugs are structurally impossible; `shlex.join()` is only ever used to
render a command for humans to read (--dry-run in a later phase), never
to build one that gets executed.
"""
from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field

MIN_VERSION = (7, 0)


class FfmpegError(RuntimeError):
    def __init__(self, argv: list[str], returncode: int, stderr: str):
        self.argv = argv
        self.returncode = returncode
        self.stderr_tail = "\n".join(stderr.splitlines()[-40:])
        super().__init__(
            f"command failed (exit {returncode}): {shlex.join(argv)}\n"
            f"{self.stderr_tail}"
        )


@dataclass
class Tool:
    ffmpeg: str
    ffprobe: str
    version: tuple[int, int] = field(default=(0, 0))

    def check_version(self) -> None:
        if self.version < MIN_VERSION:
            raise FfmpegError(
                [self.ffmpeg, "-version"],
                0,
                f"ffmpeg {'.'.join(map(str, self.version))} found, "
                f"need >= {'.'.join(map(str, MIN_VERSION))}. "
                f"Install a newer ffmpeg (e.g. `brew upgrade ffmpeg` or "
                f"`apt install ffmpeg` from a current distro).",
            )


def _parse_version(version_output: str) -> tuple[int, int]:
    m = re.search(r"ffmpeg version (\d+)\.(\d+)", version_output)
    if not m:
        return (0, 0)
    return (int(m.group(1)), int(m.group(2)))


def locate(ffmpeg_path: str | None = None, ffprobe_path: str | None = None) -> Tool:
    ffmpeg = ffmpeg_path or shutil.which("ffmpeg")
    ffprobe = ffprobe_path or shutil.which("ffprobe")
    if not ffmpeg:
        raise FfmpegError(["ffmpeg"], 0, "ffmpeg not found on PATH")
    if not ffprobe:
        raise FfmpegError(["ffprobe"], 0, "ffprobe not found on PATH")
    out = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True)
    tool = Tool(ffmpeg=ffmpeg, ffprobe=ffprobe, version=_parse_version(out.stdout))
    tool.check_version()
    return tool


def run(argv: list[str]) -> subprocess.CompletedProcess:
    """Run argv with shell=False. Raises FfmpegError on non-zero exit."""
    result = subprocess.run(argv, capture_output=True, text=False, shell=False)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
        raise FfmpegError(argv, result.returncode, stderr)
    return result


def dry_run_script(argv_list: list[list[str]]) -> str:
    """Render a list of commands as a `set -euo pipefail` shell script.
    shlex.join() only ever produces text for a human to read here --
    never a command that gets executed."""
    lines = ["#!/bin/sh", "set -euo pipefail", ""]
    for argv in argv_list:
        lines.append(shlex.join(argv))
    return "\n".join(lines) + "\n"
