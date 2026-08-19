"""`--sweep`: settle encoder settings by measurement, not argument (Sec
7.3). Renders the same short window once per KEY=v1,v2,... variant plus one
lossless reference, scores each variant against the reference with
libvmaf (preferred) -> xpsnr -> ssim -- whichever `check_capabilities()`
finds first -- and reports wall time, file size, extrapolated full-game
size, and extrapolated upload time at a configurable connection speed
(DEFAULT_UPLOAD_MBPS unless overridden -- everyone's upload speed differs
and changes over time, so this is never assumed silently).
"""
from __future__ import annotations

import dataclasses
import itertools
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from csm import encoders, host, runner
from csm.ffmpeg import Capabilities, Tool
from csm.filtergraph import build_filtergraph
from csm.plan import FfmpegJob
from csm.preview import resolve_windows
from csm.timeline import Timeline

DEFAULT_UPLOAD_MBPS = 40.0


def parse_sweep_specs(specs: list[str]) -> dict[str, list[str]]:
    dims: dict[str, list[str]] = {}
    for spec in specs:
        key, _, values = spec.partition("=")
        dims[key] = values.split(",")
    return dims


def variants(dims: dict[str, list[str]]) -> list[dict[str, str]]:
    if not dims:
        return [{}]
    keys = list(dims)
    return [dict(zip(keys, combo)) for combo in itertools.product(*(dims[k] for k in keys))]


def _variant_label(overrides: dict[str, str]) -> str:
    return "_".join(f"{k}-{v}" for k, v in sorted(overrides.items())) or "default"


@dataclass
class SweepResult:
    label: str
    overrides: dict[str, str]
    wall_seconds: float
    size_bytes: int
    extrapolated_full_bytes: float
    extrapolated_upload_seconds: float
    score: float | None
    metric: str


def _run_metric(tool: Tool, lavfi: str, variant_path: Path, reference_path: Path) -> str:
    argv = [
        tool.ffmpeg, "-hide_banner", "-y",
        "-i", str(variant_path), "-i", str(reference_path),
        "-lavfi", lavfi, "-f", "null", "-",
    ]
    return subprocess.run(argv, capture_output=True, text=True).stderr


def score(tool: Tool, capabilities: Capabilities, variant_path: Path, reference_path: Path) -> tuple[float | None, str]:
    """Sec 7.3: prefer libvmaf, fall back to xpsnr, then ssim -- always
    report which metric was used so scores are never silently compared
    across metrics. Regexes verified against this ffmpeg build's actual
    stderr output, not assumed from docs."""
    if capabilities.has_libvmaf:
        # libvmaf's own n_threads default (0) measured at ~160% CPU on a
        # 16-core machine -- effectively single-threaded, and roughly 2x
        # the cost of the encode it's scoring. Match total_cores explicitly.
        n_threads = host.detect_host().total_cores
        lavfi = f"libvmaf=n_threads={n_threads}"
        m = re.search(r"VMAF score:\s*([\d.]+)", _run_metric(tool, lavfi, variant_path, reference_path))
        if m:
            return float(m.group(1)), "vmaf"
    m = re.search(r"XPSNR\s+y:\s*([\d.]+)", _run_metric(tool, "xpsnr", variant_path, reference_path))
    if m:
        return float(m.group(1)), "xpsnr"
    m = re.search(r"All:([\d.]+)", _run_metric(tool, "ssim", variant_path, reference_path))
    if m:
        return float(m.group(1)), "ssim"
    return None, "none"


def _render_reference(tool: Tool, window_tl: Timeline, output_path: Path) -> None:
    duration = window_tl.duration + 0.5 / float(window_tl.fps)
    job = FfmpegJob(
        graph=build_filtergraph(window_tl),
        output_args=["-c:v", "ffv1", "-c:a", "flac", "-t", f"{duration:.6f}"],
        output_path=output_path,
    )
    runner.run_job(tool, job)


def run_sweep(
    tool: Tool,
    capabilities: Capabilities,
    timeline: Timeline,
    sweep_specs: list[str],
    *,
    at: list[str],
    seconds: float,
    output_dir: Path,
    upload_mbps: float = DEFAULT_UPLOAD_MBPS,
    default_crf: str = encoders.DEFAULT_CRF,
    default_preset: str = encoders.DEFAULT_PRESET,
) -> list[SweepResult]:
    label, t0, t1 = resolve_windows(timeline, at or ["middle"], seconds)[0]
    window_tl = dataclasses.replace(timeline, t0=t0, t1=t1)

    output_dir.mkdir(parents=True, exist_ok=True)
    reference_path = output_dir / "sweep_reference.mkv"
    _render_reference(tool, window_tl, reference_path)

    results = []
    for overrides in variants(parse_sweep_specs(sweep_specs)):
        vlabel = _variant_label(overrides)
        output_path = output_dir / f"sweep_{vlabel}.mp4"
        args = encoders.sweep_args(
            window_tl,
            encoder=overrides.get("encoder", "libx265"),
            crf=overrides.get("crf", default_crf),
            preset=overrides.get("preset", default_preset),
            psy_rd=overrides.get("psy-rd", encoders.DEFAULT_PSY_RD),
            aq_strength=overrides.get("aq-strength", encoders.DEFAULT_AQ_STRENGTH),
        )
        job = FfmpegJob(graph=build_filtergraph(window_tl), output_args=args, output_path=output_path)

        start = time.monotonic()
        runner.run_job(tool, job)
        wall = time.monotonic() - start

        size = output_path.stat().st_size
        ratio = timeline.duration / window_tl.duration
        extrapolated_bytes = size * ratio
        upload_seconds = extrapolated_bytes * 8 / (upload_mbps * 1_000_000)

        variant_score, metric = score(tool, capabilities, output_path, reference_path)
        results.append(SweepResult(
            label=vlabel, overrides=overrides, wall_seconds=wall, size_bytes=size,
            extrapolated_full_bytes=extrapolated_bytes,
            extrapolated_upload_seconds=upload_seconds, score=variant_score, metric=metric,
        ))
    return results


def format_results(results: list[SweepResult], upload_mbps: float = DEFAULT_UPLOAD_MBPS) -> str:
    lines = [f"upload(h) assumes {upload_mbps:g} Mbps -- override with --upload-mbps"]
    header = (
        f"{'variant':<30} {'wall(s)':>8} {'size(MB)':>9} {'full(GB)':>9} "
        f"{'upload(h)':>10} {'score':>8} {'metric':>7}"
    )
    lines.append(header)
    for r in results:
        score_str = f"{r.score:.2f}" if r.score is not None else "n/a"
        lines.append(
            f"{r.label:<30} {r.wall_seconds:>8.1f} {r.size_bytes / 1e6:>9.1f} "
            f"{r.extrapolated_full_bytes / 1e9:>9.2f} {r.extrapolated_upload_seconds / 3600:>10.2f} "
            f"{score_str:>8} {r.metric:>7}"
        )
    return "\n".join(lines)
