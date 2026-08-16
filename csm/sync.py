"""Camera-to-camera sync: GCC-PHAT audio correlation, creation_time hints,
tripod-topology weighted-least-squares solve, and the sync.yaml contract.
See DESIGN.md Sec 6 and SYNCHRONIZATION.md for the reasoning behind each
piece; this module is the implementation.
"""
from __future__ import annotations

import subprocess
import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import yaml
from scipy.stats import theilslopes

from csm.ffmpeg import Tool
from csm.probe import CameraInfo

# DESIGN.md Sec 6.3 gives 8/4 as the confident/marginal psr lines, but that's
# calibrated against real acoustic recordings. For pure uncorrelated white
# noise, extreme-value statistics over a realistic search window (a
# handful of seconds at 8kHz, 4x interpolated) alone routinely produce a
# "peak" 6-8x the sidelobe median with no real correlation at all --
# verified empirically here, not assumed. Real archived-game measurements
# in this codebase's own validation run consistently well into the tens,
# so these thresholds are raised to keep clear of that noise floor.
CONFIDENT_PSR = 20.0
MARGINAL_PSR = 10.0

# The only links ever measured directly (Sec 6.4): both intra-tripod pairs,
# and BOTH cross-tripod pairs so the solve is over-determined by one degree
# of freedom and can flag an inconsistent link instead of silently trusting it.
TRIPODS = ((1, 2), (3, 4))
FIXED_LINKS = [(1, 2), (3, 4), (1, 3), (2, 4)]


def link_kind(pair: tuple[int, int]) -> str:
    return "intra-tripod" if pair in TRIPODS else "cross-tripod"


# ---------------------------------------------------------------- Sec 6.3 --

@dataclass
class DelayEstimate:
    lag_s: float
    psr: float


def gcc_phat(a: np.ndarray, b: np.ndarray, fs: int, max_lag_s: float,
             interp: int = 4) -> DelayEstimate:
    """PHAT-whitened cross-correlation. Returns lag such that b[t] == a[t + lag]
    (calibrated against a synthetic known-shift signal -- see test 1/2), and a
    peak-to-sidelobe-ratio confidence score (psr >= 8 confident, 4-8 marginal,
    < 4 reject -- Sec 6.3)."""
    if len(a) == 0 or len(b) == 0:
        return DelayEstimate(lag_s=0.0, psr=0.0)

    n = len(a) + len(b)
    nfft = 1
    while nfft < n:
        nfft *= 2

    A = np.fft.rfft(a, n=nfft)
    B = np.fft.rfft(b, n=nfft)
    R = A * np.conj(B)
    mag = np.abs(R)
    mag[mag == 0] = 1e-12
    R_phat = R / mag

    nfft_i = nfft * interp
    cc = np.fft.irfft(R_phat, n=nfft_i)
    cc = np.concatenate((cc[-nfft_i // 2:], cc[:nfft_i // 2]))
    fs_i = fs * interp
    center = nfft_i // 2
    max_lag_samples = max(1, int(max_lag_s * fs_i))
    lo = max(0, center - max_lag_samples)
    hi = min(nfft_i, center + max_lag_samples + 1)
    window = cc[lo:hi]
    if len(window) == 0:
        return DelayEstimate(lag_s=0.0, psr=0.0)
    peak_idx = int(np.argmax(np.abs(window))) + lo

    if 0 < peak_idx < nfft_i - 1:
        y0, y1, y2 = cc[peak_idx - 1], cc[peak_idx], cc[peak_idx + 1]
        denom = y0 - 2 * y1 + y2
        delta = 0.5 * (y0 - y2) / denom if denom != 0 else 0.0
    else:
        delta = 0.0

    lag_s = ((peak_idx - center) + delta) / fs_i

    guard = int(0.005 * fs_i)
    sidelobe_lo = max(0, peak_idx - lo - guard)
    sidelobe_hi = min(len(window), peak_idx - lo + guard + 1)
    sidelobe = np.concatenate((window[:sidelobe_lo], window[sidelobe_hi:]))
    peak_val = abs(cc[peak_idx])
    median_abs = np.median(np.abs(sidelobe)) if len(sidelobe) else 1e-12
    psr = peak_val / max(median_abs, 1e-12)

    return DelayEstimate(lag_s=float(lag_s), psr=float(psr))


# ---------------------------------------------------------------- Sec 6.2 --

def extract_audio_window(tool: Tool, path: Path, local_start: float, length: float,
                          fs: int = 8000) -> np.ndarray:
    """The bandpass is not optional (Sec 6.2): outdoor wind rumble below
    ~200 Hz dominates the PHAT spectrum otherwise."""
    argv = [
        tool.ffmpeg, "-v", "error",
        "-ss", f"{local_start:.3f}", "-t", f"{length:.3f}",
        "-i", str(path),
        "-vn", "-map", "0:a:0",
        "-ac", "1", "-ar", str(fs),
        "-af", "highpass=f=250,lowpass=f=3500",
        "-c:a", "pcm_f32le", "-f", "f32le", "-",
    ]
    result = subprocess.run(argv, capture_output=True, check=True)
    return np.frombuffer(result.stdout, dtype=np.float32)


# ------------------------------------------------------------- timelines --

@dataclass
class Timeline:
    cam: int
    segments: list[Path]
    starts: list[float]
    total_duration: float
    creation0: datetime | None
    has_audio: bool


def build_timeline(info: CameraInfo) -> Timeline | None:
    if not info.segments:
        return None
    starts = []
    t = 0.0
    for seg in info.segments:
        starts.append(t)
        t += seg.duration
    return Timeline(
        cam=info.cam,
        segments=[s.path for s in info.segments],
        starts=starts,
        total_duration=t,
        creation0=info.segments[0].creation_time,
        has_audio=info.has_audio,
    )


def local_to_segment(tl: Timeline, local_t: float) -> tuple[Path, float] | None:
    if local_t < 0 or local_t > tl.total_duration:
        return None
    idx = 0
    for i, s in enumerate(tl.starts):
        if s <= local_t:
            idx = i
        else:
            break
    return tl.segments[idx], local_t - tl.starts[idx]


def creation_time_hint(tlA: Timeline, tlB: Timeline) -> float | None:
    """offset(b) - offset(a) implied by creation_time tags, or None if
    either is missing (Sec 6.1: 'the hint is missing or ... ')."""
    if tlA.creation0 is None or tlB.creation0 is None:
        return None
    return (tlB.creation0 - tlA.creation0).total_seconds()


# --------------------------------------------------------- Sec 6.4/6.1 ----

@dataclass
class Measurement:
    pair: tuple[int, int]
    lag: float
    sigma: float
    psr: float
    windows_used: int
    windows_total: int
    kind: str      # "intra-tripod" | "cross-tripod"
    method: str     # "creation-time-prior" | "blind" | "clap"


def _pick_windows(tlA: Timeline, tlB: Timeline, n_windows: int, window_len: float,
                   skip_head: float = 60.0) -> list[float]:
    """Candidate real-world (POSIX) timestamps spread across the mutual
    overlap implied by creation_time, skipping skip_head seconds of
    startup noise (Sec 6.4)."""
    if tlA.creation0 is None or tlB.creation0 is None:
        return []
    real_start = max(tlA.creation0.timestamp(), tlB.creation0.timestamp()) + skip_head
    real_end = min(
        tlA.creation0.timestamp() + tlA.total_duration,
        tlB.creation0.timestamp() + tlB.total_duration,
    ) - window_len
    if real_end <= real_start:
        return []
    if n_windows <= 1:
        return [real_start]
    return list(np.linspace(real_start, real_end, n_windows))


def _robust_combine(samples: list[tuple[float, float, float]]) -> tuple[float, float, int]:
    """samples: list of (real_t, lag, psr) already filtered to psr >= MARGINAL_PSR.
    MAD-outlier-rejects on the raw lags, then fits Theil-Sen (lag vs. window
    time) and returns (intercept, sigma, n_kept) -- Sec 6.4: 'Fit with MAD
    outlier rejection then Theil-Sen; the intercept is the offset.' The
    intercept is evaluated at the first kept window's time, which absorbs
    Sec 6.5's diagnostic-only clock drift (<2 frames/90 min) without any
    drift-correction machinery."""
    t = np.array([s[0] for s in samples])
    vals = np.array([s[1] for s in samples])

    if len(vals) >= 3:
        med = np.median(vals)
        mad = np.median(np.abs(vals - med)) * 1.4826
        if mad > 0:
            keep = np.abs(vals - med) < 5 * mad
            if keep.sum() >= 2:
                t, vals = t[keep], vals[keep]

    n = len(vals)
    t0 = t.min()
    x = t - t0
    x_eval = float(np.median(x))  # temporal centroid: minimum-variance evaluation point

    if n >= 2 and np.ptp(x) > 0:
        slope, intercept, _, _ = theilslopes(vals, x)
        lag = float(slope * x_eval + intercept)
        resid = vals - (slope * x + intercept)
        sigma = float(np.std(resid) / np.sqrt(n)) if n > 1 else 0.05
    else:
        lag = float(np.median(vals))
        sigma = float(np.std(vals) / np.sqrt(n)) if n > 1 else 0.05

    return lag, max(sigma, 1e-4), n


def _windowed_measurement(tool: Tool, tlA: Timeline, tlB: Timeline, *,
                           n_windows: int, window_len: float, prior_window: float,
                           min_confidence: float,
                           skip_head: float = 60.0) -> tuple[float, float, float, int, int] | None:
    """Returns (lag, sigma, mean_psr, n_used, n_total) or None if no usable windows."""
    real_times = _pick_windows(tlA, tlB, n_windows, window_len, skip_head=skip_head)
    if not real_times:
        return None

    samples = []
    for real_t in real_times:
        local_a = real_t - tlA.creation0.timestamp()
        local_b = real_t - tlB.creation0.timestamp()
        posA = local_to_segment(tlA, local_a)
        posB = local_to_segment(tlB, local_b)
        if posA is None or posB is None:
            continue
        segA, offA = posA
        segB, offB = posB
        try:
            wa = extract_audio_window(tool, segA, offA, window_len)
            wb = extract_audio_window(tool, segB, offB, window_len)
        except subprocess.CalledProcessError:
            continue
        if len(wa) < 100 or len(wb) < 100:
            continue
        est = gcc_phat(wa, wb, fs=8000, max_lag_s=prior_window)
        if est.psr < MARGINAL_PSR:
            continue
        # wa/wb nominally start at the same real instant, so -(local_b-local_a)
        # is the hinted offset(b)-offset(a); est.lag is audio's correction to it.
        lag = -(local_b - local_a) + est.lag_s
        samples.append((real_t, lag, est.psr))

    n_total = len(real_times)
    if not samples:
        return None
    lag, sigma, n_used = _robust_combine(samples)
    mean_psr = float(np.mean([s[2] for s in samples]))
    if mean_psr < min_confidence and n_used < max(2, n_total // 3):
        return None
    return lag, sigma, mean_psr, n_used, n_total


def _blind_measurement(tool: Tool, tlA: Timeline, tlB: Timeline, *,
                        window_len: float, max_offset: float,
                        skip_head: float = 60.0) -> tuple[float, float, float] | None:
    """Decimated coarse search over +/- max_offset with no creation_time hint
    (Sec 6.1's 'automatic blind fallback'). A short window from A is
    correlated against as much of B as is available, in one FFT sized to
    cover the whole search range.

    The probe point on A must be chosen far enough into A's recording that
    it's still guaranteed to overlap B even in the worst case -- B starting
    up to max_offset *after* A. A probe taken near A's own start (as if B
    always started first) silently finds nothing whenever B is actually the
    later camera, which is exactly the case blind search exists to handle.
    So the probe sits at skip_head + max_offset into A, and B's comparison
    window is sized generously enough to still cover B starting up to
    max_offset *before* A too.
    """
    a_start = skip_head + max_offset
    if tlA.total_duration <= a_start + window_len:
        return None
    posA = local_to_segment(tlA, a_start)
    if posA is None:
        return None
    segA, offA = posA
    try:
        wa = extract_audio_window(tool, segA, offA, window_len)
    except subprocess.CalledProcessError:
        return None

    # b_start avoids literal local time 0.0: extracting from the exact start
    # of an AAC-encoded file can include the codec's own priming/encoder
    # delay (a fixed ~1024-sample offset), which is invisible everywhere
    # else because the hint-guided path never happens to anchor exactly on
    # a segment boundary.
    b_start = 0.5
    b_len = min(a_start + max_offset + window_len, tlB.total_duration - b_start)
    posB = local_to_segment(tlB, b_start)
    if posB is None or b_len < window_len:
        return None
    segB, offB = posB
    try:
        wb = extract_audio_window(tool, segB, offB, b_len)
    except subprocess.CalledProcessError:
        return None
    if len(wa) < 100 or len(wb) < 100:
        return None

    # wb[y] == wa[y + lag_s] (gcc_phat's calibrated convention). wa's content
    # at index k is signal(offset(A) + a_start + k/fs); wb's content at index
    # m is signal(offset(B) + b_start + m/fs). Equating arguments:
    # offset(B) - offset(A) = a_start - b_start + lag_s.
    est = gcc_phat(wa, wb, fs=8000, max_lag_s=a_start + max_offset)
    lag = a_start - b_start + est.lag_s
    if abs(lag) > max_offset:
        return None
    sigma = 0.02 if est.psr >= CONFIDENT_PSR else 0.08
    return lag, sigma, est.psr


def measure_link(tool: Tool, tlA: Timeline, tlB: Timeline, pair: tuple[int, int], *,
                  n_windows: int = 24, window_len: float = 20.0, prior_window: float = 5.0,
                  min_confidence: float = 6.0, max_offset: float = 1800.0,
                  force_blind: bool = False, skip_head: float = 60.0) -> Measurement | None:
    """Sec 6.1 flow for one link: hint narrows the window, audio decides;
    silently escalate to blind search if the hinted window doesn't pan out.
    force_blind (--no-time-prior) skips straight to blind search, for
    testing the fallback path deliberately. skip_head is the startup-noise
    skip from Sec 6.4 (60s in production; tests use a much smaller value
    so short synthetic fixtures still have a usable window)."""
    kind = link_kind(pair)
    hint = None if force_blind else creation_time_hint(tlA, tlB)

    if hint is not None:
        result = _windowed_measurement(
            tool, tlA, tlB, n_windows=n_windows, window_len=window_len,
            prior_window=prior_window, min_confidence=min_confidence, skip_head=skip_head,
        )
        if result is not None:
            lag, sigma, psr, n_used, n_total = result
            return Measurement(pair=pair, lag=lag, sigma=sigma, psr=psr,
                                windows_used=n_used, windows_total=n_total,
                                kind=kind, method="creation-time-prior")

    # Missing hint, or hint-guided search found nothing usable: blind fallback.
    result = _blind_measurement(tool, tlA, tlB, window_len=window_len, max_offset=max_offset,
                                 skip_head=skip_head)
    if result is None:
        return None
    lag, sigma, psr = result
    return Measurement(pair=pair, lag=lag, sigma=sigma, psr=psr,
                        windows_used=1, windows_total=1, kind=kind, method="blind")


def measure_clap(tool: Tool, timelines: dict[int, Timeline], pair: tuple[int, int],
                  clap_at_seconds: float, *, reference_cam: int = 1,
                  search_half_width: float = 2.0) -> Measurement:
    """Sec 6.7: --clap-at points the correlator directly at a shared
    impulsive transient, worth a direct measurement good to a few
    milliseconds instead of statistical averaging. clap_at_seconds is the
    clap's timestamp on reference_cam's own clock; every other camera's
    matching window is located via its creation_time offset from
    reference_cam (a clap heard by all four cameras should land within
    search_half_width of that hint)."""
    ref_tl = timelines.get(reference_cam)
    if ref_tl is None or ref_tl.creation0 is None:
        raise ValueError(f"--clap-at needs creation_time on camera {reference_cam}")
    real_t = ref_tl.creation0.timestamp() + clap_at_seconds

    windows = {}
    for cam in pair:
        tl = timelines[cam]
        if tl.creation0 is None:
            raise ValueError(f"--clap-at needs creation_time on camera {cam}")
        local = real_t - tl.creation0.timestamp()
        pos = local_to_segment(tl, max(0.0, local - search_half_width))
        if pos is None:
            raise ValueError(f"clap-at is outside camera {cam}'s recording")
        seg, off = pos
        windows[cam] = (local, extract_audio_window(tool, seg, off, search_half_width * 2))

    i, j = pair
    local_a, wa = windows[i]
    local_b, wb = windows[j]
    est = gcc_phat(wa, wb, fs=8000, max_lag_s=search_half_width)
    lag = -(local_b - local_a) + est.lag_s
    return Measurement(pair=pair, lag=lag, sigma=0.003, psr=est.psr,
                        windows_used=1, windows_total=1,
                        kind=link_kind(pair), method="clap")


# --------------------------------------------------------------- Sec 6.4 --

@dataclass
class SolveResult:
    offsets: dict[int, float]
    residuals: dict[tuple[int, int], float]
    max_residual: float
    inconsistent_links: list[tuple[int, int]]


def solve_topology(measurements: list[Measurement], cams=(1, 2, 3, 4),
                    reference_cam: int = 1) -> SolveResult:
    """Weighted least squares over whatever links were actually measured
    (a camera can be missing a link and the graph can still be solvable).
    Raises ValueError naming the unreachable camera if the graph is
    disconnected from reference_cam (Sec 6.6 degradation ladder, step 4)."""
    others = [c for c in cams if c != reference_cam]
    col = {c: i for i, c in enumerate(others)}

    # connectivity check via BFS over measured links
    adj: dict[int, set[int]] = {c: set() for c in cams}
    for m in measurements:
        i, j = m.pair
        adj[i].add(j)
        adj[j].add(i)
    seen = {reference_cam}
    frontier = [reference_cam]
    while frontier:
        node = frontier.pop()
        for nb in adj[node]:
            if nb not in seen:
                seen.add(nb)
                frontier.append(nb)
    unreachable = [c for c in cams if c not in seen]
    if unreachable:
        raise ValueError(
            f"sync graph is disconnected: camera {unreachable[0]} has no usable link "
            f"back to camera {reference_cam}. Use --offset {unreachable[0]}=SEC to anchor it."
        )

    n = len(measurements)
    A = np.zeros((n, len(others)))
    b = np.zeros(n)
    w = np.zeros(n)
    for row, m in enumerate(measurements):
        i, j = m.pair
        if i != reference_cam:
            A[row, col[i]] -= 1
        if j != reference_cam:
            A[row, col[j]] += 1
        b[row] = m.lag
        w[row] = 1.0 / (m.sigma ** 2)

    sqrt_w = np.sqrt(w)
    Aw = A * sqrt_w[:, None]
    bw = b * sqrt_w
    x, *_ = np.linalg.lstsq(Aw, bw, rcond=None)

    offsets = {reference_cam: 0.0}
    for c, i in col.items():
        offsets[c] = float(x[i])

    residuals = {}
    inconsistent = []
    for m in measurements:
        i, j = m.pair
        predicted = offsets[j] - offsets[i]
        resid = m.lag - predicted
        residuals[m.pair] = resid
        if abs(resid) > 3 * m.sigma:
            inconsistent.append(m.pair)

    max_residual = max((abs(r) for r in residuals.values()), default=0.0)
    return SolveResult(offsets=offsets, residuals=residuals, max_residual=max_residual,
                        inconsistent_links=inconsistent)


# --------------------------------------------------------------- Sec 6.6 --

def apply_overrides(offsets: dict[int, float], offset_overrides: dict[int, float] | None = None,
                     nudge_overrides: dict[int, float] | None = None) -> dict[int, float]:
    """Sec 6.6: sync.yaml -> --offset N=SEC -> --nudge N=+-SEC. --offset
    replaces a camera's offset outright ('manual's call, always wins');
    --nudge adds to whatever value is currently in effect."""
    result = dict(offsets)
    for cam, val in (offset_overrides or {}).items():
        result[cam] = val
    for cam, delta in (nudge_overrides or {}).items():
        result[cam] = result.get(cam, 0.0) + delta
    return result


def check_prior_agreement(timelines: dict[int, Timeline], offsets: dict[int, float],
                           reference_cam: int = 1,
                           prior_tolerance: float = 5.0) -> list[str]:
    """Sec 6.1: compare each camera's final offset to creation_time and
    report disagreement -- feedback, never a failure. If every camera
    disagrees by roughly the same amount, that's a clock-set problem and
    gets one message instead of several."""
    ref_tl = timelines.get(reference_cam)
    if ref_tl is None or ref_tl.creation0 is None:
        return []

    deltas = {}
    for cam, tl in timelines.items():
        if cam == reference_cam or tl is None or tl.creation0 is None:
            continue
        hint = (tl.creation0 - ref_tl.creation0).total_seconds()
        solved = offsets.get(cam)
        if solved is None:
            continue
        disagreement = solved - hint
        if abs(disagreement) > prior_tolerance:
            deltas[cam] = (hint, solved, disagreement)

    if not deltas:
        return []

    values = [d[2] for d in deltas.values()]
    if len(deltas) >= 3 and (max(values) - min(values)) < 1.0:
        avg = sum(values) / len(values)
        return [
            f"every camera's clock disagrees with camera {reference_cam}'s by about "
            f"{avg:+.1f}s -- looks like the rig's clocks weren't set together, not a sync problem"
        ]

    return [
        f"camera {cam}: clock appears unsynced (tag implies {hint:+.1f}s, audio found {solved:+.1f}s)"
        for cam, (hint, solved, _) in deltas.items()
    ]


# ------------------------------------------------------------ sync.yaml --

def write_sync_yaml(path: Path, *, timelines: dict[int, Timeline], offsets: dict[int, float],
                     measurements: list[Measurement], solve: SolveResult,
                     prior_agreement_notes: list[str], reference_cam: int = 1,
                     method_summary: str = "creation-time-prior + audio-refine") -> None:
    cameras = {}
    for cam, tl in timelines.items():
        if tl is None:
            continue
        hint = None
        if cam != reference_cam and tl.creation0 is not None:
            ref = timelines.get(reference_cam)
            if ref is not None and ref.creation0 is not None:
                hint = round((tl.creation0 - ref.creation0).total_seconds(), 3)
        cameras[cam] = {
            "offset": round(offsets.get(cam, 0.0), 3),
            "duration": round(tl.total_duration, 1),
            "confidence": "ref" if cam == reference_cam else None,
            "prior": hint if cam != reference_cam else 0,
        }
    # Each camera's confidence is the psr of the link that actually anchors
    # it back to reference_cam -- not just any measurement that happens to
    # touch it. Camera 2 anchors via its intra-tripod link to camera 1;
    # cameras 3 and 4 (the other tripod) anchor via their cross-tripod link,
    # since that's what actually ties them back to camera 1's tripod.
    by_pair = {m.pair: m for m in measurements}
    anchor_link = {2: (1, 2), 3: (1, 3), 4: (2, 4)}
    for cam, pair in anchor_link.items():
        if cam == reference_cam or cam not in cameras:
            continue
        m = by_pair.get(pair)
        if m is not None:
            cameras[cam]["confidence"] = round(m.psr, 1)

    data = {
        "version": 1,
        "method": method_summary,
        "cameras": cameras,
        "measurements": [
            {
                "pair": list(m.pair),
                "lag": round(m.lag, 3),
                "sigma": round(m.sigma, 4),
                "psr": round(m.psr, 1),
                "windows": f"{m.windows_used}/{m.windows_total}",
                "kind": m.kind,
                "method": m.method,
            }
            for m in measurements
        ],
        "residuals": {
            "max": round(solve.max_residual, 3),
            "ok": not solve.inconsistent_links,
        },
        "notes": prior_agreement_notes,
    }
    with open(path, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False)


def read_sync_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def offsets_from_sync_yaml(data: dict) -> dict[int, float]:
    return {int(cam): float(info["offset"]) for cam, info in data["cameras"].items()}


# ------------------------------------------------------------ top level --

@dataclass
class SyncGameResult:
    timelines: dict[int, Timeline | None]
    measurements: list[Measurement]
    solve: SolveResult
    offsets: dict[int, float]
    prior_notes: list[str]
    dropped_cameras: list[int]


def sync_game(tool: Tool, cameras: dict[int, CameraInfo], *, reference_cam: int = 1,
              n_windows: int = 24, window_len: float = 20.0, prior_window: float = 5.0,
              min_confidence: float = 6.0, max_offset: float = 1800.0,
              no_time_prior: bool = False, clap_at: float | None = None,
              offset_overrides: dict[int, float] | None = None,
              nudge_overrides: dict[int, float] | None = None,
              prior_tolerance: float = 5.0, skip_head: float = 60.0) -> SyncGameResult:
    """The full Sec 6.6 degradation ladder: (1) no audio -> drop from sync,
    warn; (2) low confidence -> blind fallback (inside measure_link), then
    fail just that link; (3) a failed link that leaves the graph connected
    solves anyway; (4) a disconnected graph is a hard error naming the
    unanchored camera, UNLESS an --offset override anchors it directly;
    (5) an offset beyond --max-offset is a hard error."""
    timelines = {cam: build_timeline(info) for cam, info in cameras.items()}

    dropped = [cam for cam, tl in timelines.items() if tl is not None and not tl.has_audio]
    for cam in dropped:
        warnings.warn(f"camera{cam}: no audio stream, dropped from sync (using hint only)")

    overridden = set((offset_overrides or {}).keys())

    measurements = []
    for pair in FIXED_LINKS:
        i, j = pair
        tlA, tlB = timelines.get(i), timelines.get(j)
        if tlA is None or tlB is None or not tlA.has_audio or not tlB.has_audio:
            continue
        if clap_at is not None and link_kind(pair) == "cross-tripod":
            try:
                m = measure_clap(tool, timelines, pair, clap_at, reference_cam=reference_cam)
            except ValueError as e:
                warnings.warn(f"clap measurement failed for {pair}: {e}")
                m = measure_link(tool, tlA, tlB, pair, n_windows=n_windows,
                                  window_len=window_len, prior_window=prior_window,
                                  min_confidence=min_confidence, max_offset=max_offset,
                                  force_blind=no_time_prior, skip_head=skip_head)
        else:
            m = measure_link(tool, tlA, tlB, pair, n_windows=n_windows, window_len=window_len,
                              prior_window=prior_window, min_confidence=min_confidence,
                              max_offset=max_offset, force_blind=no_time_prior,
                              skip_head=skip_head)
        if m is None:
            warnings.warn(f"link {pair}: no usable correlation, dropping this link")
            continue
        if abs(m.lag) > max_offset:
            raise ValueError(
                f"link {pair}: measured offset {m.lag:.1f}s exceeds --max-offset {max_offset}s"
            )
        measurements.append(m)

    def needs_solving(cam: int) -> bool:
        if cam == reference_cam:
            return True
        if cam in overridden:
            return False
        tl = timelines.get(cam)
        if tl is None:
            return False  # genuinely absent camera: no footage to position
        if not tl.has_audio and tl.creation0 is not None:
            return False  # soft fallback below: hint alone is enough
        # has audio (needs the normal graph solve), or has neither audio nor
        # a hint (needs to hit the disconnected-graph error, not be silently
        # skipped -- it has footage that genuinely needs a position)
        return True

    solve_cams = tuple(c for c in cameras if needs_solving(c))
    solve_measurements = [
        m for m in measurements if m.pair[0] in solve_cams and m.pair[1] in solve_cams
    ]
    solve = solve_topology(solve_measurements, cams=solve_cams, reference_cam=reference_cam)
    offsets = apply_overrides(solve.offsets, offset_overrides, nudge_overrides)

    # Cameras excluded from the solve because they're audio-less (dropped)
    # fall back to the creation_time hint alone (Sec 6.6 step 1), if any.
    ref_tl = timelines.get(reference_cam)
    for cam in cameras:
        if cam in offsets or cam not in dropped:
            continue
        tl = timelines.get(cam)
        if tl is not None and tl.creation0 is not None and ref_tl is not None and ref_tl.creation0 is not None:
            offsets[cam] = (tl.creation0 - ref_tl.creation0).total_seconds()
            warnings.warn(f"camera{cam}: no audio, offset taken from creation_time hint only")

    prior_notes = check_prior_agreement(timelines, offsets, reference_cam=reference_cam,
                                         prior_tolerance=prior_tolerance)
    return SyncGameResult(timelines=timelines, measurements=measurements, solve=solve,
                           offsets=offsets, prior_notes=prior_notes, dropped_cameras=dropped)
