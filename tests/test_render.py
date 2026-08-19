"""DESIGN.md Sec 10.2 tests 10-21, 26, 27 (Phase 2: timeline/filtergraph/plan/runner).

Tests 10, 18, 20 (fps-default half), 21, 26 are pure/fast -- no ffmpeg
encode involved. Everything that actually renders through `runner.run_job`
is marked `slow`, since it's a real libx265 encode even at tiny
resolutions; `pytest -m "not slow"` skips these for the on-every-save loop.
"""
from __future__ import annotations

import json
import re
import subprocess
from collections import namedtuple
from fractions import Fraction
from pathlib import Path

import pytest

from csm import cli, encoders, ffmpeg as ffmpeg_mod, plan, probe, render_config, runner, sources
from csm import timeline as timeline_mod
from csm.filtergraph import build_filtergraph
from csm.timeline import Timeline, Track


# --------------------------------------------------------------- helpers --

def _track(cam, *, present=True, start=0.0, duration=10.0, width=1920, height=1080,
           fps=Fraction(30), has_audio=True, clip_durations=None, field_cutoff=None):
    if not present:
        return Track(cam=cam, present=False, clips=[], clip_durations=[], start=start,
                      duration=None, width=0, height=0, fps=Fraction(30), has_audio=False,
                      sample_rate=0, channels=0)
    if clip_durations is None:
        clips = [Path(f"/fake/cam{cam}.mp4")]
        clip_durations = [duration]
    else:
        clips = [Path(f"/fake/cam{cam}_{i}.mp4") for i in range(len(clip_durations))]
    return Track(cam=cam, present=True, clips=clips, clip_durations=clip_durations, start=start,
                 duration=duration, width=width, height=height, fps=fps,
                 has_audio=has_audio, sample_rate=48000, channels=1, field_cutoff=field_cutoff)


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
    cam1_chain = next(c for c in graph.filter_complex.split(";") if "[v1]" in c)
    assert "trim=start=2.000000" in cam1_chain
    assert "setpts=PTS-STARTPTS" in cam1_chain
    assert "tpad=start_duration" not in cam1_chain
    assert "tpad=stop_duration" not in cam1_chain

    cam2_ref = graph.inputs[1]
    cam2_chain = next(c for c in graph.filter_complex.split(";") if c.endswith("[v2]"))
    assert "trim=start" not in cam2_chain
    assert "tpad=start_duration=2.000000:color=black" in cam2_chain

    cam3_chain = next(c for c in graph.filter_complex.split(";") if c.endswith("[v3]"))
    assert "tpad=stop_duration=5.000000:color=black" in cam3_chain

    cam4_ref = graph.inputs[3]
    assert cam4_ref.kind == "color"
    cam4_chain = next(c for c in graph.filter_complex.split(";") if c.endswith("[v4]"))
    assert cam4_chain == "[3:v]format=yuv420p,setsar=1[v4]"


def test_head_trim_prunes_leading_segments():
    """A deep head-trim must not decode the camera's whole history: whole
    leading segment files are dropped from the concat list, and only the
    residual within the first remaining segment is decoded and cut. This
    is what keeps --preview windows deep into a long game fast (regression
    for the bug found while testing preview on real footage: a naive
    trim=start=head decoded 48+ minutes to reach a 60s window)."""
    tracks = [
        _track(1, start=0.0, duration=30.0, clip_durations=[10.0, 10.0, 10.0]),
        _track(2, present=False),
        _track(3, present=False),
        _track(4, present=False),
    ]
    # head = t0 - start = 23 -- inside the 3rd segment (skips the first two).
    tl = _timeline(tracks, t0=23.0, t1=30.0, audio_cams=(2, 4))
    graph = build_filtergraph(tl)

    cam1_ref = graph.inputs[0]
    assert cam1_ref.kind == "concat"
    assert [c.name for c in cam1_ref.clips] == ["cam1_2.mp4"]
    cam1_chain = next(c for c in graph.filter_complex.split(";") if "[v1]" in c)
    assert "trim=start=3.000000" in cam1_chain


def test_field_cutoff_blurs_top_strip():
    """--field-cutoff N=PIXELS: everything above the cutoff (native res,
    measured from the bottom) is split off, cropped, and blurred before
    the normal scale/pad chain; the bottom (field) strip and the rest of
    the chain are untouched. No cutoff -> single comma-chain, unchanged
    from before this feature existed (regression guard)."""
    tracks = [_track(c, width=1920, height=1080) for c in (1, 2, 3, 4)]
    tracks[0] = _track(1, width=1920, height=1080, field_cutoff=800)
    tl = _timeline(tracks, out_w=3840, out_h=2160)
    graph = build_filtergraph(tl)

    statements = graph.filter_complex.split(";")
    cam1_statements = [s for s in statements if "c1" in s or s.endswith("[v1]")]

    assert any("crop=w=1920:h=280:x=0:y=0" in s and "gblur=sigma=2" in s for s in cam1_statements)
    assert any("crop=w=1920:h=800:x=0:y=280" in s for s in cam1_statements)
    assert any(s.startswith("[c1top][c1bot]vstack=inputs=2") for s in cam1_statements)
    final_stmt = next(s for s in cam1_statements if s.endswith("[v1]"))
    assert final_stmt.startswith("[c1cut]")

    # cam2 has no cutoff -- untouched, still a single comma-chain.
    cam2_chain = next(c for c in graph.filter_complex.split(";") if c.endswith("[v2]"))
    assert cam2_chain.startswith("[1:v]")
    assert "split=2" not in cam2_chain


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


def test_sweep_svtav1_preset_accepts_names_and_numbers():
    """Regression: ffmpeg's libsvtav1 -preset is a bare number, not a named
    string like x265's -- passing 'fast' straight through (as --sweep
    preset=fast,medium does for both encoders alike, Sec 7.3's own
    example) crashed with 'Undefined constant ... in fast' at encoder-open
    time. Named presets must map to a number for svtav1; numbers pass
    through unchanged."""
    tracks = [_track(c) for c in (1, 2, 3, 4)]
    tl = _timeline(tracks)

    args = encoders.sweep_args(tl, encoder="libsvtav1", crf="30", preset="medium")
    assert args[args.index("-preset") + 1] == "6"

    args_numeric = encoders.sweep_args(tl, encoder="libsvtav1", crf="30", preset="8")
    assert args_numeric[args_numeric.index("-preset") + 1] == "8"

    with pytest.raises(ValueError, match="unknown SVT-AV1 preset"):
        encoders.sweep_args(tl, encoder="libsvtav1", crf="30", preset="bogus")


def test_sweep_x265_matches_production_tuning():
    """sweep_args' libx265 branch must carry the same aq-mode/psy-rd/
    rc-lookahead/ref tuning as the real final-h265 profile (it originally
    didn't -- only keyint -- so sweep numbers weren't representative of
    what csm render actually produces). psy-rd/aq-strength are overridable
    per Sec 7.3's --sweep psy-rd=...,aq-strength=... ; everything else
    stays fixed."""
    tracks = [_track(c) for c in (1, 2, 3, 4)]
    tl = _timeline(tracks)

    args = encoders.sweep_args(tl, encoder="libx265", crf="23", preset="medium")
    params = args[args.index("-x265-params") + 1]
    assert "aq-mode=3" in params
    assert f"psy-rd={encoders.DEFAULT_PSY_RD}" in params
    assert f"aq-strength={encoders.DEFAULT_AQ_STRENGTH}" in params
    assert "rc-lookahead=40" in params
    assert "ref=4" in params

    tuned = encoders.sweep_args(
        tl, encoder="libx265", crf="23", preset="medium", psy_rd="0.5", aq_strength="0.6",
    )
    tuned_params = tuned[tuned.index("-x265-params") + 1]
    assert "psy-rd=0.5" in tuned_params
    assert "aq-strength=0.6" in tuned_params


def test_render_config_missing_file_is_empty(tmp_path):
    config = render_config.load_render_config(tmp_path)
    assert config.field_cutoff == {}
    assert config.crf is None
    assert config.preset is None
    assert config.psy_rd is None
    assert config.aq_strength is None


def test_render_config_parses_csm_yaml(tmp_path):
    (tmp_path / "csm.yaml").write_text(
        "field_cutoff: {1: 880, 2: 910}\ncrf: '25'\npreset: medium\n"
        "psy_rd: '1.5'\naq_strength: '0.2'\n"
    )
    config = render_config.load_render_config(tmp_path)
    assert config.field_cutoff == {1: 880, 2: 910}
    assert config.crf == "25"
    assert config.preset == "medium"
    assert config.psy_rd == "1.5"
    assert config.aq_strength == "0.2"


def test_resolve_profile_layering():
    """GAMEDIR/csm.yaml provides the default; an explicit --crf/--preset/
    --psy-rd/--aq-strength on the command line still wins for that one run
    (same override pattern as --offset/--nudge over sync.yaml). A
    non-final-h265 --profile bypasses all persisted values entirely -- they
    don't mean the same thing there."""
    Args = namedtuple("Args", ["profile", "crf", "preset", "psy_rd", "aq_strength"])

    def x265_params(profile):
        args_out = profile.output_args(_timeline([_track(c) for c in (1, 2, 3, 4)]))
        return args_out, args_out[args_out.index("-x265-params") + 1]

    no_config = render_config.RenderConfig(field_cutoff={})
    args = Args(profile="final-h265", crf=None, preset=None, psy_rd=None, aq_strength=None)
    profile = cli._resolve_profile(args, no_config)
    assert profile.name == "final-h265"
    args_out, params = x265_params(profile)
    assert args_out[args_out.index("-crf") + 1] == encoders.DEFAULT_CRF
    assert args_out[args_out.index("-preset") + 1] == encoders.DEFAULT_PRESET
    assert f"psy-rd={encoders.DEFAULT_PSY_RD}" in params
    assert f"aq-strength={encoders.DEFAULT_AQ_STRENGTH}" in params

    persisted = render_config.RenderConfig(
        field_cutoff={}, crf="25", preset="medium", psy_rd="1.5", aq_strength="0.2",
    )
    profile = cli._resolve_profile(
        Args(profile="final-h265", crf=None, preset=None, psy_rd=None, aq_strength=None), persisted,
    )
    args_out, params = x265_params(profile)
    assert args_out[args_out.index("-crf") + 1] == "25"
    assert args_out[args_out.index("-preset") + 1] == "medium"
    assert "psy-rd=1.5" in params
    assert "aq-strength=0.2" in params

    # explicit CLI flag wins over the persisted value
    profile = cli._resolve_profile(
        Args(profile="final-h265", crf="21", preset=None, psy_rd="0.8", aq_strength=None), persisted,
    )
    args_out, params = x265_params(profile)
    assert args_out[args_out.index("-crf") + 1] == "21"
    assert args_out[args_out.index("-preset") + 1] == "medium"
    assert "psy-rd=0.8" in params
    assert "aq-strength=0.2" in params

    other = cli._resolve_profile(
        Args(profile="preview-x265", crf="99", preset=None, psy_rd=None, aq_strength=None), persisted,
    )
    assert other.name == "preview-x265"


def test_parse_start_end_camera_relative():
    """CAM@SPEC resolves via sync.yaml's offset(CAM), so any camera can be
    used as the reference point, not just the one the timeline happens to
    start from. Bare SPEC (no '@') is unchanged: a plain common-timeline
    point, in seconds or MM:SS."""
    offsets = {1: 0.0, 2: 2.442, 3: 146.89, 4: 148.414}

    assert cli._parse_start_end("324", offsets) == 324.0
    assert cli._parse_start_end("5:24", offsets) == 324.0

    assert cli._parse_start_end("1@324", offsets) == 324.0
    assert cli._parse_start_end("1@5:24", offsets) == 324.0
    assert cli._parse_start_end("2@50", offsets) == 2.442 + 50.0
    assert cli._parse_start_end("4@0", offsets) == 148.414

    with pytest.raises(ValueError):
        cli._parse_start_end("9@0", offsets)


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
def test_render_head_trim(tool, synthetic_game, tmp_path):
    """Regression: -ss on a concat-demuxer input silently dropped all
    video when the source had audio (verified against real ffmpeg while
    building Sec 9.2's preview -- test 10 only checked the filtergraph
    string, never a real render of the head>0 trim case). Fixed by
    trimming in-graph (trim=start=.../atrim=start=...) instead of seeking
    the concat input."""
    game = synthetic_game({1: -2.0, 2: 0.0, 3: 0.0, 4: 0.0},
                           {1: 8.0, 2: 6.0, 3: 6.0, 4: 6.0})
    output, tl = _render(tool, game, {1: -2.0, 2: 0.0, 3: 0.0, 4: 0.0}, tmp_path)
    data = _ffprobe_json(output)
    vstream = next(s for s in data["streams"] if s["codec_type"] == "video")
    astream = next(s for s in data["streams"] if s["codec_type"] == "audio")
    assert int(vstream["nb_frames"]) > 0
    assert astream["channels"] == 2
    assert abs(float(data["format"]["duration"]) - 6.0) < (1 / 30) * 2


@pytest.mark.slow
def test_render_head_trim_prunes_segments(tool, synthetic_game, tmp_path):
    """Real-ffmpeg counterpart to test_head_trim_prunes_leading_segments:
    cam1 is 3 segments of 4s each (12s total), offset -6s, so head=6 falls
    inside the 2nd segment -- the 1st segment must be dropped from the
    concat list entirely and the render must still be correct."""
    game = synthetic_game({1: -6.0, 2: 0.0, 3: 0.0, 4: 0.0},
                           {1: 12.0, 2: 6.0, 3: 6.0, 4: 6.0}, segments=3)
    output, tl = _render(tool, game, {1: -6.0, 2: 0.0, 3: 0.0, 4: 0.0}, tmp_path)
    cam1_track = tl.track(1)
    assert len(cam1_track.clips) == 3

    data = _ffprobe_json(output)
    vstream = next(s for s in data["streams"] if s["codec_type"] == "video")
    astream = next(s for s in data["streams"] if s["codec_type"] == "audio")
    assert int(vstream["nb_frames"]) > 0
    assert astream["channels"] == 2
    assert abs(float(data["format"]["duration"]) - 6.0) < (1 / 30) * 2


def _psnr_region(a: Path, b: Path, *, w: int, h: int, x: int, y: int) -> float:
    argv = [
        "ffmpeg", "-hide_banner", "-i", str(a), "-i", str(b),
        "-lavfi",
        f"[0:v]crop={w}:{h}:{x}:{y}[x];[1:v]crop={w}:{h}:{x}:{y}[y];[x][y]psnr",
        "-f", "null", "-",
    ]
    result = subprocess.run(argv, capture_output=True, text=True)
    m = re.search(r"average:(inf|[\d.]+)", result.stderr)
    assert m, result.stderr
    return float(m.group(1))


@pytest.mark.slow
def test_field_cutoff_real_render(tool, tmp_path):
    """--field-cutoff, verified against real ffmpeg: the region above the
    cutoff must be measurably blurred relative to a render without any
    cutoff, while the region below it (and the rest of the frame) must be
    essentially unchanged. synthetic_game's flat-gray fixture has nothing
    to blur, so this builds a testsrc2 camera directly (real fine detail:
    moving gradients, patterns) at exactly the tile size, so quadrant
    pixels map 1:1 to native-resolution pixels with no scale/pad in the
    way."""
    game = tmp_path / "game"
    cam_dir = game / "camera1"
    cam_dir.mkdir(parents=True)
    seg = cam_dir / "NORM_00001.MP4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc2=size=640x360:rate=30:duration=2",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(seg)],
        check=True,
    )
    for cam in (2, 3, 4):
        (game / f"camera{cam}").mkdir(parents=True, exist_ok=True)

    baseline, _ = _render(tool, game, {1: 0.0}, tmp_path, out_w=1280, out_h=720)
    baseline_path = tmp_path / "baseline.mp4"
    baseline.rename(baseline_path)
    cutoff_out, tl = _render(
        tool, game, {1: 0.0}, tmp_path, out_w=1280, out_h=720, field_cutoffs={1: 180},
    )

    # cam1 is top-left, tile 640x360, cutoff=180 splits it evenly.
    top_psnr = _psnr_region(baseline_path, cutoff_out, w=640, h=180, x=0, y=0)
    bottom_psnr = _psnr_region(baseline_path, cutoff_out, w=640, h=180, x=0, y=180)

    assert bottom_psnr > 35.0
    assert top_psnr < bottom_psnr - 5.0


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
