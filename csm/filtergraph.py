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


def _prune_leading_segments(track: Track, head: float) -> tuple[tuple[Path, ...], float]:
    """head>0 trims from the start of the camera's full recording. Decoding
    that whole span every time is fine for a full render (head is at most
    the camera-to-camera sync offset) but catastrophic for a --preview
    window deep into a long game, where head also includes the window's
    own position into the game -- decoding from the true start of the
    recording to reach it can mean tens of minutes of throwaway decode.
    Skip whole leading segment *files* first (free -- fewer -i entries, no
    seeking at all), so the in-graph trim below only ever has to decode at
    most one segment's worth, not the camera's whole history."""
    cumulative = 0.0
    skip = 0
    for d in track.clip_durations:
        if cumulative + d <= head:
            cumulative += d
            skip += 1
        else:
            break
    return tuple(track.clips[skip:]), head - cumulative


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
    if head > 0:
        clips, residual = _prune_leading_segments(track, head)
    else:
        clips, residual = tuple(track.clips), 0.0
    ref = InputRef(kind="concat", label=f"cam{track.cam}", clips=clips)

    pre_ops: list[str] = []
    if residual > 0:
        # `-ss` before a concat-demuxer `-i` is unreliable when the source
        # has audio (verified: fails outright, even for tiny offsets, on
        # ffmpeg 9.0.1 -- silent empty output, exit code 0). Trim in-graph
        # instead: decode from the start of the (already-pruned) clip list
        # and cut here. Frame-accurate and matches the verified-safe
        # pattern in Sec 3.4.
        pre_ops.append(f"trim=start={residual:.6f}")
        pre_ops.append("setpts=PTS-STARTPTS")

    post_ops: list[str] = []
    if (track.width, track.height) != (tw, th):
        post_ops.append(
            f"scale={tw}:{th}:force_original_aspect_ratio=decrease:"
            f"flags=lanczos+accurate_rnd:in_range=pc:out_range=tv"
        )
        post_ops.append(f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color=black")
        post_ops.append("format=yuv420p")
        post_ops.append("setsar=1")
    else:
        post_ops.append("format=yuv420p")
        post_ops.append("setsar=1")

    post_ops.append(f"fps=fps={out_fps}")
    if head < 0:
        post_ops.append(f"tpad=start_duration={-head:.6f}:color=black")
    if tail > 0:
        post_ops.append(f"tpad=stop_duration={tail:.6f}:color=black")

    cam = track.cam
    cutoff = track.field_cutoff
    if cutoff is not None and 0 < cutoff < track.height:
        # Real footage observation: aq-mode/psy-rd spend bits on whatever
        # looks locally complex, which includes shiny turf texture and
        # distant background as readily as players. The camera is fixed
        # for the whole game, so the sky/stands-vs-field boundary is
        # constant -- pre-blurring the top strip lowers its local
        # complexity so the encoder naturally spends fewer bits there.
        w, h = track.width, track.height
        top_h = h - cutoff
        statements: list[str] = []
        entry = f"[{idx}:v]"
        if pre_ops:
            statements.append(entry + ",".join(pre_ops) + f"[c{cam}pre]")
            entry = f"[c{cam}pre]"
        statements.append(f"{entry}split=2[c{cam}full][c{cam}crop]")
        statements.append(f"[c{cam}full]crop=w={w}:h={top_h}:x=0:y=0,gblur=sigma=2[c{cam}top]")
        statements.append(f"[c{cam}crop]crop=w={w}:h={cutoff}:x=0:y={top_h}[c{cam}bot]")
        statements.append(f"[c{cam}top][c{cam}bot]vstack=inputs=2[c{cam}cut]")
        statements.append(f"[c{cam}cut]" + ",".join(post_ops) + f"[v{cam}]")
        chain = ";".join(statements)
    else:
        chain = f"[{idx}:v]" + ",".join(pre_ops + post_ops) + f"[v{cam}]"

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
    if head > 0:
        clips, residual = _prune_leading_segments(track, head)
    else:
        clips, residual = tuple(track.clips), 0.0
    ref = InputRef(kind="concat", label=f"cam{track.cam}", clips=clips)

    ops: list[str] = []
    if residual > 0:
        ops.append(f"atrim=start={residual:.6f}")
        ops.append("asetpts=PTS-STARTPTS")
    ops.append("aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=mono")
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
