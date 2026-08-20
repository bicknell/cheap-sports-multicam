"""csm CLI: `csm probe` and `csm sync` (DESIGN.md Sec 12, Phase 1 subset)."""
from __future__ import annotations

import argparse
import subprocess
import sys
import warnings
from fractions import Fraction
from pathlib import Path

from csm import encoders, ffmpeg, plan, preview as preview_mod, probe, render_config, runner, sources, sweep, sync
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


def _parse_kv_ints(items: list[str] | None) -> dict[int, int]:
    result = {}
    for item in items or []:
        cam, _, val = item.partition("=")
        result[int(cam)] = int(val)
    return result


def _parse_audio_cams(spec: str) -> tuple[int, ...]:
    return tuple(int(x) for x in spec.split(","))


def _parse_time(spec: str) -> float:
    """SECONDS, MM:SS, or HH:MM:SS."""
    parts = spec.split(":")
    if len(parts) == 3:
        hh, mm, ss = parts
        return int(hh) * 3600 + int(mm) * 60 + float(ss)
    if len(parts) == 2:
        mm, ss = parts
        return int(mm) * 60 + float(ss)
    return float(spec)


def _parse_clap_at(spec: str) -> float:
    return _parse_time(spec)


def _parse_start_end(spec: str, offsets: dict[int, float]) -> float:
    """Plain SECONDS, MM:SS, or HH:MM:SS on the common timeline, or
    CAM@SECONDS / CAM@MM:SS / CAM@HH:MM:SS -- a point in camera CAM's own
    raw footage, resolved via sync.yaml's offset(CAM) to the equivalent
    common-timeline second. Lets you scrub whichever camera is convenient
    rather than mentally translating through the timeline's own origin
    (Sec 12)."""
    if "@" in spec:
        cam_str, _, rest = spec.partition("@")
        cam = int(cam_str)
        if cam not in offsets:
            raise ValueError(f"camera {cam} not in sync offsets (have: {sorted(offsets)})")
        return offsets[cam] + _parse_time(rest)
    return _parse_time(spec)


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
                # volumedetect's stats print at ffmpeg's "info" log level, not
                # "error" -- -v error silently ate them here (mean/peak always
                # printed blank, on every camera, every game).
                tool.ffmpeg, "-v", "info", "-t", "20",
                "-i", str(info.first.path), "-map", "0:a:0",
                "-af", "volumedetect", "-f", "null", "-",
            ]
            result = subprocess.run(argv, capture_output=True, text=True)
            mean = next((l for l in result.stderr.splitlines() if "mean_volume" in l), "")
            peak = next((l for l in result.stderr.splitlines() if "max_volume" in l), "")
            mean = mean.rsplit("]", 1)[-1].strip()
            peak = peak.rsplit("]", 1)[-1].strip()
            print(f"camera{cam} audio: {mean} {peak}")

    if args.frame:
        frame_dir = args.gamedir / ".csm" / "frames"
        frame_dir.mkdir(parents=True, exist_ok=True)
        for cam, info in infos.items():
            if info.first is None:
                continue
            # Default well past typical setup time (cameras are often still
            # being raised/aimed in the first 10-15s) but still clamped to
            # something the first segment actually contains.
            at = min(args.frame_at, max(info.first.duration - 1.0, 0.0))
            out_path = frame_dir / f"camera{cam}.png"
            argv = [
                tool.ffmpeg, "-y", "-v", "error", "-ss", f"{at:.6f}",
                "-i", str(info.first.path), "-frames:v", "1", str(out_path),
            ]
            subprocess.run(argv, check=True)
            print(f"camera{cam}: wrote {out_path} ({info.first.width}x{info.first.height})")

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
            for w in caught:
                print(f"warning: {w.message}", file=sys.stderr)
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
    args: argparse.Namespace, tool, infos: dict[int, probe.CameraInfo],
    config: render_config.RenderConfig,
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
    field_cutoffs = dict(config.field_cutoff)
    field_cutoffs.update(_parse_kv_ints(getattr(args, "field_cutoff", None)))

    try:
        start = _parse_start_end(args.start, offsets) if args.start is not None else None
        end = _parse_start_end(args.end, offsets) if args.end is not None else None
        return timeline_mod.build_timeline(
            infos, offsets, out_w=out_w, out_h=out_h, fps=fps, audio_cams=audio_cams,
            align_start=args.align_start, align_end=args.align_end,
            start=start, end=end, duration=args.duration,
            field_cutoffs=field_cutoffs,
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


def _resolve_profile(
    args: argparse.Namespace, config: render_config.RenderConfig,
) -> encoders.EncoderProfile:
    """--crf/--preset/--psy-rd/--aq-strength (or their GAMEDIR/csm.yaml
    persisted defaults) only apply to final-h265 -- an explicitly-chosen
    other --profile is used as-is, since these don't mean the same thing
    across profiles."""
    if args.profile != "final-h265":
        return encoders.get_profile(args.profile)
    crf = args.crf or config.crf or encoders.DEFAULT_CRF
    preset = args.preset or config.preset or encoders.DEFAULT_PRESET
    psy_rd = getattr(args, "psy_rd", None) or config.psy_rd or encoders.DEFAULT_PSY_RD
    aq_strength = (
        getattr(args, "aq_strength", None) or config.aq_strength or encoders.DEFAULT_AQ_STRENGTH
    )
    return encoders.final_h265_profile(
        crf=crf, preset=preset, psy_rd=psy_rd, aq_strength=aq_strength,
    )


def cmd_render(args: argparse.Namespace) -> int:
    tool = ffmpeg.locate(args.ffmpeg_path, args.ffprobe_path)
    cameras = sources.discover_cameras(args.gamedir)
    infos = {cam: probe.probe_camera(tool, camera)
             for cam, camera in cameras.items() if camera.present}

    config = render_config.load_render_config(args.gamedir)
    tl = _build_timeline(args, tool, infos, config)
    if tl is None:
        return 1

    profile = _resolve_profile(args, config)
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

    config = render_config.load_render_config(args.gamedir)
    tl = _build_timeline(args, tool, infos, config)
    if tl is None:
        return 1

    capabilities = ffmpeg.check_capabilities(tool)
    metadata = _load_metadata(args.gamedir)
    output_dir = args.output or (args.gamedir / ".csm" / "preview")
    preview_at = args.preview_at or ["middle"]

    if args.sweep:
        default_crf = args.crf or config.crf or encoders.DEFAULT_CRF
        default_preset = args.preset or config.preset or encoders.DEFAULT_PRESET
        default_psy_rd = args.psy_rd or config.psy_rd or encoders.DEFAULT_PSY_RD
        default_aq_strength = args.aq_strength or config.aq_strength or encoders.DEFAULT_AQ_STRENGTH

        if args.dry_run:
            plan = sweep.build_sweep_plan(
                tl, args.sweep,
                at=preview_at, seconds=args.preview_seconds, output_dir=output_dir,
                default_crf=default_crf, default_preset=default_preset,
                default_psy_rd=default_psy_rd, default_aq_strength=default_aq_strength,
            )
            work_dir = args.work_dir or (args.gamedir / ".csm")
            print(f"# reference -> {plan.reference_path}")
            print(runner.dry_run(tool, plan.reference_job, work_dir))
            for vlabel, _overrides, output_path, job in plan.variant_jobs:
                print(f"# variant {vlabel} -> {output_path}")
                print(runner.dry_run(tool, job, work_dir))
            return 0

        results = sweep.run_sweep(
            tool, capabilities, tl, args.sweep,
            at=preview_at, seconds=args.preview_seconds, output_dir=output_dir,
            upload_mbps=args.upload_mbps,
            default_crf=default_crf, default_preset=default_preset,
            default_psy_rd=default_psy_rd, default_aq_strength=default_aq_strength,
        )
        print(sweep.format_results(results, upload_mbps=args.upload_mbps))
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
    p_probe.add_argument(
        "--frame", action="store_true",
        help="Write one still frame per camera to GAMEDIR/.csm/frames/cameraN.png, "
             "for reading off pixel coordinates (e.g. for --field-cutoff)",
    )
    p_probe.add_argument(
        "--frame-at", type=float, default=45.0,
        help="Seconds into the first segment to grab --frame from (default 45 -- past "
             "typical camera setup/positioning time, before assuming a fixed shot)",
    )
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
    p_sync.add_argument(
        "--clap-at", default=None, metavar="SEC|MM:SS|HH:MM:SS",
        help="Timestamp of a deliberate clap/clapperboard at midfield, on camera 1's own "
             "raw clock only (not any other camera, and not the aligned render timeline)",
    )
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
    p_render.add_argument(
        "--start", default=None, metavar="SEC|MM:SS|HH:MM:SS|CAM@...",
        help="Point on the common timeline, or a point in camera CAM's own raw "
             "footage (e.g. 2@5:24 -- 5:24 into camera 2), resolved via sync.yaml",
    )
    p_render.add_argument(
        "--end", default=None, metavar="SEC|MM:SS|HH:MM:SS|CAM@...",
        help="Same syntax as --start",
    )
    p_render.add_argument(
        "--duration", type=_parse_time, default=None, metavar="SEC|MM:SS|HH:MM:SS",
        help="Alternative to --end: this much time after --start",
    )
    p_render.add_argument("--fps", type=float, default=30.0)
    p_render.add_argument("--audio-cams", default="2,4")
    p_render.add_argument("--out-size", default="3840x2160", help="WxH, or 'native'")
    p_render.add_argument(
        "--field-cutoff", action="append", metavar="N=PIXELS",
        help="Repeatable. Pixels from the bottom of camera N's native frame that are "
             "high-priority field; everything above is pre-blurred so the encoder spends "
             "fewer bits on sky/stands. Measure from a still frame (e.g. `csm probe --frame`)",
    )
    p_render.add_argument("--profile", default="final-h265", choices=sorted(encoders.PROFILES))
    p_render.add_argument(
        "--crf", default=None,
        help=f"Overrides GAMEDIR/csm.yaml's persisted value, or the default "
             f"({encoders.DEFAULT_CRF}). Only meaningful with --profile final-h265",
    )
    p_render.add_argument(
        "--preset", default=None,
        help=f"Overrides GAMEDIR/csm.yaml's persisted value, or the default "
             f"({encoders.DEFAULT_PRESET}). Only meaningful with --profile final-h265",
    )
    p_render.add_argument(
        "--psy-rd", dest="psy_rd", default=None,
        help=f"Overrides GAMEDIR/csm.yaml's persisted value, or the default "
             f"({encoders.DEFAULT_PSY_RD}). Only meaningful with --profile final-h265",
    )
    p_render.add_argument(
        "--aq-strength", dest="aq_strength", default=None,
        help=f"Overrides GAMEDIR/csm.yaml's persisted value, or the default "
             f"({encoders.DEFAULT_AQ_STRENGTH}). Only meaningful with --profile final-h265",
    )
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
    p_preview.add_argument(
        "--start", default=None, metavar="SEC|MM:SS|HH:MM:SS|CAM@...",
        help="Point on the common timeline, or a point in camera CAM's own raw "
             "footage (e.g. 2@5:24 -- 5:24 into camera 2), resolved via sync.yaml",
    )
    p_preview.add_argument(
        "--end", default=None, metavar="SEC|MM:SS|HH:MM:SS|CAM@...",
        help="Same syntax as --start",
    )
    p_preview.add_argument(
        "--duration", type=_parse_time, default=None, metavar="SEC|MM:SS|HH:MM:SS",
        help="Alternative to --end: this much time after --start",
    )
    p_preview.add_argument("--fps", type=float, default=30.0)
    p_preview.add_argument("--audio-cams", default="2,4")
    p_preview.add_argument("--out-size", default="3840x2160", help="WxH, or 'native'")
    p_preview.add_argument(
        "--field-cutoff", action="append", metavar="N=PIXELS",
        help="Repeatable. Pixels from the bottom of camera N's native frame that are "
             "high-priority field; everything above is pre-blurred so the encoder spends "
             "fewer bits on sky/stands. Measure from a still frame (e.g. `csm probe --frame`)",
    )
    p_preview.add_argument("--preview-seconds", type=float, default=60.0)
    p_preview.add_argument(
        "--preview-at", action="append", metavar="middle|SEC|MM:SS|HH:MM:SS|PCT%",
        help="Repeatable; default is 'middle' plus an automatic window at the first "
             "moment all four cameras are live",
    )
    p_preview.add_argument("--preview-encoder", default="auto", metavar="auto|NAME")
    p_preview.add_argument(
        "--sweep", action="append", metavar="KEY=v1,v2,...",
        help="Repeatable. Recognized keys: crf, preset, encoder (libx265/libsvtav1), "
             "psy-rd, aq-strength (libx265 only). e.g. --sweep crf=21,23,25 "
             "--sweep preset=fast,medium",
    )
    p_preview.add_argument(
        "--crf", default=None,
        help=f"With --sweep: baseline CRF for dimensions not swept over. Overrides "
             f"GAMEDIR/csm.yaml's persisted value, or the default ({encoders.DEFAULT_CRF})",
    )
    p_preview.add_argument(
        "--preset", default=None,
        help=f"With --sweep: baseline preset for dimensions not swept over. Overrides "
             f"GAMEDIR/csm.yaml's persisted value, or the default ({encoders.DEFAULT_PRESET})",
    )
    p_preview.add_argument(
        "--psy-rd", dest="psy_rd", default=None,
        help=f"With --sweep: baseline psy-rd for dimensions not swept over. Overrides "
             f"GAMEDIR/csm.yaml's persisted value, or the default ({encoders.DEFAULT_PSY_RD})",
    )
    p_preview.add_argument(
        "--aq-strength", dest="aq_strength", default=None,
        help=f"With --sweep: baseline aq-strength for dimensions not swept over. Overrides "
             f"GAMEDIR/csm.yaml's persisted value, or the default ({encoders.DEFAULT_AQ_STRENGTH})",
    )
    p_preview.add_argument(
        "--upload-mbps", type=float, default=sweep.DEFAULT_UPLOAD_MBPS,
        help="Your actual upload speed, for --sweep's extrapolated upload-time column "
             f"(default {sweep.DEFAULT_UPLOAD_MBPS:g})",
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
