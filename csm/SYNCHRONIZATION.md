# Synchronization: how `csm sync` works

Four cameras, two tripods, no shared trigger. Getting all four onto one
timeline used to mean scrubbing through QuickTime by hand looking for a
frame where a ball hits the ground in two cameras at once, accurate to
maybe ±30ms if you were careful. `csm sync` replaces that with audio
correlation: every camera also recorded a mono soundtrack, and any two
microphones that heard the same real-world moment — a whistle, a shout, the
general roar of a game — have that moment encoded in their audio, just
shifted by however far apart the cameras started recording. Find that
shift and you've found the sync offset, to a precision manual scrubbing
can't touch.

## GCC-PHAT, in plain terms

Given two audio clips of the *same* sound recorded from two mics, standard
cross-correlation would slide one clip past the other and look for the lag
that maximizes agreement. That works, but real recordings aren't equally
loud or equally clean at every frequency — a lag where both clips happen to
have a loud bass rumble at the same relative offset can outscore the lag
where the actual shared sound aligns.

**GCC-PHAT** ("Generalized Cross-Correlation, PHAse Transform") fixes this
by throwing away *how loud* each frequency is and keeping only *what phase
it's at*. Concretely, for two audio windows `a` and `b`:

1. FFT both.
2. Take `R = fft(a) * conj(fft(b))` — a complex number per frequency,
   whose phase encodes the relative timing at that frequency.
3. **Whiten**: divide `R` by its own magnitude at every frequency, so every
   frequency contributes with equal weight regardless of how much energy
   was actually there.
4. Inverse-FFT the whitened `R`. The result is a "correlation" as a
   function of lag; its peak is the estimated delay.
5. Refine the peak to sub-sample precision with a 3-point parabolic fit
   around the discrete maximum, and 4x zero-padding before the inverse FFT
   for extra interpolation headroom.

This is `sync.gcc_phat()`. On synthetic signals with a precisely known
delay it recovers the delay to a small fraction of a sample (confirmed by
this codebase's own test suite: fractional-sample delays resolve to better
than 0.2 samples). On real archived game audio, checked against six-camera
ground truth recovered from the manual sync values used at the time (see
below), it matches to within tens of milliseconds — often better than the
manual process it replaces, and likely close to the *manual* ground truth's
own precision limit rather than GCC-PHAT's.

### The catch: PHAT wants broadband, non-repeating content

Whitening is a double-edged sword. It's what makes PHAT robust to two mics
recording at different volumes or with different frequency responses — but
it also means PHAT can't tell "genuinely correlated" from "coincidentally
similar phase structure" at a frequency with no real signal there at all.
Two consequences that mattered while building and testing this:

- **A signal with a narrow or discrete spectrum is a bad target.** A pure
  tone, or a sum of a few dozen fixed tones, has near-zero energy between
  its spectral lines. PHAT still whitens those near-empty bins to full
  weight, so tiny numerical noise there can dominate the result. Real game
  audio (crowd noise, wind, footsteps, a whistle's broadband transient) is
  never like this, so it isn't a problem in production — it only bit this
  project's own *test fixtures*, which originally used a synthetic tone.
- **A perfectly periodic signal is ambiguous over any search wider than its
  period.** If the same waveform repeats every 1.7 seconds, PHAT correlates
  just as well at a 1.7s-wrong lag as at the true one — there's no
  broadband "fingerprint" to prefer one repetition over another. This
  doesn't happen with real ambient audio either. It's mentioned here
  because it's a genuine trap for anyone tempted to reach for a clean
  synthetic test tone.

The practical upshot for this codebase: `tests/conftest.py`'s synthetic
fixtures use a shared slice of real broadband noise (generated once,
sliced per camera by its offset) rather than a parametric tone, specifically
because both of the above bit the original attempt.

### Confidence: peak-to-sidelobe ratio (psr)

After finding the peak, `gcc_phat()` also reports how *trustworthy* it is:
the peak's height divided by the median absolute correlation value outside
a small guard band around the peak (5ms). A sharp, isolated peak well above
the surrounding noise floor means the two clips really do share content at
that lag; a peak barely above the noise floor means the "match" could be
chance.

**Calibration note:** the thresholds here (`MARGINAL_PSR = 10`,
`CONFIDENT_PSR = 20`) are higher than a first reading of the numbers might
suggest, and higher than this project's own design notes originally
proposed (4/8). That's not arbitrary — it's measured. Correlating two
*completely unrelated* white-noise clips at a realistic search width (a few
seconds, 4x-interpolated) reliably produces a psr in the 6-8 range, purely
from extreme-value statistics: picking the tallest of many noise samples
finds something taller than the median even when nothing real is there.
Real correlated links in this project's archived-game validation scored
consistently in the tens to low hundreds, so raising the bar to 10/20 costs
nothing on genuine signal while actually rejecting the noise-floor false
positives the lower thresholds let through.

## `creation_time` narrows the search; audio decides the answer

Camera clocks are set by hand from a phone app. That gets them within
roughly a second of each other — useful for pointing the search in the
right direction, useless as the actual answer (a 1-second error is 30
frames at 30fps). So the flow is:

1. **Hint.** If both cameras have a `creation_time` tag, their difference
   estimates the offset to within about a second, narrowing GCC-PHAT's
   search to a small window (`--prior-window`, default 5s) instead of
   scanning the entire recording.
2. **Audio measures.** GCC-PHAT runs inside that narrow window and produces
   the actual answer.
3. **Blind fallback, automatically.** If the hint is missing, or the
   hint-guided search comes up empty or unconvincing, that *specific link*
   silently escalates to a blind search over the full `--max-offset` range
   (default 1800s) instead of failing outright. This is the "forgotten
   camera" case — one camera's clock was never synced, so its hint is
   wrong by minutes, but the audio still finds the true offset once it
   searches widely enough. `sync.yaml` records `method: blind` for any link
   that needed this, and the final report names the camera whose clock
   disagreed, purely as a diagnostic ("clock appears unsynced") — never a
   failure.

**Invariant: the final number always comes from audio (or an explicit
`--offset`). `creation_time` only ever decides where to look.**

### Blind search has to work regardless of which camera started first

A subtlety worth recording because it was a real bug during development:
a naive blind search probes a short clip near camera A's own start and
correlates it against camera B's whole recording. That silently fails
whenever B actually started *after* A by more than the probe's own head
start — A's probed moment simply doesn't exist yet in B's file, no matter
how wide the search range is. `_blind_measurement` instead probes A at
`skip_head + max_offset` into its recording (far enough in that even a
worst-case B-started-late scenario still overlaps) and sizes B's comparison
window generously enough to also cover B having started *before* A. It's
direction-agnostic by construction rather than by luck.

A second, smaller trap in the same function: extracting audio starting at
literal time zero of an AAC-encoded file can land on the codec's own
priming/encoder delay (a fixed ~1024-sample artifact), producing a small
but real and consistent bias. The fix is to never anchor an extraction
exactly at a file's start — `_blind_measurement` uses a small margin
instead.

## Exploiting the tripod geometry

Cameras 1+2 share a tripod; so do 3+4. That geometry is exploited two ways.

**Within a tripod (intra-tripod, 1↔2 and 3↔4):** the mics are inches apart,
so the difference in arrival time from *any* sound source is under a
millisecond regardless of where on the field it came from. These links are
essentially exact — psr in the hundreds is typical.

**Across tripods (cross-tripod, 1↔3 and 2↔4):** the tripods are 60-70m
apart, so a sound's arrival-time difference between them depends on where
on the field it came from — up to about ±200ms of bias, worst at the
sidelines, near-zero for a sound at midfield. A single cross-tripod window
is therefore noisier (σ ≈ 120ms). The fix isn't a better single
measurement; it's averaging many. `--sync-windows` (default 24) windows are
spread across the mutual recording overlap (skipping the first minute to
avoid setup noise), each with its own GCC-PHAT measurement and MAD-based
outlier rejection, then fit with Theil-Sen (`scipy.stats.theilslopes`) —
robust to the occasional bad window, and incidentally absorbing the
cameras' small clock drift (measured under 2 frames over 90 minutes) as a
side effect, with no dedicated drift-correction code needed. Averaging
over enough windows with reasonably symmetric field-position bias drives
the standard error down to roughly `σ/√K` — about 25ms at K=24, under one
video frame.

**Both cross-links get measured, not just one.** 1↔3 and 2↔4 are
independent estimates of the same underlying tripod-to-tripod delay. That
redundancy is what makes the topology *over-determined*: four measured
links, three unknowns (camera 1 is the fixed reference at offset 0), solved
by weighted least squares. The leftover degree of freedom shows up as a
residual on every link — if two different paths through the graph (say,
1→2→4 and 1→3→4) disagree by more than 3σ, that's reported as an explicit
inconsistency rather than silently averaged away. This is the difference
between "auto-sync worked" and "auto-sync silently handed you a garbage
number," and it costs nothing since the correlations already exist.

## `sync.yaml`: written once, editable, never silently redone

`csm sync --write` produces `GAMEDIR/sync.yaml` recording every camera's
final offset *and* every individual measurement (lag, confidence, method,
which windows were used). It's meant to be read by a human when something
looks wrong, and edited by hand if needed — nothing downstream re-runs
detection automatically. Manual control layers in a fixed order:
`sync.yaml` → `--offset N=SEC` (replaces a camera's offset outright) →
`--nudge N=±SEC` (adds a small correction on top).

## A clap is worth it, but only from midfield

Sound takes about 200ms to cross the ~60-70m between tripods. A clap next
to one tripod arrives at the other tripod biased by that entire
propagation delay — confident, precise, and *wrong*. A clap at the center
of the field is equidistant from both tripods, so the bias cancels and the
measurement becomes a direct, low-noise cross-tripod reading (a few
milliseconds) instead of a statistical average. `--clap-at MM:SS` (camera
1's clock) uses it for that link, tagged `kind: clap` in `sync.yaml` so a
bad clap is diagnosable rather than mysterious. It has to happen *after*
all four cameras are already rolling, not during setup — the whole point
is that it's audible on every camera.

## Validated against real footage

Before any of the above got built, the core claim — that GCC-PHAT actually
recovers real sync offsets — was checked directly against two archived
games with independently known ground truth (recovered from the `-ss` trim
values in the `runme.sh` each game was actually rendered with). Both
validated to within tens of milliseconds on every link, including the
701-second cross-tripod gap in one of the games (the tripods were ~700s
apart in start time because of the walk to set up the second one). The
same check, run through the actual `csm sync` implementation rather than a
standalone script, reproduced the result. See `../docs/DESIGN.md` Sec 6.0 and
Sec 2.5 for the numbers.
