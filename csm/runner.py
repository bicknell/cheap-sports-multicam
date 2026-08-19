"""Executes an FfmpegJob (DESIGN.md Sec 4, 9.1). The only module in the
render path allowed to touch the filesystem or spawn a process: the concat
demuxer needs a real list file per camera (Sec 5.4), and writing it happens
here, right before running, rather than in plan.py/filtergraph.py which
must stay pure.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from csm import ffmpeg
from csm.ffmpeg import Tool
from csm.filtergraph import InputRef
from csm.plan import FfmpegJob


def _concat_list_line(path: Path) -> str:
    # Absolute, not str(path) verbatim: the concat demuxer resolves a
    # relative entry against the *list file's own* directory (a temp dir
    # that has nothing to do with the process cwd), not the cwd a relative
    # GAMEDIR was typed relative to. Surfaced for real running `csm render`
    # on a relative GAMEDIR (a droplet's `csm render droplet-test-game`) --
    # every prior render happened to pass an absolute GAMEDIR.
    escaped = str(path.resolve()).replace("'", "'\\''")
    return f"file '{escaped}'\n"


def _input_args(ref: InputRef, tmpdir: Path, index: int) -> list[str]:
    if ref.kind == "concat":
        list_file = tmpdir / f"input{index}.txt"
        list_file.write_text("".join(_concat_list_line(p) for p in ref.clips))
        # No -ss here: head-trim happens in-graph (filtergraph.py's
        # trim=start=.../atrim=start=...), not via input seek. See the
        # comment there for why.
        return ["-f", "concat", "-safe", "0", "-protocol_whitelist", "file",
                 "-i", str(list_file)]
    if ref.kind == "color":
        spec = (
            f"color=c={ref.color}:s={ref.width}x{ref.height}:"
            f"rate={ref.fps}:duration={ref.duration:.6f}"
        )
        return ["-f", "lavfi", "-i", spec]
    if ref.kind == "anullsrc":
        layout = "mono" if ref.channels == 1 else "stereo"
        spec = f"anullsrc=r={ref.sample_rate}:cl={layout}"
        return ["-f", "lavfi", "-t", f"{ref.duration:.6f}", "-i", spec]
    raise ValueError(f"unknown InputRef.kind: {ref.kind!r}")


def build_argv(tool: Tool, job: FfmpegJob, tmpdir: Path) -> list[str]:
    argv = [tool.ffmpeg, "-y", "-v", "error"]
    for i, ref in enumerate(job.graph.inputs):
        argv += _input_args(ref, tmpdir, i)
    argv += ["-filter_complex", job.graph.filter_complex]
    argv += ["-map", f"[{job.graph.video_out_label}]", "-map", f"[{job.graph.audio_out_label}]"]
    for key, value in job.metadata.items():
        argv += ["-metadata", f"{key}={value}"]
    argv += job.output_args
    argv += [str(job.output_path)]
    return argv


def run_job(tool: Tool, job: FfmpegJob, work_dir: Path | None = None) -> None:
    job.output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work_dir) as tmp:
        argv = build_argv(tool, job, Path(tmp))
        ffmpeg.run(argv)


def dry_run(tool: Tool, job: FfmpegJob, materialize_dir: Path) -> str:
    """Like run_job, but writes concat lists into a directory that outlives
    this call and prints the command instead of running it, so the printed
    script stays pasteable (Sec 12: '--dry-run ... provably the same
    command'). materialize_dir is not cleaned up here -- it's part of the
    game's .csm/ work directory."""
    materialize_dir.mkdir(parents=True, exist_ok=True)
    argv = build_argv(tool, job, materialize_dir)
    return ffmpeg.dry_run_script([argv])
