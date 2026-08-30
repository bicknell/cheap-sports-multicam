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
import sys
from dataclasses import dataclass, field

MIN_VERSION = (7, 0)

# Confirmed via live reproduction (2026-08-30): ffmpeg 9.0.1 has a severe bug
# where a filtergraph combining video (heavy: 4-camera decode/filter/encode)
# and audio (light: concat/trim/join/aac) in one process leaks memory
# unboundedly once encoding starts -- 5-10GB/hour, no ceiling, confirmed via
# `top`'s compressed-memory-aware footprint (plain RSS undercounts it, since
# most of the leaked memory is cold/inactive pages the OS compresses). The
# audio track silently stops partway through a long render while video
# completes normally -- not a crash, just missing sound from that point on.
# ffmpeg 6.1.6 and 7.1.5 do NOT reproduce this (confirmed clean on the same
# real footage, past the point where 9.0.1 always failed); 8.x is untested.
# Warn rather than hard-block: the bad range isn't nailed down precisely
# (8.x unknown) and a future ffmpeg release may fix it, which a hardcoded
# upper bound would then incorrectly still reject.
SUSPECT_VERSION = (8, 0)


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
        if self.version >= SUSPECT_VERSION:
            print(
                f"warning: ffmpeg {'.'.join(map(str, self.version))} found -- 9.0.1 is "
                f"confirmed to silently truncate audio on long renders (video completes "
                f"normally; unbounded memory growth once encoding starts, see ffmpeg.py "
                f"for how this was confirmed). 6.1.6 and 7.1.5 are confirmed clean; 8.x is "
                f"untested. Until this is retested against your exact version, prefer "
                f"ffmpeg 7.1.x -- e.g. `brew install ffmpeg@7` and pass "
                f"--ffmpeg/--ffprobe pointing at its keg-only binaries. ALWAYS inspect a "
                f"finished render's actual audio duration (not just that it completed) "
                f"before trusting it.",
                file=sys.stderr,
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


@dataclass
class Capabilities:
    version_ok: bool
    has_libx265: bool
    has_libsvtav1: bool
    has_libvmaf: bool
    has_hevc_videotoolbox: bool
    videotoolbox_accepts_qv: bool


def check_capabilities(tool: Tool) -> Capabilities:
    """Probe optional pieces so features degrade with a message instead of a
    filtergraph parse error 40 minutes into a render (Sec 10.1)."""
    encoders = subprocess.run(
        [tool.ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True
    ).stdout
    filters = subprocess.run(
        [tool.ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True
    ).stdout
    has_hevc_videotoolbox = "hevc_videotoolbox" in encoders

    videotoolbox_accepts_qv = False
    if has_hevc_videotoolbox:
        probe = subprocess.run(
            [
                tool.ffmpeg, "-hide_banner", "-v", "error",
                "-f", "lavfi", "-i", "color=c=black:s=16x16:d=0.1",
                "-c:v", "hevc_videotoolbox", "-q:v", "50", "-frames:v", "1",
                "-f", "null", "-",
            ],
            capture_output=True, text=True,
        )
        videotoolbox_accepts_qv = probe.returncode == 0

    return Capabilities(
        version_ok=tool.version >= MIN_VERSION,
        has_libx265="libx265" in encoders,
        has_libsvtav1="libsvtav1" in encoders,
        has_libvmaf="libvmaf" in filters,
        has_hevc_videotoolbox=has_hevc_videotoolbox,
        videotoolbox_accepts_qv=videotoolbox_accepts_qv,
    )


def dry_run_script(argv_list: list[list[str]]) -> str:
    """Render a list of commands as a `set -euo pipefail` shell script.
    shlex.join() only ever produces text for a human to read here --
    never a command that gets executed."""
    lines = ["#!/bin/sh", "set -euo pipefail", ""]
    for argv in argv_list:
        lines.append(shlex.join(argv))
    return "\n".join(lines) + "\n"
