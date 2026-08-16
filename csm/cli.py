"""csm CLI: `csm probe` and `csm sync` (DESIGN.md Sec 12, Phase 1 subset)."""
from __future__ import annotations

import argparse
import subprocess
import sys
import warnings
from pathlib import Path

from csm import ffmpeg, probe, sources, sync


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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
