"""csm CLI: `csm probe` and `csm sync` (DESIGN.md Sec 12, Phase 1 subset)."""
from __future__ import annotations

import argparse
import subprocess
import sys
import warnings
from fractions import Fraction
from pathlib import Path

from csm import encoders, ffmpeg, plan, preview as preview_mod, probe, runner, sources, sweep, sync
from csm import timeline as timeline_mod


def _parse_kv_seconds(items: list[str] | None) -> dict[int, float]:
    result = {}
    for item in items or []:
        cam, _, val = item.partition("=")
        result[int(cam)] = float(val)
    return result


def _parse_kv_frames(items: list[str] | None, fps: float) -> dict[int, float]:
    result = {}
    for item in items or []:
        cam, _, val = item.partition("=")
        result[int(cam)] = float(val) / fps
    return result


def _parse_audio_cams(spec: str) -> tuple[int, ...]:
    return tuple(int(x) for x in spec.split(","))


def _parse_clap_at(spec: str) -> float:
    parts = spec.split(":")
    if len(parts) == 2:
        mm, ss = parts
        return int(mm) * 60 + float(ss)
    return float(spec)


def _parse_out_size(spec: str, infos: dict[int, probe.CameraInfo]) -> tuple[int, int]:
    if spec == "native":
        widths = [info.first.width for info in infos.values() if info.first]
        heights = [info.first.height for info in infos.values() if info.first]
        if not widths:
            raise ValueError("no cameras present; cannot derive --out-size native")
        return max(widths) * 2, max(heights) * 2
    w, h = spec.lower().split("x")
    return int(w), int(h)


def _add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("gamedir", type=Path, help="Directory containing camera1..camera4")
    p.add_argument("--ffmpeg", dest="ffmpeg_path", default=None)
    p.add_argument("--ffprobe", dest="ffprobe_path", default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("-q", "--quiet", action="store_true")


def cmd_probe(args: argparse.Namespace) -> int:
    tool = ffmpeg.locate(args.ffmpeg_path, args.ffprobe_path)
    cameras = sources.discover_cameras(args.gamedir)
    infos = {}
    for cam, camera in cameras.items():
        if not camera.present:
            continue
        infos[cam] = probe.probe_camera(tool, camera)

    audio_cams = _parse_audio_cams(args.audio_cams)
    issues = probe.validate_game(infos, audio_cams=audio_cams)

    if args.json:
        import json
        out = {
            str(cam): {
                "present": cam in infos,
                "segments": len(infos[cam].segments) if cam in infos else 0,
                "duration": infos[cam].total_duration if cam in infos else 0.0,
                "width": infos[cam].first.width if cam in infos and infos[cam].first else None,
                "height": infos[cam].first.height if cam in infos and infos[cam].first else None,
                "fps": str(infos[cam].first.fps) if cam in infos and infos[cam].first else None,
                "has_audio": infos[cam].has_audio if cam in infos else False,
            }
            for cam in cameras
        }
        out["issues"] = [str(i) for i in issues]
        print(json.dumps(out, indent=2))
        return 1 if any(i.severity == "error" for i in issues) else 0

    for cam, camera in cameras.items():
        if cam not in infos:
            print(f"camera{cam}: MISSING")
            continue
        info = infos[cam]
        first = info.first
        print(
            f"camera{cam}: {len(info.segments)} segment(s), "
            f"{info.total_duration:.1f}s, {first.width}x{first.height}, "
            f"{first.fps} fps, {first.codec_name}/{first.pix_fmt}, "
            f"audio={'yes' if info.has_audio else 'no'}"
        )
        if first.creation_time:
            print(f"  creation_time: {first.creation_time.isoformat()}")

    if args.audio:
        for cam, info in infos.items():
            if not info.has_audio or info.first is None:
                continue
            argv = [
                tool.ffmpeg, "-v", "error", "-t", "20",
                "-i", str(info.first.path), "-map", "0:a:0",
                "-af", "volumedetect", "-f", "null", "-",
            ]
            result = subprocess.run(argv, capture_output=True, text=True)
            mean = next((l for l in result.stderr.splitlines() if "mean_volume" in l), "")
            peak = next((l for l in result.stderr.splitlines() if "max_volume" in l), "")
            print(f"camera{cam} audio: {mean.strip()} {peak.strip()}")

    if issues:
        print()
        for issue in issues:
            print(issue)
    return 1 if any(i.severity == "error" for i in issues) else 0


def cmd_sync(args: argparse.Namespace) -> int:
    tool = ffmpeg.locate(args.ffmpeg_path, args.ffprobe_path)
    cameras = sources.discover_cameras(args.gamedir)
    infos = {cam: probe.probe_camera(tool, camera) for cam, camera in cameras.items()}

    offset_overrides = _parse_kv_seconds(args.offset)
    offset_overrides.update(_parse_kv_frames(args.offset_frames, args.fps))
    nudge_overrides = _parse_kv_seconds(args.nudge)
    clap_at = _parse_clap_at(args.clap_at) if args.clap_at else None

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            result = sync.sync_game(
                tool, infos,
                n_windows=args.sync_windows, window_len=args.sync_window_len,
                prior_window=args.prior_window, min_confidence=args.min_confidence,
                max_offset=args.max_offset, no_time_prior=args.no_time_prior,
                clap_at=clap_at, offset_overrides=offset_overrides,
                nudge_overrides=nudge_overrides, prior_tolerance=args.prior_tolerance,
            )
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        for w in caught:
            print(f"warning: {w.message}", file=sys.stderr)

    for cam in sorted(result.offsets):
        print(f"camera{cam}: offset={result.offsets[cam]:+.3f}s")
    for note in result.prior_notes:
        print(f"note: {note}")
    if result.solve.inconsistent_links:
        print(f"warning: inconsistent links: {result.solve.inconsistent_links}", file=sys.stderr)

    if args.write:
        sync_file = args.sync_file or (args.gamedir / "sync.yaml")
        sync.write_sync_yaml(
            sync_file, timelines=result.timelines, offsets=result.offsets,
            measurements=result.measurements, solve=result.solve,
            prior_agreement_notes=result.prior_notes,
        )
        print(f"wrote {sync_file}")
    return 0


def _build_timeline(
    args: argparse.Namespace, tool, infos: dict[int, probe.CameraInfo]
) -> timeline_mod.Timeline | None:
    """Shared by cmd_render and cmd_preview: sync.yaml + overrides ->
    Timeline. Prints an `error:` line and returns None on failure."""
    sync_file = args.sync_file or (args.gamedir / "sync.yaml")
    if not sync_file.exists():
        print(
            f"error: {sync_file} not found -- run `csm sync {args.gamedir} --write` first",
            file=sys.stderr,
        )
        return None
    data = sync.read_sync_yaml(sync_file)
    offsets = sync.offsets_from_sync_yaml(data)

    offset_overrides = _parse_kv_seconds(args.offset)
    offset_overrides.update(_parse_kv_frames(args.offset_frames, args.fps))
    nudge_overrides = _parse_kv_seconds(args.nudge)
    offsets = sync.apply_overrides(offsets, offset_overrides, nudge_overrides)

    try:
        out_w, out_h = _parse_out_size(args.out_size, infos)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return None

    audio_cams = _parse_audio_cams(args.audio_cams)
    fps = Fraction(args.fps).limit_denominator(100000)

    try:
        return timeline_mod.build_timeline(
            infos, offsets, out_w=out_w, out_h=out_h, fps=fps, audio_cams=audio_cams,
            align_start=args.align_start, align_end=args.align_end,
            start=args.start, end=args.end, duration=args.duration,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return None


def _load_metadata(gamedir: Path) -> dict[str, str]:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        metadata = sources.load_metadata(gamedir)
        for w in caught:
            print(f"warning: {w.message}", file=sys.stderr)
    return metadata


def cmd_render(args: argparse.Namespace) -> int:
    tool = ffmpeg.locate(args.ffmpeg_path, args.ffprobe_path)
    cameras = sources.discover_cameras(args.gamedir)
    infos = {cam: probe.probe_camera(tool, camera)
             for cam, camera in cameras.items() if camera.present}

    tl = _build_timeline(args, tool, infos)
    if tl is None:
        return 1

    profile = encoders.get_profile(args.profile)
    output_path = args.output or (args.gamedir / "final_video.mp4")
    metadata = _load_metadata(args.gamedir)
    jobs = plan.build_plan(tl, profile, output_path, metadata=metadata)

    if args.dry_run:
        work_dir = args.work_dir or (args.gamedir / ".csm")
        for job in jobs:
            print(runner.dry_run(tool, job, work_dir))
        return 0

    for job in jobs:
        runner.run_job(tool, job, work_dir=args.work_dir)
    print(f"wrote {output_path}")
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    tool = ffmpeg.locate(args.ffmpeg_path, args.ffprobe_path)
    cameras = sources.discover_cameras(args.gamedir)
    infos = {cam: probe.probe_camera(tool, camera)
             for cam, camera in cameras.items() if camera.present}

    tl = _build_timeline(args, tool, infos)
    if tl is None:
        return 1

    capabilities = ffmpeg.check_capabilities(tool)
    metadata = _load_metadata(args.gamedir)
    output_dir = args.output or (args.gamedir / ".csm" / "preview")
    preview_at = args.preview_at or ["middle"]

    if args.sweep:
        results = sweep.run_sweep(
            tool, capabilities, tl, args.sweep,
            at=preview_at, seconds=args.preview_seconds, output_dir=output_dir,
        )
        print(sweep.format_results(results))
        return 0

    try:
        profile = encoders.resolve_preview_profile(args.preview_encoder, capabilities)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    windows = preview_mod.resolve_windows(
        tl, preview_at, args.preview_seconds, include_all_live=True,
    )
    jobs = preview_mod.build_preview_plan(tl, profile, output_dir, windows=windows, metadata=metadata)

    if args.dry_run:
        work_dir = args.work_dir or (args.gamedir / ".csm")
        for job in jobs:
            print(runner.dry_run(tool, job, work_dir))
        return 0

    for job in jobs:
        runner.run_job(tool, job, work_dir=args.work_dir)
        print(f"wrote {job.output_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="csm")
    sub = parser.add_subparsers(dest="command", required=True)

    p_probe = sub.add_parser("probe", help="Inspect camera footage")
    _add_common_args(p_probe)
    p_probe.add_argument("--json", action="store_true")
    p_probe.add_argument("--audio", action="store_true", help="Print per-camera audio levels")
    p_probe.add_argument("--audio-cams", default="2,4")
    p_probe.set_defaults(func=cmd_probe)

    p_sync = sub.add_parser("sync", help="Detect camera sync offsets")
    _add_common_args(p_sync)
    p_sync.add_argument("--write", action="store_true", help="Write GAMEDIR/sync.yaml")
    p_sync.add_argument("--sync-file", type=Path, default=None)
    p_sync.add_argument("--no-time-prior", action="store_true")
    p_sync.add_argument("--prior-window", type=float, default=5.0)
    p_sync.add_argument("--prior-tolerance", type=float, default=5.0)
    p_sync.add_argument("--offset", action="append", metavar="N=SEC")
    p_sync.add_argument("--offset-frames", action="append", metavar="N=F")
    p_sync.add_argument("--nudge", action="append", metavar="N=+-SEC")
    p_sync.add_argument("--sync-windows", type=int, default=24)
    p_sync.add_argument("--sync-window-len", type=float, default=20.0)
    p_sync.add_argument("--max-offset", type=float, default=1800.0)
    p_sync.add_argument("--min-confidence", type=float, default=6.0)
    p_sync.add_argument("--clap-at", default=None, metavar="MM:SS")
    p_sync.add_argument("--fps", type=float, default=30.0, help="For --offset-frames only")
    p_sync.set_defaults(func=cmd_sync)

    p_render = sub.add_parser("render", help="Render the tiled multicam video")
    _add_common_args(p_render)
    p_render.add_argument("--sync-file", type=Path, default=None)
    p_render.add_argument("--offset", action="append", metavar="N=SEC")
    p_render.add_argument("--offset-frames", action="append", metavar="N=F")
    p_render.add_argument("--nudge", action="append", metavar="N=+-SEC")
    p_render.add_argument("--align-start", choices=("latest", "earliest"), default="latest")
    p_render.add_argument("--align-end", choices=("longest", "shortest"), default="longest")
    p_render.add_argument("--start", type=float, default=None)
    p_render.add_argument("--end", type=float, default=None)
    p_render.add_argument("--duration", type=float, default=None)
    p_render.add_argument("--fps", type=float, default=30.0)
    p_render.add_argument("--audio-cams", default="2,4")
    p_render.add_argument("--out-size", default="3840x2160", help="WxH, or 'native'")
    p_render.add_argument("--profile", default="final-h265", choices=sorted(encoders.PROFILES))
    p_render.add_argument("--output", type=Path, default=None)
    p_render.add_argument("--work-dir", type=Path, default=None)
    p_render.add_argument("--dry-run", action="store_true")
    p_render.set_defaults(func=cmd_render)

    p_preview = sub.add_parser("preview", help="Render a short preview window, or sweep encoder settings")
    _add_common_args(p_preview)
    p_preview.add_argument("--sync-file", type=Path, default=None)
    p_preview.add_argument("--offset", action="append", metavar="N=SEC")
    p_preview.add_argument("--offset-frames", action="append", metavar="N=F")
    p_preview.add_argument("--nudge", action="append", metavar="N=+-SEC")
    p_preview.add_argument("--align-start", choices=("latest", "earliest"), default="latest")
    p_preview.add_argument("--align-end", choices=("longest", "shortest"), default="longest")
    p_preview.add_argument("--start", type=float, default=None)
    p_preview.add_argument("--end", type=float, default=None)
    p_preview.add_argument("--duration", type=float, default=None)
    p_preview.add_argument("--fps", type=float, default=30.0)
    p_preview.add_argument("--audio-cams", default="2,4")
    p_preview.add_argument("--out-size", default="3840x2160", help="WxH, or 'native'")
    p_preview.add_argument("--preview-seconds", type=float, default=60.0)
    p_preview.add_argument(
        "--preview-at", action="append", metavar="middle|MM:SS|PCT",
        help="Repeatable; default is 'middle' plus an automatic window at the first "
             "moment all four cameras are live",
    )
    p_preview.add_argument("--preview-encoder", default="auto", metavar="auto|NAME")
    p_preview.add_argument(
        "--sweep", action="append", metavar="KEY=v1,v2,...",
        help="Repeatable; e.g. --sweep crf=21,23,25 --sweep preset=fast,medium",
    )
    p_preview.add_argument("--output", type=Path, default=None, help="Output directory")
    p_preview.add_argument("--work-dir", type=Path, default=None)
    p_preview.add_argument("--dry-run", action="store_true")
    p_preview.set_defaults(func=cmd_preview)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
