"""DESIGN.md Sec 10.2 tests 10-21, 26, 27 (Phase 2: timeline/filtergraph/plan/runner).

Tests 10, 18, 20 (fps-default half), 21, 26 are pure/fast -- no ffmpeg
encode involved. Everything that actually renders through `runner.run_job`
is marked `slow`, since it's a real libx265 encode even at tiny
resolutions; `pytest -m "not slow"` skips these for the on-every-save loop.
"""
from __future__ import annotations

import json
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from csm import cli, encoders, ffmpeg as ffmpeg_mod, plan, probe, runner, sources
from csm import timeline as timeline_mod
from csm.filtergraph import build_filtergraph
from csm.timeline import Timeline, Track


# --------------------------------------------------------------- helpers --

def _track(cam, *, present=True, start=0.0, duration=10.0, width=1920, height=1080,
           fps=Fraction(30), has_audio=True):
    if not present:
        return Track(cam=cam, present=False, clips=[], start=start, duration=None,
                      width=0, height=0, fps=Fraction(30), has_audio=False,
                      sample_rate=0, channels=0)
    return Track(cam=cam, present=True, clips=[Path(f"/fake/cam{cam}.mp4")], start=start,
                 duration=duration, width=width, height=height, fps=fps,
                 has_audio=has_audio, sample_rate=48000, channels=1)


def _timeline(tracks, t0=0.0, t1=10.0, out_w=3840, out_h=2160, fps=Fraction(30),
              audio_cams=(2, 4)):
    return Timeline(t0=t0, t1=t1, out_w=out_w, out_h=out_h, fps=fps, tracks=tracks,
                     audio_cams=audio_cams)


@pytest.fixture(scope="module")
def tool():
    return ffmpeg_mod.locate()


def _probe_all(tool, game_dir):
    cams = sources.discover_cameras(game_dir)
    return {cam: probe.probe_camera(tool, camera) for cam, camera in cams.items() if camera.present}


def _render(tool, game_dir, offsets, tmp_path, **timeline_kwargs):
    infos = _probe_all(tool, game_dir)
    kwargs = dict(out_w=3840, out_h=2160, fps=Fraction(30))
    kwargs.update(timeline_kwargs)
    tl = timeline_mod.build_timeline(infos, offsets, **kwargs)
    profile = encoders.get_profile("final-h265")
    output = tmp_path / "out.mp4"
    job = plan.build_plan(tl, profile, output)[0]
    runner.run_job(tool, job, work_dir=tmp_path)
    return output, tl


def _ffprobe_json(output: Path) -> dict:
    argv = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(output),
    ]
    result = subprocess.run(argv, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def _crop_is_black(output: Path, *, w: int, h: int, x: int, y: int, at: float | None = None) -> bool:
    argv = ["ffmpeg", "-v", "info"]
    if at is not None:
        argv += ["-ss", f"{at:.6f}"]
    argv += ["-i", str(output)]
    if at is not None:
        argv += ["-frames:v", "1"]
    argv += ["-vf", f"crop={w}:{h}:{x}:{y},blackframe=98:32", "-f", "null", "-"]
    result = subprocess.run(argv, capture_output=True, text=True)
    return "blackframe" in result.stderr


# ---------------------------------------------------------- test 10 -------

def test_timeline_fragments():
    """Pure unit: head>0, head<0, tail>0, absent -> exact expected filter strings."""
    tracks = [
        _track(1, start=-2.0, duration=12.0),   # head = t0 - start = 2 > 0 (trim)
        _track(2, start=2.0, duration=8.0),     # head = -2 < 0 (pad front)
        _track(3, start=0.0, duration=5.0),     # tail = 10 - 5 = 5 > 0 (pad end)
        _track(4, present=False),               # absent
    ]
    tl = _timeline(tracks, t0=0.0, t1=10.0, audio_cams=(2, 4))
    graph = build_filtergraph(tl)

    cam1_ref = graph.inputs[0]
    assert cam1_ref.kind == "concat"
    assert cam1_ref.seek == pytest.approx(2.0)
    cam1_chain = next(c for c in graph.filter_complex.split(";") if "[v1]" in c)
    assert "tpad=start_duration" not in cam1_chain
    assert "tpad=stop_duration" not in cam1_chain

    cam2_ref = graph.inputs[1]
    assert cam2_ref.seek == 0.0
    cam2_chain = next(c for c in graph.filter_complex.split(";") if c.endswith("[v2]"))
    assert "tpad=start_duration=2.000000:color=black" in cam2_chain

    cam3_chain = next(c for c in graph.filter_complex.split(";") if c.endswith("[v3]"))
    assert "tpad=stop_duration=5.000000:color=black" in cam3_chain

    cam4_ref = graph.inputs[3]
    assert cam4_ref.kind == "color"
    cam4_chain = next(c for c in graph.filter_complex.split(";") if c.endswith("[v4]"))
    assert cam4_chain == "[3:v]format=yuv420p,setsar=1[v4]"


# ---------------------------------------------------------- test 18 -------

def test_scale_skipped_when_source_matches_tile():
    """1080p sources at 4K out-size -> no scale in the graph; 4K sources -> a downscale is emitted."""
    tracks_match = [_track(c, width=1920, height=1080) for c in (1, 2, 3, 4)]
    tl_match = _timeline(tracks_match, out_w=3840, out_h=2160)
    graph_match = build_filtergraph(tl_match)
    assert "scale=" not in graph_match.filter_complex

    tracks_big = [_track(c, width=3840, height=2160) for c in (1, 2, 3, 4)]
    tl_big = _timeline(tracks_big, out_w=3840, out_h=2160)
    graph_big = build_filtergraph(tl_big)
    assert "scale=1920:1080:" in graph_big.filter_complex


# ---------------------------------------------------------- test 21 -------

def test_gop_scales_with_fps():
    tracks = [_track(c) for c in (1, 2, 3, 4)]
    tl60 = _timeline(tracks, fps=Fraction(60))
    args60 = encoders.get_profile("final-h265").output_args(tl60)
    x265_params = args60[args60.index("-x265-params") + 1]
    assert "keyint=120:min-keyint=120" in x265_params

    tl30 = _timeline(tracks, fps=Fraction(30))
    args30 = encoders.get_profile("final-h265").output_args(tl30)
    x265_params30 = args30[args30.index("-x265-params") + 1]
    assert "keyint=60:min-keyint=60" in x265_params30


# ---------------------------------------------------------- test 26 -------

def test_probe_rejects_mismatched_fps(tool, tmp_path):
    cam_dir = tmp_path / "game" / "camera1"
    cam_dir.mkdir(parents=True)
    seg1 = cam_dir / "NORM_00001.MP4"
    seg2 = cam_dir / "NORM_00002.MP4"
    for seg, fps in ((seg1, 30), (seg2, 25)):
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
             "-i", f"color=c=gray:s=320x180:rate={fps}:duration=1",
             "-c:v", "libx264", "-preset", "ultrafast", str(seg)],
            check=True,
        )
    camera = sources.Camera(cam=1, dir=cam_dir, segments=[seg1, seg2])
    info = probe.probe_camera(tool, camera)
    issues = probe.validate_camera(info)
    assert any(i.severity == "error" and seg2.name in i.message and "fps" in i.message
               for i in issues)


# ------------------------------------------------------- rendering tests --

@pytest.mark.slow
def test_render_output_shape(tool, synthetic_game, tmp_path):
    game = synthetic_game({1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}, {1: 6.0, 2: 6.0, 3: 6.0, 4: 6.0})
    output, tl = _render(tool, game, {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}, tmp_path)

    data = _ffprobe_json(output)
    vstream = next(s for s in data["streams"] if s["codec_type"] == "video")
    astream = next(s for s in data["streams"] if s["codec_type"] == "audio")

    assert int(vstream["width"]) == 3840
    assert int(vstream["height"]) == 2160
    assert vstream["codec_name"] == "hevc"
    assert abs(int(vstream["nb_frames"]) - 180) <= 1
    assert astream["channels"] == 2
    assert int(astream["sample_rate"]) == 48000
    assert abs(float(data["format"]["duration"]) - 6.0) < (1 / 30)


@pytest.mark.slow
def test_render_color_range(tool, tmp_path):
    game = tmp_path / "game"
    cam_dir = game / "camera1"
    cam_dir.mkdir(parents=True)
    seg = cam_dir / "NORM_00001.MP4"
    # Full-range source at a size that differs from the tile (2720x1530, like
    # real footage) so the scale+range-conversion path in filtergraph.py
    # actually triggers.
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "color=c=white:s=2720x1530:rate=30:duration=2",
         "-pix_fmt", "yuvj420p", "-c:v", "libx264", "-preset", "ultrafast", str(seg)],
        check=True,
    )
    for cam in (2, 3, 4):
        (game / f"camera{cam}").mkdir(parents=True, exist_ok=True)

    output, tl = _render(tool, game, {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}, tmp_path,
                          out_w=3840, out_h=2160, fps=Fraction(30))

    data = _ffprobe_json(output)
    vstream = next(s for s in data["streams"] if s["codec_type"] == "video")
    assert vstream["color_range"] == "tv"
    assert vstream["color_primaries"] == "bt709"
    assert vstream["color_transfer"] == "bt709"
    assert vstream["color_space"] == "bt709"

    # Sample a pixel from cam1's quadrant (top-left): full-range white (255)
    # should land near limited-range legal white (235). Read the raw
    # yuv420p10le luma sample directly -- converting to -pix_fmt gray first
    # re-expands the range during the swscale conversion, which would be
    # testing swscale's read-back behavior rather than what got encoded.
    argv = [
        "ffmpeg", "-v", "error", "-i", str(output), "-frames:v", "1",
        "-vf", "crop=2:2:100:100", "-pix_fmt", "yuv420p10le", "-f", "rawvideo", "-",
    ]
    result = subprocess.run(argv, capture_output=True, check=True)
    import struct
    luma_10bit = struct.unpack_from("<H", result.stdout, 0)[0]
    luma_8bit_equiv = luma_10bit / 4
    assert 228 <= luma_8bit_equiv <= 240


@pytest.mark.slow
def test_render_missing_camera(tool, synthetic_game, tmp_path):
    game = synthetic_game({1: 0.0, 2: 0.0, 4: 0.0}, {1: 4.0, 2: 4.0, 4: 4.0}, missing=(3,))
    output, tl = _render(tool, game, {1: 0.0, 2: 0.0, 4: 0.0}, tmp_path)
    assert _crop_is_black(output, w=1920, h=1080, x=0, y=1080)


@pytest.mark.slow
def test_render_short_camera(tool, synthetic_game, tmp_path):
    game = synthetic_game({1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0},
                           {1: 6.0, 2: 3.0, 3: 6.0, 4: 6.0})
    output, tl = _render(tool, game, {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}, tmp_path,
                          align_end="longest")
    data = _ffprobe_json(output)
    assert abs(float(data["format"]["duration"]) - 6.0) < (1 / 30) * 2

    assert not _crop_is_black(output, w=1920, h=1080, x=1920, y=0, at=1.0)
    assert _crop_is_black(output, w=1920, h=1080, x=1920, y=0, at=4.0)


@pytest.mark.slow
def test_render_mixed_resolution(tool, synthetic_game, tmp_path):
    game = synthetic_game(
        {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}, {1: 3.0, 2: 3.0, 3: 3.0, 4: 3.0},
        sizes={1: (1920, 1080), 2: (1920, 1080), 3: (1920, 1080), 4: (2720, 1530)},
    )
    output, tl = _render(tool, game, {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}, tmp_path)
    data = _ffprobe_json(output)
    vstream = next(s for s in data["streams"] if s["codec_type"] == "video")
    assert int(vstream["width"]) == 3840 and int(vstream["height"]) == 2160
    # cam4 (bottom-right) is exactly 16:9, matching the tile aspect, so a
    # correct fit leaves no black padding bars in its quadrant.
    assert not _crop_is_black(output, w=1920, h=1080, x=1920, y=1080)


@pytest.mark.slow
def test_render_out_size_arbitrary(tool, synthetic_game, tmp_path):
    game = synthetic_game({1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}, {1: 2.0, 2: 2.0, 3: 2.0, 4: 2.0})
    output, tl = _render(tool, game, {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}, tmp_path,
                          out_w=2560, out_h=1440)
    data = _ffprobe_json(output)
    vstream = next(s for s in data["streams"] if s["codec_type"] == "video")
    assert int(vstream["width"]) == 2560
    assert int(vstream["height"]) == 1440
    assert tl.tile == (1280, 720)


@pytest.mark.slow
def test_render_native_size(tool, synthetic_game, tmp_path):
    game = synthetic_game(
        {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}, {1: 2.0, 2: 2.0, 3: 2.0, 4: 2.0},
        sizes={c: (2720, 1530) for c in (1, 2, 3, 4)},
    )
    infos = _probe_all(tool, game)
    out_w, out_h = cli._parse_out_size("native", infos)
    assert (out_w, out_h) == (5440, 3060)

    output, tl = _render(tool, game, {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}, tmp_path,
                          out_w=out_w, out_h=out_h)
    data = _ffprobe_json(output)
    vstream = next(s for s in data["streams"] if s["codec_type"] == "video")
    assert int(vstream["width"]) == 5440
    assert int(vstream["height"]) == 3060
    graph = build_filtergraph(tl)
    assert "scale=" not in graph.filter_complex


@pytest.mark.slow
def test_non_16x9_source_letterboxed(tool, synthetic_game, tmp_path):
    game = synthetic_game(
        {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}, {1: 2.0, 2: 2.0, 3: 2.0, 4: 2.0},
        sizes={1: (1440, 1080), 2: (1920, 1080), 3: (1920, 1080), 4: (1920, 1080)},
    )
    output, tl = _render(tool, game, {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}, tmp_path)
    # cam1's quadrant is top-left, 1920x1080; a 4:3 source pillarboxes,
    # leaving black bands at the left/right edges but not in the center.
    assert _crop_is_black(output, w=10, h=10, x=0, y=500)
    assert not _crop_is_black(output, w=10, h=10, x=960, y=500)


@pytest.mark.slow
def test_fps_derived_from_source(tool, synthetic_game, tmp_path):
    game = synthetic_game({1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}, {1: 2.0, 2: 2.0, 3: 2.0, 4: 2.0}, fps=25)

    output_default, tl = _render(tool, game, {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}, tmp_path)
    data = _ffprobe_json(output_default)
    vstream = next(s for s in data["streams"] if s["codec_type"] == "video")
    assert vstream["r_frame_rate"] == "30/1"

    output_25 = tmp_path / "out25.mp4"
    infos = _probe_all(tool, game)
    tl25 = timeline_mod.build_timeline(infos, {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}, fps=Fraction(25))
    job = plan.build_plan(tl25, encoders.get_profile("final-h265"), output_25)[0]
    runner.run_job(tool, job, work_dir=tmp_path)
    data25 = _ffprobe_json(output_25)
    vstream25 = next(s for s in data25["streams"] if s["codec_type"] == "video")
    assert vstream25["r_frame_rate"] == "25/1"

    # --offset-frames converts at the output rate, not the source rate.
    assert cli._parse_kv_frames(["3=15"], 25.0) == {3: 0.6}


@pytest.mark.slow
def test_concat_segments(tool, synthetic_game, tmp_path):
    game = synthetic_game({1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}, {1: 6.0, 2: 6.0, 3: 6.0, 4: 6.0},
                           segments=3)
    infos = _probe_all(tool, game)
    for cam, info in infos.items():
        assert info.total_duration == pytest.approx(6.0, abs=0.05)

    output, tl = _render(tool, game, {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}, tmp_path)
    data = _ffprobe_json(output)
    assert abs(float(data["format"]["duration"]) - 6.0) < (1 / 30) * 2
