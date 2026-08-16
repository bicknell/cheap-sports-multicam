"""Sec 10.2: synthetic_game builds real cameraN/*.mp4 fixtures. The shared
sync-detectable signal is a single broadband noise buffer generated once per
game and sliced per camera by its offset -- real ambient audio (crowd, wind,
footsteps) is broadband and non-periodic, and GCC-PHAT wants exactly that.
A neat deterministic formula (an impulse train, a chirp) is tempting for
"reproducible", but both turn out to be actively hostile to PHAT over a wide
blind-search range: an impulse train is periodic (near-tied correlation
peaks every period), and a linear chirp's swept spectrum is close enough to
flat that PHAT's whitening erases the very thing that would disambiguate
it, so both can lock onto a confident, wrong lag once the search window
exceeds their (rather short) unique-content span. A slice of real noise
has neither problem, and it's what the actual production code correlates
against anyway. anoisesrc has no seed in this ffmpeg build, but that only
means the buffer differs across test *runs* -- it's generated exactly once
per synthetic_game() call and every camera slices the same buffer, which is
all correctness here depends on.

Video is a cheap placeholder (a real stream is required by probe.py, but
its content is irrelevant to Phase 1's sync-only tests).
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _make_master_noise(path: Path, duration: float, sample_rate: int) -> None:
    argv = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i",
        f"anoisesrc=color=white:amplitude=0.9:sample_rate={sample_rate}:duration={duration}",
        "-c:a", "pcm_s16le", str(path),
    ]
    subprocess.run(argv, check=True, capture_output=True)


def _make_segment(path: Path, *, width: int, height: int, fps: int, duration: float,
                   master_path: Path | None, master_offset: float, sample_rate: int,
                   creation_time: datetime | None, with_audio: bool) -> None:
    argv = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"color=c=gray:s={width}x{height}:rate={fps}:duration={duration}",
    ]
    if with_audio:
        argv += [
            "-ss", f"{master_offset:.6f}", "-t", f"{duration:.6f}", "-i", str(master_path),
            "-map", "0:v", "-map", "1:a", "-c:a", "aac",
        ]
    else:
        argv += ["-map", "0:v"]
    argv += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if creation_time is not None:
        argv += ["-metadata", f"creation_time={creation_time.strftime('%Y-%m-%dT%H:%M:%S.%fZ')}"]
    argv += [str(path)]
    subprocess.run(argv, check=True, capture_output=True)


@pytest.fixture
def synthetic_game(tmp_path):
    def _make(
        offsets: dict[int, float],
        durations: dict[int, float],
        *,
        missing: tuple[int, ...] = (),
        no_audio: tuple[int, ...] = (),
        no_creation_time: tuple[int, ...] = (),
        sizes: dict[int, tuple[int, int]] | None = None,
        segments: int = 1,
        creation_times: dict[int, datetime] | None = None,
        fps: int = 30,
        sample_rate: int = 16000,
        noise_amp: float = 0.05,  # unused, kept for call-site compatibility
    ) -> Path:
        game_dir = tmp_path / "game"
        game_dir.mkdir(exist_ok=True)
        base_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        active = [cam for cam in (1, 2, 3, 4) if cam not in missing]
        needs_audio = [cam for cam in active if cam not in no_audio]

        master_path = None
        master_t0 = 0.0
        if needs_audio:
            starts = [offsets.get(cam, 0.0) for cam in needs_audio]
            ends = [offsets.get(cam, 0.0) + durations.get(cam, durations.get(1, 10.0))
                    for cam in needs_audio]
            master_t0 = min(starts) - 1.0
            master_duration = max(ends) - master_t0 + 1.0
            master_path = tmp_path / "master_noise.wav"
            _make_master_noise(master_path, master_duration, sample_rate)

        for cam in active:
            off = offsets.get(cam, 0.0)
            dur = durations.get(cam, durations.get(1, 10.0))
            w, h = (sizes or {}).get(cam, (160, 90))
            cam_dir = game_dir / f"camera{cam}"
            cam_dir.mkdir(exist_ok=True)

            if cam in no_creation_time:
                ct0 = None
            elif creation_times is not None and cam in creation_times:
                ct0 = creation_times[cam]
            else:
                ct0 = base_time + timedelta(seconds=off)

            seg_dur = dur / segments
            for seg_i in range(segments):
                seg_path = cam_dir / f"NORM_{seg_i + 1:05d}.MP4"
                seg_start = seg_i * seg_dur
                _make_segment(
                    seg_path, width=w, height=h, fps=fps, duration=seg_dur,
                    master_path=master_path, master_offset=(off + seg_start) - master_t0,
                    sample_rate=sample_rate,
                    creation_time=(ct0 + timedelta(seconds=seg_start)) if ct0 is not None else None,
                    with_audio=cam not in no_audio,
                )
        return game_dir

    return _make
