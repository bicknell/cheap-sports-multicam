"""DESIGN.md Sec 10.2, tests 1-9 (Phase 1 scope)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from csm import ffmpeg, probe, sources, sync

TOL = 1.0 / 60.0  # DESIGN.md Sec 6.0/10.2's precision gate, independent of output fps


@pytest.fixture(scope="module")
def tool():
    return ffmpeg.locate()


def _sync_game(tool, game_dir, **kwargs):
    kwargs.setdefault("skip_head", 2.0)
    kwargs.setdefault("n_windows", 6)
    kwargs.setdefault("window_len", 3.0)
    cams = sources.discover_cameras(game_dir)
    infos = {c: probe.probe_camera(tool, cams[c]) for c in cams}
    return sync.sync_game(tool, infos, **kwargs)


# ---- 1. gcc_phat_known_integer_delay ----

def test_gcc_phat_known_integer_delay():
    rng = np.random.default_rng(0)
    fs = 8000
    base = rng.standard_normal(fs * 4).astype(np.float32)
    for shift in (10, 50, 137, -80):
        if shift >= 0:
            a = base[: fs * 3]
            b = np.roll(base, shift)[: fs * 3]
        else:
            a = base[: fs * 3]
            b = np.roll(base, shift)[: fs * 3]
        est = sync.gcc_phat(a, b, fs=fs, max_lag_s=1.0)
        # np.roll(base, shift)[t] == base[t - shift] == a[t - shift], i.e. b[t] = a[t-shift]
        # our calibrated convention is b[t] = a[t + lag] => lag = -shift
        expected_samples = -shift
        assert abs(est.lag_s * fs - expected_samples) <= 1.0


# ---- 2. gcc_phat_fractional_delay ----

def test_gcc_phat_fractional_delay():
    # Broadband noise, not a sparse tone sum: PHAT whitens every frequency
    # bin to equal weight, so a signal with near-zero true energy between a
    # few discrete tones just amplifies numerical noise there instead of
    # locating the real delay. A frequency-domain phase shift of broadband
    # noise gives an exact fractional-sample delay without that trap.
    fs = 8000
    n = fs * 3
    rng = np.random.default_rng(1)
    a = rng.standard_normal(n).astype(np.float32)
    true_delay_s = 0.01234
    A = np.fft.rfft(a)
    freqs = np.fft.rfftfreq(n, d=1.0 / fs)
    b = np.fft.irfft(A * np.exp(-2j * np.pi * freqs * true_delay_s), n=n).astype(np.float32)

    est = sync.gcc_phat(a, b, fs=fs, max_lag_s=1.0)
    # b(t) = a(t - true_delay_s) here, i.e. b is delayed relative to a, which
    # in our calibrated convention (b[t] = a[t + lag_s]) makes lag_s negative.
    assert abs(est.lag_s - (-true_delay_s)) < 0.2 / fs


# ---- 3. gcc_phat_reports_low_confidence ----

def test_gcc_phat_reports_low_confidence():
    rng = np.random.default_rng(2)
    fs = 8000
    a = rng.standard_normal(fs * 2).astype(np.float32)
    b = rng.standard_normal(fs * 2).astype(np.float32)  # unrelated noise
    est = sync.gcc_phat(a, b, fs=fs, max_lag_s=1.0)
    assert est.psr < sync.MARGINAL_PSR


# ---- 4. sync_detects_injected_offsets ----

def test_sync_detects_injected_offsets(tool, synthetic_game):
    offsets = {1: 0.0, 2: 1.6, 3: 701.4, 4: 698.9}
    durations = {1: 760.0, 2: 760.0, 3: 60.0, 4: 60.0}
    game_dir = synthetic_game(offsets, durations)
    result = _sync_game(tool, game_dir, skip_head=2.0, n_windows=4, window_len=3.0)
    for cam, expected in offsets.items():
        assert abs(result.offsets[cam] - expected) <= TOL


# ---- 5. sync_creation_time_prior_narrows_search ----

def test_sync_creation_time_prior_narrows_search(tool, synthetic_game):
    offsets = {1: 0.0, 2: 12.0, 3: 12.0, 4: 12.0}
    durations = {1: 60.0, 2: 60.0, 3: 60.0, 4: 60.0}
    game_dir = synthetic_game(offsets, durations)

    hinted = _sync_game(tool, game_dir, prior_window=5.0)
    blind = _sync_game(tool, game_dir, no_time_prior=True, max_offset=30.0)

    assert abs(hinted.offsets[2] - offsets[2]) <= TOL
    # Blind search has no creation_time hint to narrow its window, so it's
    # inherently coarser than the hinted path -- design's own text asks for
    # agreement "to within a frame" here, not the 1/60s precision gate.
    assert abs(hinted.offsets[2] - blind.offsets[2]) <= 1.0 / 30.0


# ---- 6. sync_unsynced_camera_clock ----

def test_sync_unsynced_camera_clock(tool, synthetic_game):
    offsets = {1: 0.0, 2: 1.0, 3: 5.0, 4: 6.0}
    durations = {1: 90.0, 2: 90.0, 3: 90.0, 4: 90.0}
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    creation_times = {
        1: base,
        2: base + timedelta(seconds=offsets[2]),
        3: base + timedelta(seconds=offsets[3] + 900.0),  # cam 3's clock is wrong by 900s
        4: base + timedelta(seconds=offsets[4]),
    }
    game_dir = synthetic_game(offsets, durations, creation_times=creation_times)
    result = _sync_game(tool, game_dir, max_offset=60.0)

    for cam, expected in offsets.items():
        assert abs(result.offsets[cam] - expected) <= TOL

    link_13 = next(m for m in result.measurements if m.pair == (1, 3))
    assert link_13.method == "blind"

    assert any("camera 3" in note for note in result.prior_notes)


# ---- 7. sync_cross_tripod_bias_averages_out ----

def test_sync_cross_tripod_bias_averages_out():
    rng = np.random.default_rng(3)
    true_lag = 5.0
    n = 24
    real_t = np.arange(n) * 10.0
    biases = rng.uniform(-0.18, 0.18, n)
    lags = true_lag + biases
    psrs = rng.uniform(10, 30, n)
    samples = list(zip(real_t.tolist(), lags.tolist(), psrs.tolist()))

    lag, sigma, n_used = sync._robust_combine(samples)
    assert abs(lag - true_lag) < 0.035


# ---- 8. sync_disconnected_graph ----

def test_sync_disconnected_graph(tool, synthetic_game):
    # No audio AND no creation_time: a camera with only audio missing still
    # degrades gracefully via its clock hint (Sec 6.6 step 1); it's truly
    # disconnected only when neither path back to the reference exists.
    offsets = {1: 0.0, 2: 1.0, 3: 5.0, 4: 6.0}
    durations = {1: 60.0, 2: 60.0, 3: 60.0, 4: 60.0}
    game_dir = synthetic_game(offsets, durations, no_audio=(3,), no_creation_time=(3,))

    with pytest.raises(ValueError) as exc_info:
        _sync_game(tool, game_dir)
    msg = str(exc_info.value)
    assert "camera 3" in msg
    assert "--offset" in msg


# ---- 9. sync_overrides ----

def test_sync_overrides():
    offsets = {1: 0.0, 2: 1.0, 3: 2.0}
    result = sync.apply_overrides(offsets, offset_overrides={3: 2.5})
    assert result[3] == 2.5

    result2 = sync.apply_overrides(offsets, nudge_overrides={3: 0.033})
    assert result2[3] == pytest.approx(2.033)

    result3 = sync.apply_overrides(offsets, offset_overrides={3: 2.5}, nudge_overrides={3: 0.033})
    assert result3[3] == pytest.approx(2.533)


def test_parse_creation_time_from_filename():
    """Older camera firmware writes no creation_time tag at all, but names
    every segment YYYYMMDDHHMMSS_NNNNNN.ext -- used as a fallback prior."""
    got = probe._parse_creation_time_from_filename("20250914155248_000001.MP4")
    assert got == datetime(2025, 9, 14, 15, 52, 48, tzinfo=timezone.utc)

    # NORM_ (current camera) filenames don't match -- no false positive.
    assert probe._parse_creation_time_from_filename("NORM_00001.MP4") is None
    assert probe._parse_creation_time_from_filename("not_a_timestamp.MP4") is None
    # Invalid calendar date in the digits (month 13) -- rejected, not garbage.
    assert probe._parse_creation_time_from_filename("20251314000000_000001.MP4") is None
