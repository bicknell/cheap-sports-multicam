"""DESIGN.md Sec 10.2 tests 23-25 (Phase 3: preview and polish).

test 24 is pure/fast; tests 23 and 25 render through `runner.run_job` and
are marked `slow`.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest
import yaml

from csm import encoders, ffmpeg as ffmpeg_mod, plan, preview as preview_mod, probe, runner, sources
from csm import timeline as timeline_mod


@pytest.fixture(scope="module")
def tool():
    return ffmpeg_mod.locate()


def _probe_all(tool, game_dir):
    cams = sources.discover_cameras(game_dir)
    return {cam: probe.probe_camera(tool, camera) for cam, camera in cams.items() if camera.present}


def _ffprobe_json(output: Path) -> dict:
    argv = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(output),
    ]
    result = subprocess.run(argv, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def _psnr(a: Path, b_seek: float, b: Path) -> float:
    # -v error would suppress the psnr filter's summary line (it logs at
    # info level), so the regex below would never match.
    argv = [
        "ffmpeg", "-hide_banner", "-ss", f"{b_seek:.6f}", "-i", str(b), "-i", str(a),
        "-lavfi", "[0:v]setpts=PTS-STARTPTS[x];[1:v]setpts=PTS-STARTPTS[y];[x][y]psnr",
        "-f", "null", "-",
    ]
    result = subprocess.run(argv, capture_output=True, text=True)
    m = re.search(r"average:(inf|[\d.]+)", result.stderr)
    assert m, result.stderr
    return float(m.group(1))


# ---------------------------------------------------------- test 23 -------

@pytest.mark.slow
def test_preview_window(tool, synthetic_game, tmp_path):
    game = synthetic_game({1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}, {1: 12.0, 2: 12.0, 3: 12.0, 4: 12.0})
    infos = _probe_all(tool, game)
    tl = timeline_mod.build_timeline(infos, {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}, fps=Fraction(30))
    profile = encoders.get_profile("final-h265")

    full_output = tmp_path / "full.mp4"
    full_job = plan.build_plan(tl, profile, full_output)[0]
    runner.run_job(tool, full_job, work_dir=tmp_path)

    windows = preview_mod.resolve_windows(tl, ["50%"], 4.0)
    assert len(windows) == 1
    label, w_t0, w_t1 = windows[0]
    assert label == "50%"
    assert w_t1 - w_t0 == pytest.approx(4.0, abs=1e-6)
    assert w_t0 == pytest.approx(tl.duration / 2 - 2.0, abs=1e-6)

    preview_dir = tmp_path / "preview"
    jobs = preview_mod.build_preview_plan(tl, profile, preview_dir, windows=windows)
    assert len(jobs) == 1
    runner.run_job(tool, jobs[0], work_dir=tmp_path)
    preview_output = jobs[0].output_path

    data = _ffprobe_json(preview_output)
    assert abs(float(data["format"]["duration"]) - 4.0) < (1 / 30) * 2

    assert _psnr(preview_output, w_t0, full_output) > 35.0


# ---------------------------------------------------------- test 24 -------

def test_dry_run_is_valid_shell(tool, tmp_path):
    game = tmp_path / "game dir with 'quote' and \"dquote\""
    cam_dir = game / "camera1"
    cam_dir.mkdir(parents=True)
    seg = cam_dir / "NORM_00001.MP4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "color=c=gray:s=320x180:rate=30:duration=2",
         "-c:v", "libx264", "-preset", "ultrafast", str(seg)],
        check=True, capture_output=True,
    )
    for cam in (2, 3, 4):
        (game / f"camera{cam}").mkdir(parents=True, exist_ok=True)

    infos = _probe_all(tool, game)
    tl = timeline_mod.build_timeline(infos, {1: 0.0}, out_w=640, out_h=360, fps=Fraction(30))
    profile = encoders.get_profile("final-h265")
    output = game / "final's \"video\".mp4"
    job = plan.build_plan(tl, profile, output)[0]

    materialize_dir = tmp_path / ".csm"
    script = runner.dry_run(tool, job, materialize_dir)

    script_path = tmp_path / "dry_run.sh"
    script_path.write_text(script)
    result = subprocess.run(["bash", "-n", str(script_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

    # Every path round-trips: shlex-parsing the printed command line
    # reproduces the exact argv that would have been executed.
    expected_argv = runner.build_argv(tool, job, materialize_dir)
    command_line = script.strip("\n").split("\n")[-1]
    assert shlex.split(command_line) == expected_argv
    assert str(output) in expected_argv


# ---------------------------------------------------------- test 25 -------

@pytest.mark.slow
def test_metadata_roundtrip(tool, synthetic_game, tmp_path):
    game = synthetic_game({1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}, {1: 2.0, 2: 2.0, 3: 2.0, 4: 2.0})
    values = {
        "title": 'Game vs. "Rivals" & Friends',
        "comment": "quote' and unicode: café",
    }
    (game / "metadata.yaml").write_text(yaml.safe_dump(values, allow_unicode=True))

    infos = _probe_all(tool, game)
    tl = timeline_mod.build_timeline(infos, {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}, fps=Fraction(30))
    profile = encoders.get_profile("final-h265")
    output = tmp_path / "out.mp4"
    loaded = sources.load_metadata(game)
    assert loaded == values

    job = plan.build_plan(tl, profile, output, metadata=loaded)[0]
    runner.run_job(tool, job, work_dir=tmp_path)

    data = _ffprobe_json(output)
    tags = data["format"].get("tags", {})
    for key, value in values.items():
        assert tags.get(key) == value
