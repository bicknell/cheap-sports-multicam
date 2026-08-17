"""The render Timeline/Track model (DESIGN.md Sec 5).

Turns per-camera probe data + sync offsets into a single common timeline
with head/tail alignment math, so missing and short cameras are just
Tracks with `present=False` or a `duration` shorter than the timeline,
rather than special cases scattered through the render code.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from csm.probe import CameraInfo

TRIPODS = ((1, 2), (3, 4))
LAYOUT = "0_0|w0_0|0_h0|w0_h0"


@dataclass
class Track:
    cam: int
    present: bool
    clips: list[Path]
    clip_durations: list[float]  # per-clip, same order as clips -- lets a head-trim skip
                                  # whole leading segment files instead of decoding them
    start: float                # position on the common timeline of this camera's own t=0
    duration: float | None
    width: int
    height: int
    fps: Fraction
    has_audio: bool
    sample_rate: int
    channels: int
    field_cutoff: int | None = None  # pixels from the bottom (native res) treated as
                                      # high-priority field; above it gets pre-blurred so
                                      # aq-mode/psy-rd stop spending bits on sky/stands


@dataclass
class Timeline:
    t0: float
    t1: float
    out_w: int
    out_h: int
    fps: Fraction
    tracks: list[Track]         # always 4, cameras 1..4, ordered by cam
    audio_cams: tuple[int, ...] = (2, 4)

    @property
    def tile(self) -> tuple[int, int]:
        return self.out_w // 2, self.out_h // 2

    @property
    def duration(self) -> float:
        return self.t1 - self.t0

    def track(self, cam: int) -> Track:
        return next(t for t in self.tracks if t.cam == cam)


def build_tracks(
    cameras: dict[int, CameraInfo],
    offsets: dict[int, float],
    field_cutoffs: dict[int, int] | None = None,
) -> list[Track]:
    field_cutoffs = field_cutoffs or {}
    tracks = []
    for cam in (1, 2, 3, 4):
        info = cameras.get(cam)
        if info is None or not info.segments:
            tracks.append(Track(
                cam=cam, present=False, clips=[], clip_durations=[],
                start=offsets.get(cam, 0.0),
                duration=None, width=0, height=0, fps=Fraction(30),
                has_audio=False, sample_rate=0, channels=0,
            ))
            continue
        first = info.segments[0]
        tracks.append(Track(
            cam=cam, present=True, clips=[s.path for s in info.segments],
            clip_durations=[s.duration for s in info.segments],
            start=offsets.get(cam, 0.0), duration=info.total_duration,
            width=first.width, height=first.height, fps=first.fps,
            has_audio=info.has_audio,
            sample_rate=first.sample_rate or 48000, channels=first.channels or 1,
            field_cutoff=field_cutoffs.get(cam),
        ))
    return tracks


def build_timeline(
    cameras: dict[int, CameraInfo],
    offsets: dict[int, float],
    *,
    out_w: int = 3840,
    out_h: int = 2160,
    fps: Fraction = Fraction(30),
    audio_cams: tuple[int, ...] = (2, 4),
    align_start: str = "latest",
    align_end: str = "longest",
    start: float | None = None,
    end: float | None = None,
    duration: float | None = None,
    field_cutoffs: dict[int, int] | None = None,
) -> Timeline:
    """Sec 5.1. `--start`/`--end`/`--duration` override the alignment policy
    explicitly; `align_start`/`align_end` pick among the cameras otherwise."""
    tracks = build_tracks(cameras, offsets, field_cutoffs)
    present = [t for t in tracks if t.present]
    if not present:
        raise ValueError("no cameras present")

    starts = [t.start for t in present]
    ends = [t.start + t.duration for t in present]

    if start is not None:
        t0 = start
    elif align_start == "latest":
        t0 = max(starts)
    elif align_start == "earliest":
        t0 = min(starts)
    else:
        raise ValueError(f"unknown align_start: {align_start!r}")

    if end is not None:
        t1 = end
    elif duration is not None:
        t1 = t0 + duration
    elif align_end == "longest":
        t1 = max(ends)
    elif align_end == "shortest":
        t1 = min(ends)
    else:
        raise ValueError(f"unknown align_end: {align_end!r}")

    if t1 <= t0:
        raise ValueError(f"empty timeline: t0={t0} t1={t1}")

    return Timeline(
        t0=t0, t1=t1, out_w=out_w, out_h=out_h, fps=fps, tracks=tracks,
        audio_cams=audio_cams,
    )


def head_tail(timeline: Timeline, track: Track) -> tuple[float, float]:
    """Sec 5.1: head>0 trim, head<0 pad black at front; tail>0 pad black at
    end. Only meaningful for a present track."""
    head = timeline.t0 - track.start
    tail = timeline.t1 - (track.start + track.duration)
    return head, tail
