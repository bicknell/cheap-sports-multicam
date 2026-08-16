"""ffprobe -> FileInfo dataclasses, plus the validations DESIGN.md Sec 10.1
says probe.py must run because the rest of the pipeline assumes them.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

from csm import sources
from csm.ffmpeg import Tool


@dataclass
class SegmentInfo:
    path: Path
    width: int
    height: int
    fps: Fraction
    duration: float
    nb_frames: int | None
    codec_name: str
    pix_fmt: str
    color_range: str | None
    color_primaries: str | None
    color_trc: str | None
    sample_aspect_ratio: str | None
    rotation: int
    creation_time: datetime | None
    has_audio: bool
    sample_rate: int | None
    channels: int | None
    audio_duration: float | None


@dataclass
class CameraInfo:
    cam: int
    segments: list[SegmentInfo]

    @property
    def total_duration(self) -> float:
        return sum(s.duration for s in self.segments)

    @property
    def has_audio(self) -> bool:
        return bool(self.segments) and all(s.has_audio for s in self.segments)

    @property
    def first(self) -> SegmentInfo | None:
        return self.segments[0] if self.segments else None


@dataclass
class Issue:
    severity: str  # "error" | "warning"
    message: str

    def __str__(self):
        return f"[{self.severity}] {self.message}"


def _parse_creation_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def probe_segment(tool: Tool, path: Path) -> SegmentInfo:
    argv = [
        tool.ffprobe, "-v", "error", "-print_format", "json",
        "-show_entries",
        "format=duration:format_tags=creation_time:"
        "stream=codec_type,codec_name,width,height,r_frame_rate,nb_frames,"
        "pix_fmt,color_range,color_primaries,color_transfer,"
        "sample_aspect_ratio,sample_rate,channels,duration",
        "-show_entries", "stream_side_data=rotation",
        str(path),
    ]
    result = subprocess.run(argv, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)

    fmt = data.get("format", {})
    duration = float(fmt.get("duration", 0.0))
    creation_time = _parse_creation_time(fmt.get("tags", {}).get("creation_time"))

    video = next((s for s in data["streams"] if s.get("codec_type") == "video"), None)
    audio = next((s for s in data["streams"] if s.get("codec_type") == "audio"), None)
    if video is None:
        raise ValueError(f"{path}: no video stream")

    fps = Fraction(video["r_frame_rate"])
    nb_frames = int(video["nb_frames"]) if video.get("nb_frames", "N/A") != "N/A" else None
    rotation = 0
    for sd in video.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotation = int(sd["rotation"])

    return SegmentInfo(
        path=path,
        width=int(video["width"]),
        height=int(video["height"]),
        fps=fps,
        duration=duration,
        nb_frames=nb_frames,
        codec_name=video.get("codec_name", ""),
        pix_fmt=video.get("pix_fmt", ""),
        color_range=video.get("color_range"),
        color_primaries=video.get("color_primaries"),
        color_trc=video.get("color_transfer"),
        sample_aspect_ratio=video.get("sample_aspect_ratio"),
        rotation=rotation,
        creation_time=creation_time,
        has_audio=audio is not None,
        sample_rate=int(audio["sample_rate"]) if audio and "sample_rate" in audio else None,
        channels=int(audio["channels"]) if audio and "channels" in audio else None,
        audio_duration=float(audio["duration"]) if audio and "duration" in audio else None,
    )


def probe_camera(tool: Tool, camera: sources.Camera) -> CameraInfo:
    return CameraInfo(cam=camera.cam, segments=[probe_segment(tool, p) for p in camera.segments])


def validate_camera(info: CameraInfo, fps_tolerance: float = 0.01) -> list[Issue]:
    """Sec 10.1: segments of a camera must agree on size/fps/codec, name the
    offending file. Also runs the frame-drop and concat-drift diagnostics
    and the filename-order-vs-creation_time-monotonicity check."""
    issues: list[Issue] = []
    if not info.segments:
        return issues

    ref = info.segments[0]
    for seg in info.segments[1:]:
        if (seg.width, seg.height) != (ref.width, ref.height):
            issues.append(Issue("error",
                f"camera{info.cam}: {seg.path.name} is {seg.width}x{seg.height}, "
                f"expected {ref.width}x{ref.height} (from {ref.path.name})"))
        if abs(seg.fps - ref.fps) > fps_tolerance:
            issues.append(Issue("error",
                f"camera{info.cam}: {seg.path.name} is {seg.fps} fps, "
                f"expected {ref.fps} fps (from {ref.path.name})"))
        if seg.codec_name != ref.codec_name:
            issues.append(Issue("error",
                f"camera{info.cam}: {seg.path.name} is codec {seg.codec_name}, "
                f"expected {ref.codec_name} (from {ref.path.name})"))

    for seg in info.segments:
        if seg.nb_frames is not None:
            expected_duration = seg.nb_frames / float(seg.fps)
            delta = abs(expected_duration - seg.duration)
            if delta > 1.0 / float(seg.fps):
                issues.append(Issue("warning",
                    f"camera{info.cam}: {seg.path.name} nb_frames/fps ({expected_duration:.3f}s) "
                    f"disagrees with container duration ({seg.duration:.3f}s) by {delta:.3f}s "
                    f"-- possible dropped frames"))
        if seg.has_audio and seg.audio_duration is not None:
            delta = abs(seg.audio_duration - seg.duration)
            if delta > 0.5:
                issues.append(Issue("warning",
                    f"camera{info.cam}: {seg.path.name} audio duration ({seg.audio_duration:.3f}s) "
                    f"and video duration ({seg.duration:.3f}s) differ by {delta:.3f}s "
                    f"-- possible A/V drift"))

    times = [s.creation_time for s in info.segments]
    if all(t is not None for t in times):
        for a, b in zip(times, times[1:]):
            if b < a:
                issues.append(Issue("warning",
                    f"camera{info.cam}: segment filename order disagrees with creation_time "
                    f"(files are not in recording order)"))
                break

    return issues


def validate_game(
    cameras: dict[int, CameraInfo],
    audio_cams: tuple[int, ...] = (2, 4),
    timeline_fps: Fraction | None = None,
    fps_tolerance: float = 0.01,
) -> list[Issue]:
    issues: list[Issue] = []
    for cam, info in cameras.items():
        issues.extend(validate_camera(info, fps_tolerance=fps_tolerance))
        if timeline_fps is not None and info.first is not None:
            if abs(info.first.fps - timeline_fps) > fps_tolerance:
                issues.append(Issue("warning",
                    f"camera{cam}: source fps {info.first.fps} differs from "
                    f"output fps {timeline_fps}; will be converted"))

    for cam in audio_cams:
        info = cameras.get(cam)
        if info is None or not info.segments:
            issues.append(Issue("error", f"camera{cam} is an audio-cam but is missing entirely"))
        elif not info.has_audio:
            issues.append(Issue("error", f"camera{cam} is an audio-cam but has no audio stream"))

    return issues
