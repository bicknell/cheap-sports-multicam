# csm reference

Complete reference for `csm`, the sync-and-render tool in this repository. If
you just want to get a video out the door, see the root [README.md](../README.md)'s
Software section for a quickstart; this document covers everything, including
the parts you'll only need once (initial setup, tuning quality for your own
footage, troubleshooting a bad sync).

This document describes the software only — it makes no assumptions about
camera brand, resolution, or frame rate. `docs/cameras.md` and `docs/soccer.md`
are separate, hardware/sport-specific notes.

`docs/DESIGN.md` is the design document this implementation was built from —
useful if you want the reasoning behind a decision, not just what the decision
was.

---

## 1. Installing

Needs Python 3.10+ and `ffmpeg`/`ffprobe` **7.0 or newer** on `PATH`.

```bash
brew install ffmpeg          # macOS
# or
apt install ffmpeg           # Debian/Ubuntu — 25.04+ ships 7.x
```

Check your version with `ffmpeg -version`. `csm` will refuse to run against
anything older, with a message telling you what's missing.

`libx265` is required (it's the default encoder). `libvmaf`, `libsvtav1`, and
`hevc_videotoolbox` are used when present and quietly skipped when not —
nothing depends on them being there.

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

This installs `csm` on `PATH` inside the virtualenv (numpy/scipy/PyYAML ship
prebuilt wheels for macOS and Linux, so nothing compiles). Run `pytest -m "not
slow"` afterward if you want to confirm the install works — it should finish
in well under a minute and doesn't touch any real footage.

---

## 2. Expected directory layout

Every `csm` command takes a single `GAMEDIR` argument — a directory shaped
like this:

```
GAMEDIR/
  camera1/          NORM_00001.MP4, NORM_00002.MP4, ...  (or .mov/.mts/.m4v)
  camera2/
  camera3/
  camera4/
  metadata.yaml      optional — see Sec 8
  sync.yaml           written by `csm sync --write`, read by `csm render`/`csm preview`
  .csm/               working directory csm creates itself (dry-run scripts, preview output)
```

- Cameras are numbered 1–4 and tiled `camera1 | camera2` on top, `camera3 |
  camera4` on the bottom. This project assumes a 2-tripod, 4-camera rig
  (cameras 1+2 on one tripod, 3+4 on the other) — see `docs/soccer.md` for the
  reasoning, though nothing in the software itself requires that specific
  arrangement to be soccer or even sports.
- A camera directory can be **missing entirely**, or contain **fewer or
  shorter segments** than the others. Both are first-class, handled cases —
  not errors. A missing camera renders as a black quadrant; a short camera's
  quadrant goes black once its footage runs out, without truncating the rest
  of the game.
- Segment files within a camera directory are read in filename order — this
  matters if your camera doesn't zero-pad segment numbers consistently.
  `csm probe` cross-checks filename order against each segment's
  `creation_time` and warns if they disagree.
- `metadata.yaml`, if present, is a flat string-to-string mapping written into
  the output file's container metadata (title, comment, etc. — any key valid
  in [ffmpeg's metadata
  API](https://ffmpeg.org/doxygen/7.0/group__metadata__api.html)).

---

## 3. The commands

Four subcommands, meant to be run in this order: `probe` → `sync` → `preview`
(optional but recommended) → `render`.

### 3.1 `csm probe GAMEDIR`

Inspects the footage without touching sync or rendering. Run this first on
every game — it's the fastest way to catch a camera that didn't record, a
memory card with the wrong settings, or a segment that doesn't match the
others.

```bash
csm probe GAMEDIR
csm probe GAMEDIR --audio       # also print per-camera audio levels
csm probe GAMEDIR --json        # machine-readable output
csm probe GAMEDIR --frame       # one still PNG per camera, for reading off pixel coordinates
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--json` | off | Emit a JSON object instead of text; exit code is still 1 on any error-severity issue |
| `--audio` | off | Run `volumedetect` on the first 20s of each audio-bearing camera and print mean/peak level |
| `--audio-cams` | `2,4` | Which camera numbers to check for audio presence/level |
| `--frame` | off | Write one still frame per camera to `GAMEDIR/.csm/frames/cameraN.png`. Open these in any image viewer and read pixel coordinates off the cursor — this is how you find the value for `--field-cutoff` (Sec 5.5) |
| `--frame-at` | `45.0` | Seconds into the first segment to grab `--frame` from. The default is past typical camera setup time — the first 10-15s is often still you raising/aiming the camera, which makes a bad reference frame |
| `--ffmpeg`, `--ffprobe` | auto-detected on `PATH` | Explicit binary paths |

Prints, per camera: segment count, total duration, resolution, fps, codec,
audio presence, and `creation_time` if the file has one. Below that, any
issues found — always look at these, they're not just noise:

- segments of the same camera disagreeing on size/fps/codec (names the
  offending file)
- fps outside tolerance of the assumed timeline fps
- a camera listed in `--audio-cams` that actually has no audio stream
- a probable dropped-frame indicator (`nb_frames / fps` vs. container
  duration mismatch)
- audio/video duration drift within a segment
- segment filename order that disagrees with `creation_time` order
- insufficient free disk space for the estimated output

### 3.2 `csm sync GAMEDIR --write`

Detects each camera's time offset relative to camera 1, using audio
cross-correlation (GCC-PHAT) refined by each camera's recording-start
timestamp as a search hint. See `SYNCHRONIZATION.md` for how this actually
works, and Sec 6 below for what to do when it doesn't.

```bash
csm sync GAMEDIR --write
```

Prints each camera's offset and, with `--write`, saves everything to
`GAMEDIR/sync.yaml` — the file `csm render`/`csm preview` actually read.
**Nothing downstream re-runs detection automatically**; `sync.yaml` is meant
to be inspected, and hand-edited if needed.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--write` | off | Save results to `--sync-file` (default `GAMEDIR/sync.yaml`) |
| `--sync-file` | `GAMEDIR/sync.yaml` | Explicit path |
| `--clap-at MM:SS` | none | Anchor the cross-tripod measurement on a deliberate clap at this timestamp (camera 1's clock) — see Sec 6.3 |
| `--offset N=SEC` | none | Override camera N's offset outright, e.g. `--offset 3=182.4` (repeatable) |
| `--offset-frames N=F` | none | Same, in frames at `--fps` instead of seconds (repeatable) |
| `--nudge N=+-SEC` | none | Add a small correction on top of whatever offset is in effect (repeatable) |
| `--no-time-prior` | off | Ignore `creation_time` tags entirely and blind-search every link (slower, useful for testing or a camera whose clock is known-bad) |
| `--prior-window` | `5.0` | Seconds either side of the `creation_time`-hinted offset to search |
| `--prior-tolerance` | `5.0` | How far a solved offset can disagree with its `creation_time` hint before it's reported as "clock appears unsynced" |
| `--sync-windows` | `24` | Number of audio windows sampled per camera pair for the cross-tripod (1↔3, 2↔4) measurement |
| `--sync-window-len` | `20.0` | Length in seconds of each sampled window |
| `--max-offset` | `1800.0` | Widest offset a blind search will consider (seconds) |
| `--min-confidence` | `6.0` | Peak-to-sidelobe ratio below which a correlation is rejected |
| `--fps` | `30.0` | Only used to interpret `--offset-frames` |

### 3.3 `csm render GAMEDIR`

Produces the final tiled video. Requires `sync.yaml` to already exist (run
`csm sync --write` first — `csm render` errors with that exact instruction if
it doesn't).

```bash
csm render GAMEDIR                              # everything at its default
csm render GAMEDIR --dry-run                     # print the ffmpeg command instead of running it
csm render GAMEDIR --out-size native --duration 120
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--sync-file` | `GAMEDIR/sync.yaml` | Explicit path |
| `--offset`, `--offset-frames`, `--nudge` | — | Same as `csm sync`, layered **on top of** `sync.yaml` without needing to re-run sync |
| `--align-start` | `latest` | `latest`: timeline starts once **every** present camera is recording (no camera contributes a black quadrant at the very start unless it's genuinely missing). `earliest`: starts as soon as the *first* camera does, so late-starting cameras begin black |
| `--align-end` | `longest` | `longest`: timeline runs until the last camera stops (a camera that dies early goes black rather than truncating the game). `shortest`: ends when the first camera stops |
| `--start T` | none | Explicit start time (seconds on the aligned timeline), overrides `--align-start` |
| `--end T` | none | Explicit end time, overrides `--align-end` |
| `--duration D` | none | Explicit duration from `--start` (or the computed start), overrides `--align-end` |
| `--fps` | `30.0` | Output frame rate |
| `--audio-cams` | `2,4` | Exactly two camera numbers to mix down to stereo (left/right). Run `csm probe --audio` first if you're not sure which mics are usable |
| `--out-size` | `3840x2160` | `WxH`, or `native` (2× whatever the present cameras actually recorded — use this if your cameras aren't 1080p) |
| `--field-cutoff N=PIXELS` | none | Repeatable. Pre-blurs everything above this height (native resolution, measured from the bottom) for camera N, so the encoder stops spending bits on sky/stands. See Sec 5.5 |
| `--profile` | `final-h265` | Encoder profile — run `csm render GAMEDIR --profile bogus` to see the current list |
| `--crf` | `23` | Only meaningful with `--profile final-h265`. Overrides `GAMEDIR/csm.yaml`'s persisted value if it has one — see Sec 5.6 |
| `--preset` | `fast` | Same as `--crf` — `GAMEDIR/csm.yaml` provides the default, this overrides it for one run |
| `--output` | `GAMEDIR/final_video.mp4` | Output file path |
| `--work-dir` | a temp directory | Where concat list files are materialized during the render |
| `--dry-run` | off | Print the exact `ffmpeg` command (properly quoted, pasteable, provably the same command that would have run) instead of running it |

The **generate → inspect → run** habit is worth keeping even though `csm`
builds the command for you: `csm render GAMEDIR --dry-run | tee
GAMEDIR/.csm/render.sh` gives you an audit trail and a chance to hand-edit
before committing hours of encode time.

### 3.4 `csm preview GAMEDIR`

Renders a short window through the *exact same* pipeline as a full render —
same alignment, same filtergraph, same encoder machinery — so a preview that
looks right guarantees the full render is built the same way. Takes all of
`csm render`'s sync/timeline flags (`--offset`, `--align-start`, `--out-size`,
etc.) plus:

```bash
csm preview GAMEDIR                                          # 60s at the middle of the game
csm preview GAMEDIR --preview-at 0% --preview-seconds 30      # 30s right after all cameras go live
csm preview GAMEDIR --sweep crf=21,23,25,26                   # quality/speed sweep — see Sec 4
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--field-cutoff N=PIXELS` | none | Same as `csm render` (Sec 3.3, Sec 5.5) — worth checking in a preview before committing to a full render |
| `--preview-seconds` | `60.0` | Window length |
| `--preview-at` | `middle` | Repeatable. `middle`, `MM:SS`, or a `PCT%` — each an offset into the *rendered* timeline (0 = the moment the timeline starts, not the start of any individual camera's footage). An additional automatic window is always rendered at the first moment all four cameras are simultaneously live (skipped if it's the same window you already asked for), since that's where a sync error is most visible |
| `--preview-encoder` | `auto` | `auto` picks `hevc_videotoolbox` (fast, macOS-only) if available, else `libx265 -preset veryfast`. Pass an explicit profile name to force one |
| `--crf`, `--preset` | `23`, `fast` | **Only with `--sweep`**: the baseline value for any dimension you don't explicitly sweep over (e.g. `--sweep preset=fast,medium` alone still needs a CRF to render at — this is it). `GAMEDIR/csm.yaml` provides the default; these override it. Has no effect on plain (non-`--sweep`) preview, which always uses the fast preview encoder regardless |
| `--upload-mbps` | `40` | **Your actual upload speed**, in Mbps — used only by `--sweep`'s extrapolated upload-time column (Sec 4). Everyone's connection is different and changes over time, so don't trust the default; pass your own |
| `--output` | `GAMEDIR/.csm/preview/` | **A directory**, not a file — one `preview_<label>.mp4` per window |
| `--dry-run` | off | Same as `csm render --dry-run` |

**A note on window position and speed:** a window's head-trim cost — how much
of a camera's footage must be decoded before reaching the window — grows with
how far into the game the window sits, plus each camera's own sync offset.
`--preview-at 0%` (or the automatic all-cameras-live window) is cheapest;
`middle` on a long game can mean a few minutes of decode before the actual
59s of encode even starts. This is bounded — at most one recording segment's
worth of decode, never the whole game — but it isn't free. If a preview feels
slow, try a window closer to the start.

---

## 4. Tuning quality: `--sweep`

Don't guess a CRF value — measure it against **your own footage**, since the
right operating point depends on your cameras' bitrate, motion content, and
how much time you're willing to spend encoding. `csm preview --sweep` renders
the same short window once per setting, plus one lossless reference, and
scores each variant against the reference:

```bash
# Compare CRF values at the default encoder/preset:
csm sync GAMEDIR --write                    # sweep needs sync.yaml, same as render
csm preview GAMEDIR --sweep crf=21,23,25,26

# Compare encoders and presets (cartesian product — this runs 4 variants):
csm preview GAMEDIR --sweep encoder=libx265,libsvtav1 --sweep preset=fast,medium

# Use your actual upload speed instead of the 40 Mbps default:
csm preview GAMEDIR --sweep crf=21,23,25,26 --upload-mbps 25

# Tune how much the encoder spends on "complex-looking" detail (turf
# texture, specular glare) vs staying conservative — libx265 only:
csm preview GAMEDIR --sweep psy-rd=0.5,1.0,2.0 --sweep aq-strength=0.6,1.0
```

Each `--sweep KEY=v1,v2,...` is repeatable; multiple `--sweep` flags in one
invocation cross-multiply (2 encoders × 2 presets = 4 renders). Recognized
keys: `crf`, `preset`, `encoder` (`libx265` or `libsvtav1`), `psy-rd`,
`aq-strength` (`libx265` only — SVT-AV1 has its own different tuning knobs,
not exposed here).

**`psy-rd`/`aq-strength`** are worth sweeping if particular content (shiny
turf, glare, distant crowd texture) is eating disproportionate bitrate
relative to what actually matters in frame. Both default to `final-h265`'s
production values (`psy-rd=2.0`, `aq-strength=1.0`) when not swept. Turning
either down trades a little sharpness on genuinely detailed regions
(players, jersey numbers) for fewer bits spent on background texture —
whether that's a good trade is a visual judgment call the sweep's VMAF/xpsnr
score won't fully capture, since those metrics don't know which region of
the frame matters more to a viewer. Watch the actual output, not just the
score, before deciding. See also `--field-cutoff` (Sec 5.5) for a more
targeted fix when the problem region is consistently sky/stands rather than
texture spread across the whole frame.

**Pass `--upload-mbps` with your actual connection speed.** The default (40)
is just a starting point from this project's own original connection — it is
*not* a safe assumption for anyone else's, and home upload speeds change over
time even for the same person. The printed table always states which value
was used, so results are self-documenting even if you paste them somewhere
without the command that generated them.

Output is a table:

```
upload(h) assumes 40 Mbps -- override with --upload-mbps
variant                         wall(s)  size(MB)  full(GB)  upload(h)    score  metric
crf-21                            612.4     201.3      9.82       1.96    89.14    vmaf
crf-23                            589.7     140.6      6.86       1.37    82.05    vmaf
crf-25                            571.2      98.1      4.79       0.96    74.88    vmaf
crf-26                            558.9      83.4      4.07       0.81    70.12    vmaf
```

- **wall(s)** — actual encode time for the preview window.
- **size(MB)** — actual output file size for the preview window.
- **full(GB)** / **upload(h)** — extrapolated from the window's size/duration
  ratio to your full game (using the actual timeline duration from
  `sync.yaml`, not a guess), and to upload time at `--upload-mbps` (default
  40, override with your own).
- **score** / **metric** — quality against a lossless reference of the same
  window, using whichever of `libvmaf` (preferred), `xpsnr`, or `ssim` your
  ffmpeg build actually has — always reported explicitly so you never compare
  scores computed by two different metrics.

Read the tradeoff, not just the top score: a CRF one step lower is rarely
worth it if it doesn't change what you can see on the eventual YouTube upload
but does change your encode time by 10%. Once you've picked a value, either
pass it every time with `--x265-param`-style overrides (not yet built — for
now, edit `csm/encoders.py`'s `FINAL_H265` profile directly and record your
choice and its measured numbers here or in the README, so next season starts
from evidence instead of repeating the sweep).

---

## 5. Rendering behavior in detail

### 5.1 Alignment

The "timeline" is the common time axis every camera gets warped onto.
`--align-start`/`--align-end` (Sec 3.3) pick it from the cameras actually
present; `--start`/`--end`/`--duration` override that explicitly. A camera
that starts after the timeline begins is padded black at the front; one that
starts before is trimmed; one that ends before the timeline does is padded
black at the end rather than truncating everyone else.

### 5.2 Resolution

Tile size is always half of `--out-size` in each dimension. A camera whose
native resolution doesn't match the tile size is scaled (Lanczos,
aspect-ratio-preserving, letterboxed/pillarboxed as needed — never stretched)
and range-converted from full-range to broadcast-legal limited-range BT.709,
which is what YouTube and most players actually expect. If every camera
already matches the tile size exactly, no scale filter is emitted at all
(check with `--dry-run` if you want to confirm).

### 5.3 Encoder profiles

`final-h265` (the default) is `libx265`, preset `fast`, CRF 23, 10-bit
`yuv420p10le`, tuned `aq-mode`/`psy-rd`/`rc-lookahead` for per-pixel quality,
explicit BT.709 color tagging (both via generic flags and `-x265-params`,
because on some ffmpeg builds the generic flags alone don't reliably reach
the container), `+faststart`, AAC 192k stereo audio.

**These numbers came from a real `--sweep` against actual game footage**
(2026-05-03, plus the worst-case-turf 2026-04-19 game identified in Sec 4),
not a guess:

| variant | wall(s), 24s window | extrapolated full game | VMAF |
| --- | --- | --- | --- |
| crf-21, medium | 315.9 | 45.6 GB / 2.53h upload | 79.50 |
| crf-23, medium | 293.6 | 31.7 GB / 1.76h upload | 78.04 |
| **crf-23, fast (current default)** | **211.0** | **30.4 GB / 1.69h upload** | **77.55** |
| crf-26, medium | 257.8 | 17.3 GB / 0.96h upload | 75.33 |

`fast` costs about half a VMAF point against `medium` at the same CRF for a
~28% encode-time saving — in the same "diminishing returns past this point"
territory the CRF comparison itself shows. `libsvtav1` was also compared and
intentionally not adopted here: at matched nominal CRF it produced files
over 2× the size of `libx265` (AV1's CRF scale isn't numerically comparable
to HEVC's, so that specific comparison wasn't apples-to-apples — a real
verdict would need AV1 tuned to a matching file size or VMAF first), and
DESIGN.md already has a separate, standing reason to defer AV1 to Phase 5
regardless (no local AV1 playback on the Intel/T2 Mac this was built on).

**`aq-strength` moved from `1.0` to `0.3`**, sweeping the worst-case-turf
game identified below (2026-04-19 — see Sec 4's blue-turf note):

| aq-strength | size (MB), 24s window | VMAF |
| --- | --- | --- |
| 1.0 (old default) | 203.3 | 77.48 |
| 0.8 | 194.8 | 77.40 |
| 0.5 | 186.0 | 77.32 |
| **0.3 (current default)** | **177.1** | **77.22** |

Roughly linear, real, favorable tradeoff the whole way down — ~13% smaller for
a 0.26 VMAF point cost, no sign of a cliff by 0.3. `psy-rd` was swept over the
same range on the same content and found essentially flat (200.8→203.3 MB
across 0.5→2.0, VMAF within 0.05) — not worth touching. Neither is currently
exposed as its own flag; both are fixed inside `final-h265` beyond what
`--sweep psy-rd=...`/`aq-strength=...` can already test. Worth sweeping
further below 0.3 before assuming it's the true optimum — that's as far as
this pass went, not necessarily where the tradeoff stops paying off.

**Treat this as provisional, not settled.** Re-run the sweep (Sec 4) after
gaining real experience with newer features like `--field-cutoff`, and
again on any new hardware (a DigitalOcean droplet, an Apple Silicon Mac) —
the right operating point depends on the machine doing the encoding as much
as the footage.

### 5.4 `--dry-run`

Every invocation of `render`/`preview` accepts `--dry-run`, which prints the
exact `ffmpeg` command instead of running it — properly quoted with
`shlex.join` (never hand-built string interpolation, which is what let a
single quote or space in a filename silently corrupt the old script). The
printed command is provably the same one that would execute, down to the
concat list files it references, which get written to `GAMEDIR/.csm/` so the
script stays pasteable and re-runnable later.

### 5.5 `--field-cutoff`: stop paying for sky and stands

Encoders that adapt bit allocation to local detail (`aq-mode`, `psy-rd` —
both on by default in `final-h265`) can't tell "this is complex because it's
a player's face" from "this is complex because it's shiny turf glinting in
the sun" or a distant parking lot. Both get bits; only one is worth them.
Since a tripod-mounted camera frames the same shot for the whole game, the
line between "field" (worth the bits) and "sky/background" (not) sits at a
constant height in the frame the entire recording.

```bash
csm probe GAMEDIR --frame              # writes GAMEDIR/.csm/frames/cameraN.png
# open camera1.png, find the pixel row where field ends and sky/stands begin,
# measuring from the BOTTOM of the frame
csm render GAMEDIR --field-cutoff 1=800 --field-cutoff 3=650
```

`--field-cutoff N=PIXELS` (repeatable, one per camera) means "the bottom
PIXELS rows, in camera N's native resolution, are the field." Everything
above that line is pre-blurred before the encoder ever sees it, which lowers
its local complexity so `aq-mode`/`psy-rd` naturally spend fewer bits there —
no encoder feature depends on this beyond what `final-h265` already uses.
Field size, tripod height, and framing all vary game to game, so there's no
sensible tool-wide default — but see Sec 5.6 for saving *this game's* values
so you don't have to retype them on every command.

This only makes sense for the simple case of a horizontal skyline. Camera
frames a genuinely irregular boundary (trees, bleachers encroaching from the
side) aren't well served by a single cutoff height — that would need
region-of-interest masking of arbitrary shape, which isn't built.

### 5.6 `GAMEDIR/csm.yaml`: persisting settings for one game

`--field-cutoff` (and a CRF/preset picked via `--sweep`, Sec 4) don't change
between commands against the same game — the tripod didn't move between your
preview and your final render. Rather than retype them every time, put them
in `GAMEDIR/csm.yaml`:

```yaml
field_cutoff: {1: 880, 2: 910, 3: 800, 4: 845}
crf: 25
preset: fast
```

Both `render` and `preview` read this file automatically if it exists. Same
override layering as `sync.yaml`'s offsets: **the file provides the
default, an explicit CLI flag on any given command still wins** for that one
run — so `csm render GAMEDIR --crf 21` produces a one-off CRF-21 render
without touching the persisted value, and everything else still uses what's
in the file.

- `field_cutoff` applies to both `render` and `preview` — it changes the
  actual picture in both.
- `crf`/`preset` apply to `render`'s `final-h265` profile, and become
  `--sweep`'s baseline for whichever dimension you *don't* explicitly sweep
  over (`--sweep preset=fast,medium` alone still needs a CRF to render each
  variant at — this is where it comes from if you don't pass `--crf`). They
  have **no effect on plain preview** (no `--sweep`), which always uses the
  fast, deliberately-unrelated-to-final-quality preview encoder.
- Written by hand today — there's no `csm render --write-defaults` (yet) to
  generate it from a chosen sweep result automatically.

---

## 6. When automatic sync can't find the sync point

`csm sync` degrades gracefully rather than failing outright, in this order —
worth understanding so a bad or partial sync is diagnosable instead of
mysterious:

1. **No audio stream on a camera.** That camera is dropped from correlation
   entirely; its offset falls back to the `creation_time` hint alone (or
   `--offset` if you give one), and `csm sync` warns about it by name.
2. **A specific link's correlation is weak.** If `creation_time` hints exist
   but the audio search inside that narrow window comes up unconvincing
   (peak-to-sidelobe ratio below `--min-confidence`), **that one link**
   silently escalates to a blind search over the full `--max-offset` range
   instead of failing. `sync.yaml` records `method: blind` for any link that
   needed this — check for it if a sync looks off. This is exactly the
   "forgotten camera" case: one camera's clock was never set, so its hint is
   wrong by minutes or hours, but the audio still finds the real answer once
   it searches widely enough.
3. **A camera's solved offset disagrees with its `creation_time` hint** by
   more than `--prior-tolerance` (default 5s). Reported as a note — "camera 3:
   clock appears unsynced" — never a failure. This is informational (fix the
   camera's clock before next game); the audio-derived offset is still used.
4. **The sync graph is disconnected** — some camera has no usable
   correlation back to camera 1 at all (e.g. its audio is silent, corrupted,
   or genuinely doesn't overlap with anything). This is a **hard error**:
   `sync graph is disconnected: camera N has no usable link back to camera 1.
   Use --offset N=SEC to anchor it.` Fix it exactly as the message says —
   figure out N's true offset by another method (manual scrubbing in
   QuickTime/VLC as a last resort, or a `creation_time`-only estimate if you
   trust the clock) and pass `--offset N=SEC`. Camera N will still render
   correctly; it's only the *automatic detection* for that one camera that
   failed.
5. **A measured offset exceeds `--max-offset`.** Hard error naming the link
   and the offending value. If your tripods are genuinely further apart in
   start time than 1800s (30 minutes), raise `--max-offset` explicitly —
   don't assume the default fits every setup.

**Before reaching for any of the above, try these first:**

- **Re-run with `--clap-at MM:SS`** if you remembered to clap (see below) —
  it replaces the statistical cross-tripod estimate with a direct,
  low-noise measurement and often resolves a marginal-confidence link
  outright.
- **Check `csm probe --audio`** — if a camera's audio is unusually quiet or
  clipped, correlation quality suffers even when a signal is technically
  present.
- **Widen the search** with `--no-time-prior` (forces every link to blind
  search — slower, but immune to a bad `creation_time` hint) or a larger
  `--max-offset`.
- **Inspect `sync.yaml` directly** — it records every individual
  measurement's lag, confidence (`psr`), and method, not just the final
  answer, specifically so a bad auto-sync is diagnosable. A link with low
  `psr` and a plausible-looking lag might still be wrong; a `residuals` block
  with `ok: false` means two different measurement paths disagreed by more
  than 3σ, which is worth investigating even if no hard error was raised.
- **Override by hand as a last resort**: `csm sync GAMEDIR --write --offset
  3=182.4` (from manual scrubbing, or a best guess) takes precedence over
  automatic detection for that camera, and everything downstream — the
  reported measurements, `sync.yaml`, the render — treats it as authoritative.

**Recording a clap is cheap insurance.** A sharp clap or clapperboard,
**at the center of the field** (not next to either tripod — a clap at one end
is biased by the ~200ms it takes sound to cross the field, which makes the
measurement *worse*, not better), **after all four cameras are already
rolling**, turns the cross-tripod offset from a statistical estimate (~25ms
standard error) into a near-exact direct measurement. Pass the timestamp with
`--clap-at MM:SS` (camera 1's clock). It's the single cheapest accuracy
improvement available and costs about ten seconds per game — but stays
optional, since correlation has to keep working for the games where nobody
remembered.

See `SYNCHRONIZATION.md` for how the correlation itself works, if you want
the mechanism rather than just the troubleshooting steps.

---

## 7. `metadata.yaml`

Optional. A flat mapping written into the render's container metadata:

```yaml
title: "vs. Ashburn United — 2026-05-03"
comment: "Home game, midfield clap at kickoff"
```

Any key valid in [ffmpeg's metadata
API](https://ffmpeg.org/doxygen/7.0/group__metadata__api.html) works. A
non-string value is coerced with `str()` and a warning is printed — it used
to silently drop *all* metadata in the old script if even one value wasn't a
string; that's fixed.

---

## 8. Portability notes

- **Stock ffmpeg only** — nothing here needs a from-source build.
  `libfdk_aac` is not used (abandoned for exactly that reason);
  `libvmaf`/`drawtext`/`libsvtav1` are used opportunistically, never required.
- **ffmpeg 7.0 is the floor**, checked at startup with an actionable message.
- **Pure-wheel Python dependencies** (numpy, scipy, PyYAML) — `pip install
  -e .` never compiles anything on macOS or Linux.
- Every command works identically on macOS and Linux; nothing in `csm` itself
  assumes a specific camera brand, resolution, or frame rate. Camera- and
  sport-specific advice lives in `docs/cameras.md` and `docs/soccer.md`,
  clearly separated from the software's own behavior.

---

## 9. Package layout

```
csm/
  cli.py         argparse subcommands (probe, sync, render, preview)
  ffmpeg.py      locate/run ffmpeg+ffprobe, version gate, capability probing
  probe.py       ffprobe -> per-segment info, cross-segment validation
  sources.py     camera discovery, segment ordering, metadata.yaml
  sync.py        GCC-PHAT, creation_time hints, topology solve, sync.yaml
  timeline.py    per-camera Track model, alignment math
  filtergraph.py PURE: Timeline -> ffmpeg -filter_complex string
  encoders.py    encoder profiles (final-h265, preview, sweep variants)
  plan.py        Timeline + profile -> FfmpegJob(s)
  runner.py      the only module that touches the filesystem / spawns ffmpeg
  preview.py     preview window resolution + plan building
  sweep.py       --sweep implementation
  render_config.py  GAMEDIR/csm.yaml -- persisted field_cutoff/crf/preset
  host.py        host capability detection
```

`sync.py` is the most interesting module if you want to understand the
correlation approach — see `SYNCHRONIZATION.md`. `docs/DESIGN.md` covers the
reasoning behind every other design decision, including several that aren't
obvious from the code alone (why the concat demuxer is used as an input
rather than a preprocessing pass, why in-graph trim is used instead of
`-ss` for head-alignment, why chunked parallel encoding isn't built in v1).
