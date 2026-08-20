"""`--preview`: a preview is a Timeline narrowed to a short window, run
through the exact same filtergraph/plan/runner path as a full render (Sec
9.2) -- so a preview that looks right guarantees the full render is built
the same way. No special-cased render logic lives here.
"""
from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from csm.encoders import EncoderProfile
from csm.host import HostProfile
from csm.plan import FfmpegJob, build_plan
from csm.timeline import Timeline


def _resolve_center(spec: str, duration: float) -> float:
    if spec == "middle":
        return duration / 2
    if spec.endswith("%"):
        return duration * float(spec[:-1]) / 100
    parts = spec.split(":")
    if len(parts) == 3:
        hh, mm, ss = parts
        return int(hh) * 3600 + int(mm) * 60 + float(ss)
    if len(parts) == 2:
        mm, ss = parts
        return int(mm) * 60 + float(ss)
    return float(spec)


def _window(center: float, seconds: float, duration: float) -> tuple[float, float]:
    rel_t0 = max(0.0, center - seconds / 2)
    rel_t1 = min(duration, rel_t0 + seconds)
    rel_t0 = max(0.0, rel_t1 - seconds)
    return rel_t0, rel_t1


def first_all_cameras_live(timeline: Timeline) -> float | None:
    """Offset (relative to timeline.t0) of the first moment every camera is
    recording -- where a sync error is most visible (Sec 9.2). None if any
    camera is absent, since that moment never arrives."""
    if not all(t.present for t in timeline.tracks):
        return None
    latest_start = max(t.start for t in timeline.tracks)
    offset = latest_start - timeline.t0
    return min(max(offset, 0.0), timeline.duration)


def resolve_windows(
    timeline: Timeline,
    at: list[str],
    seconds: float,
    *,
    include_all_live: bool = False,
) -> list[tuple[str, float, float]]:
    """Sec 9.2: `--preview-at middle|MM:SS|PCT` (repeatable), each an offset
    into the rendered timeline (0 == timeline.t0), plus an optional
    automatic window at the first moment all four cameras are live. Returns
    (label, absolute_t0, absolute_t1) triples."""
    windows: list[tuple[str, float, float]] = []
    for spec in at:
        center = _resolve_center(spec, timeline.duration)
        rel_t0, rel_t1 = _window(center, seconds, timeline.duration)
        windows.append((spec, timeline.t0 + rel_t0, timeline.t0 + rel_t1))

    if include_all_live:
        offset = first_all_cameras_live(timeline)
        if offset is not None:
            rel_t0, rel_t1 = _window(offset, seconds, timeline.duration)
            pair = (timeline.t0 + rel_t0, timeline.t0 + rel_t1)
            already_covered = any(
                abs(w[1] - pair[0]) < 1e-6 and abs(w[2] - pair[1]) < 1e-6 for w in windows
            )
            if not already_covered:
                windows.append(("all-cameras-live", pair[0], pair[1]))

    return windows


def _slug(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", label).strip("-") or "window"


def build_preview_plan(
    timeline: Timeline,
    profile: EncoderProfile,
    output_dir: Path,
    *,
    windows: list[tuple[str, float, float]],
    host: HostProfile | None = None,
    metadata: dict[str, str] | None = None,
) -> list[FfmpegJob]:
    jobs: list[FfmpegJob] = []
    for label, t0, t1 in windows:
        window_tl = dataclasses.replace(timeline, t0=t0, t1=t1)
        output_path = output_dir / f"preview_{_slug(label)}.mp4"
        jobs += build_plan(window_tl, profile, output_path, host=host, metadata=metadata)
    return jobs
