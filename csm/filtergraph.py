"""PURE: Timeline + EncoderProfile -> filter_complex string (DESIGN.md Sec 4,
5.1, 5.2, 5.3). No filesystem or process access -- `build_filtergraph`
returns data describing what ffmpeg inputs are needed (`InputRef`) and the
`-filter_complex` string that consumes them; turning an `InputRef` into a
real `-i` argument (which may mean writing a concat list file) is
`runner.py`'s job, not this module's.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from csm.timeline import Timeline, Track, head_tail


@dataclass
class InputRef:
    kind: str            # "concat" | "color" | "anullsrc"
    label: str            # e.g. "cam1"; for humans / dry-run only
    clips: tuple[Path, ...] = ()
    seek: float = 0.0     # -ss before -i, only for kind == "concat"
    duration: float | None = None   # -t after -i, only for lavfi sources
    width: int = 0
    height: int = 0
    fps: Fraction = Fraction(30)
    color: str = "black"
    sample_rate: int = 48000
    channels: int = 1


@dataclass
class FilterGraph:
    inputs: list[InputRef]         # index in list == ffmpeg input index
    filter_complex: str
    video_out_label: str = "outv"
    audio_out_label: str = "outa"


def _video_chain(idx: int, track: Track, timeline: Timeline) -> tuple[InputRef | None, str]:
    tw, th = timeline.tile
    out_fps = timeline.fps

    if not track.present:
        ref = InputRef(
            kind="color", label=f"cam{track.cam}", width=tw, height=th,
            fps=out_fps, color="black", duration=timeline.duration,
        )
        chain = f"[{idx}:v]format=yuv420p,setsar=1[v{track.cam}]"
        return ref, chain

    head, tail = head_tail(timeline, track)
    ref = InputRef(kind="concat", label=f"cam{track.cam}", clips=tuple(track.clips),
                    seek=max(head, 0.0))

    ops: list[str] = []
    if (track.width, track.height) != (tw, th):
        ops.append(
            f"scale={tw}:{th}:force_original_aspect_ratio=decrease:"
            f"flags=lanczos+accurate_rnd:in_range=pc:out_range=tv"
        )
        ops.append(f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color=black")
        ops.append("format=yuv420p")
        ops.append("setsar=1")
    else:
        ops.append("format=yuv420p")
        ops.append("setsar=1")

    ops.append(f"fps=fps={out_fps}")
    if head < 0:
        ops.append(f"tpad=start_duration={-head:.6f}:color=black")
    if tail > 0:
        ops.append(f"tpad=stop_duration={tail:.6f}:color=black")

    chain = f"[{idx}:v]" + ",".join(ops) + f"[v{track.cam}]"
    return ref, chain


def _audio_chain(idx: int, track: Track, timeline: Timeline) -> tuple[InputRef | None, str]:
    if not track.present:
        ref = InputRef(
            kind="anullsrc", label=f"cam{track.cam}a", sample_rate=48000, channels=1,
            duration=timeline.duration,
        )
        chain = f"[{idx}:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=mono[a{track.cam}]"
        return ref, chain

    head, tail = head_tail(timeline, track)
    ref = InputRef(kind="concat", label=f"cam{track.cam}", clips=tuple(track.clips),
                    seek=max(head, 0.0))

    ops = ["aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=mono"]
    if head < 0:
        ops.append(f"adelay=delays={-head * 1000:.3f}:all=1")
    if tail > 0:
        ops.append(f"apad=whole_dur={timeline.duration:.6f}")

    chain = f"[{idx}:a]" + ",".join(ops) + f"[a{track.cam}]"
    return ref, chain


def build_filtergraph(timeline: Timeline) -> FilterGraph:
    if len(timeline.audio_cams) != 2:
        raise ValueError(
            f"audio_cams must have exactly 2 entries for the stereo join, got "
            f"{timeline.audio_cams!r}"
        )

    inputs: list[InputRef] = []
    video_chains: list[str] = []
    # video: one input per camera (real cameras share their input with audio;
    # absent cameras get a separate color input, since a real -i can't be
    # split into "one input, sometimes present").
    video_input_idx: dict[int, int] = {}
    for track in timeline.tracks:
        ref, chain = _video_chain(len(inputs), track, timeline)
        video_input_idx[track.cam] = len(inputs)
        inputs.append(ref)
        video_chains.append(chain)

    audio_chains: list[str] = []
    audio_labels: list[str] = []
    for cam in timeline.audio_cams:
        track = timeline.track(cam)
        if track.present:
            # Reuse the same input as the video chain -- it's the same file(s).
            idx = video_input_idx[cam]
            ref, chain = _audio_chain(idx, track, timeline)
        else:
            ref, chain = _audio_chain(len(inputs), track, timeline)
            inputs.append(ref)
        audio_chains.append(chain)
        audio_labels.append(f"[a{cam}]")

    video_labels = "".join(f"[v{t.cam}]" for t in timeline.tracks)
    stack = (
        f"{video_labels}xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0:fill=black[outv]"
    )
    join = (
        f"{''.join(audio_labels)}join=inputs=2:channel_layout=stereo:"
        f"map=0.0-FL|1.0-FR[outa]"
    )

    filter_complex = ";".join(video_chains + audio_chains + [stack, join])
    return FilterGraph(inputs=inputs, filter_complex=filter_complex)
