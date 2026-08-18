# Design: rewriting the multicam pipeline

**Status:** approved, Phases 0-3 done, Phase 4's quality-tuning half done (droplet half
still open). Written 2026-08-15; scope trimmed 2026-08-16 — chunked encoding,
`final-av1`/`draft-vt`, envelope cross-correlation, automatic clap-detection, and full
droplet automation moved to conditional TODOs (§13) or later phases (§15), and the sync
design gained an explicit validate-before-building gate (§6.0). Nothing below was deleted,
only deferred; search for "deferred" to find every cut. Phase 0 landed on `main` 2026-08-16
(`b98736b`). Phase 1 (the §6.0 spike, `csm probe`/`csm sync`, GCC-PHAT, topology solve,
`sync.yaml`) landed on `csm-rewrite` 2026-08-16 (`acc8980`). Phase 2
(`timeline`/`filtergraph`/`plan`/`runner`, `csm render`, single ffmpeg job, 4K default,
color-range fix) landed on `csm-rewrite` 2026-08-16 (`5d60742`). Phase 3 (`csm preview`,
`--sweep`, metadata embedding, `check_capabilities()`, and a fix for a real concat-demuxer
seek bug found while testing) landed on `csm-rewrite` 2026-08-16 (`6b80188`). Phase 4's
quality-tuning half (locked CRF/preset/aq-strength from a real sweep, `--field-cutoff`,
`GAMEDIR/csm.yaml` persistence, `--upload-mbps`) landed on `csm-rewrite` 2026-08-17
(`c5fbf74`); the droplet half (§14) is still open.
**Implementation happens on a branch; `main` stays usable all season.**

This document is meant to be executable by someone — or something — with no memory of the
discussion that produced it. Where a number is measured, its provenance is given. Where
something is unverified, it says so rather than asserting it.

---

## 1. Why rewrite

`generate-ffmpeg.py` is 493 lines that print a bash script of ffmpeg commands by string
concatenation. It worked for a season. Four things make it painful:

1. **Manual sync.** Finding a shared moment in QuickTime and passing six frame numbers on
   the command line is tedious, and accurate to roughly ±30 ms at best.
2. **Missing or short cameras break it.** A camera that overheats, dies, or never recorded
   forces hand-built black filler video.
3. **ffmpeg feels fragile.** The git history contains roughly ten distinct approaches to
   the final render, several of which ran out of memory or silently produced wrong output.
   There are no tests, so every change is a gamble.
4. **Hardware acceleration and 2.7K source were both tried and abandoned** without a clear
   answer on whether either was worth it.

Goals, in priority order: **(1) easier to use, (2) better quality, (3) faster.**

### Fixed assumptions

These are structural and the code may rely on them:

- **4 cameras on 2 tripods** — cameras 1 and 2 on one stand, 3 and 4 on the other, at
  opposite ends of the field, roughly 60–70 m apart.
- **2×2 output matrix**, camera 1 top-left, 2 top-right, 3 bottom-left, 4 bottom-right.

### Explicitly not assumed

- **Camera make or model.** The current cameras are Akaso Brave 7, but GoPros or anything
  else must work. No camera-model knowledge belongs in the code.
- **Resolution**, input or output. See §5.2.
- **Frame rate**, input or output. See §5.3.

> **Existing camera documentation is stale.** `soccer.md` still describes 5-minute loop
> mode and 1080p — leftovers from the Brave 7 LE, never updated after the switch to the
> Brave 7. The archived footage is actually 2720×1530 in 853-second size-rollover
> segments. `cameras.md` and `soccer.md` should be relabelled as model-specific
> observations that may be out of date, and `README.md` should describe the software with
> no camera assumptions at all.

---

## 2. Measured facts

Everything in this section came from probing the two archived games,
`/Volumes/SoccerVideos/2026-04-26` and `/Volumes/SoccerVideos/2026-05-03`. Both retain the
`runme.sh` actually used, which yields **ground-truth sync offsets** to validate against.

### 2.1 Source characteristics

| Property            | Value                                                               |
| ------------------- | ------------------------------------------------------------------- |
| Resolution          | **2720×1530** — exactly 16:9, so `scale=1920:1080` is exact         |
| Codec / profile     | HEVC Main, **`pix_fmt=yuvj420p` (full range)**                      |
| Frame rate          | 30/1 exact; `nb_frames` present and exact (25590 = 853×30)          |
| Segment length      | **853 s (14.2 min)**, 3,229,614,080 B = 3.0 GiB — a *size* rollover |
| Segments per camera | 7                                                                   |
| Video bitrate       | ~30 Mbps                                                            |
| Audio               | **AAC-LC, mono, 48 kHz, 128 kbps**                                  |
| Input per game      | **~76 GB measured** on 2026-05-03 (README claims 20 GB — wrong)     |

### 2.2 The output is 5440×3060, not 4K — an unintended regression

**4K was always the intent. The 5.4K output is a mistake nobody noticed.** Neither
archived game passed the `-s` flag, so the 2720×1530 inputs were tiled directly:

|                   | 2026-04-26      | 2026-05-03      |
| ----------------- | --------------- | --------------- |
| Resolution        | **5440×3060**   | **5440×3060**   |
| Duration          | 4690 s (78 min) | 4680 s (78 min) |
| Video bitrate     | 29.4 Mbps       | **50.2 Mbps**   |
| File size         | **17.3 GB**     | **29.4 GB**     |
| Upload at 40 Mbps | ~1.0 h          | ~1.7 h          |

5440×3060 is 16.6 megapixels — **exactly double 4K**. That is why encoding runs at 2–4×
realtime and why the DigitalOcean droplet exists at all. The extra pixels are largely
wasted: YouTube's delivery ladder tops out at the rungs at or below the upload height, so
a 3060-high upload reaches essentially every viewer at 2160p, where each quadrant is
1920×1080 regardless.

**Fix it structurally, not by flipping a default.** The old code made scaling an opt-in
global flag the user had to remember, so output resolution was an emergent property of
whatever the cameras happened to record. The new design derives tile size from the
requested output size, always emits a `scale` when a source does not match, and has
`csm probe` print the resolution the render *will* produce — so this class of mistake is
visible before hours are spent.

Expected effect of moving to 3840×2160: roughly **half the encode time and half the file
size**, for detail the viewer never receives. Each quadrant becomes a 2720×1530 → 1920×1080
lanczos downscale, which is genuine supersampling and better per-pixel than shooting
1080p — the "2.7K is slightly better" intuition was correct, and this captures it properly.

### 2.3 Color range is currently mishandled

Source is `yuvj420p` (full range, 0–255). Output carries `color_range=pc` and
`color_space=bt709`, but **`color_primaries=unknown` and `color_transfer=unknown`**.
Full-range HEVC with incomplete tagging is a real hazard — players and YouTube's
transcoder can misread levels, giving crushed blacks or washed-out highlights. Convert
explicitly to limited-range BT.709, the safe delivery standard, and tag completely. Free
quality fix, invisible without probing real output.

### 2.4 `creation_time` is a useful prior — never the answer

Camera clocks are set from a phone app. In practice this gets them within about a second
of each other, but it is **not frame accurate**, and **sometimes a camera is forgotten and
never synced at all**. Comparing first-segment `creation_time` deltas against ground truth
recovered from `runme.sh`:

| Game       | Pair | `creation_time` Δ | Ground truth | Error  |
| ---------- | ---- | ----------------- | ------------ | ------ |
| 2026-04-26 | 1→2  | +3 s              | +1.600 s     | 1.40 s |
| 2026-04-26 | 1→3  | +702 s            | +701.400 s   | 0.60 s |
| 2026-04-26 | 1→4  | +699 s            | +698.867 s   | 0.13 s |
| 2026-05-03 | 1→2  | −1 s              | −1.467 s     | 0.47 s |
| 2026-05-03 | 1→3  | +182 s            | +182.433 s   | 0.43 s |
| 2026-05-03 | 1→4  | +184 s            | +184.633 s   | 0.63 s |

All six agree within 1.4 s, consistent with the tag's 1-second resolution.

The design consequence is narrow and specific: **`creation_time` narrows the audio search
window; audio determines the answer.** A 1.4 s prior error is irrelevant when steering a
±5 s search and catastrophic if trusted directly — 1.4 s is 42 frames.

### 2.5 Ground-truth sync offsets (validate against these)

Recovered from the `-ss` trim values in each `runme.sh`, relative to camera 1:

```
2026-04-26:   cam1 0.000    cam2 +1.600    cam3 +701.400   cam4 +698.867
2026-05-03:   cam1 0.000    cam2 −1.467    cam3 +182.433   cam4 +184.633
```

Cross-tripod offsets are **minutes**, because of the walk to the other end of the field to
set up the second tripod. Intra-tripod offsets are small (1.6 s, 1.5 s). Consequences: a
`--max-offset` of 120 s would fail outright, and the first segments of cameras 1/2 and 3/4
may barely overlap — a 701 s offset against 853 s segments leaves little in common.

### 2.6 Workflow evidence

`2026-04-26` contains both `final_video.mp4` and `final_video_untrimmed.mp4`, and its
`runme.sh` has stages 1 and 2 commented out — the generated script was hand-edited to
re-run only the tiling stage, then the result was trimmed separately. That is direct
evidence for first-class `--start`/`--end` flags, for resumability, and for keeping the
generate-inspect-run habit. Orphaned `filelist-*` temp files are still sitting in the
camera directories.

---

## 3. Benchmarks

All measurements on the iMac Pro: Intel Xeon W-2140B, 8 physical / 16 logical cores,
32 GB RAM, macOS 15.7.9, **ffmpeg 9.0.1**.

### 3.1 Real footage — the numbers to plan with

24 seconds of 4K30 output built from the four 2720×1530 cameras of `2026-05-03`, seeked to
10 minutes into the common timeline using the ground-truth offsets from §2.5. Extrapolated
to the full 4680-second game by ×195.

| Config                                          | wall (24 s out) | full game  | size (24 s) | full game   |
| ----------------------------------------------- | --------------- | ---------- | ----------- | ----------- |
| today's settings @ 5440×3060 — *what ships now* | 292.1 s         | **15.8 h** | 166.3 MB    | **32.4 GB** |
| today's settings @ 3840×2160                    | 129.1 s         | 7.0 h      | 83.4 MB     | 16.3 GB     |
| proposed profile @ 3840×2160                    | 205.1 s         | **11.1 h** | 140.6 MB    | **27.4 GB** |

("Proposed profile" is CRF 23, 10-bit main10, `aq-mode=3`, `psy-rdoq=1.0`,
`rc-lookahead=40`, no `fast-intra`. The single-process figure was measured twice — 203.4 s
and 206.8 s — so run-to-run variance is under 2 %.)

**The extrapolation is validated against reality:** 32.4 GB predicted for today's settings
versus the real archived `2026-05-03/final_video.mp4` at **29.4 GB** — within 10 %, from a
single busy 24-second window. The method can be trusted.

Three conclusions, and the second one corrects an earlier assumption:

- **The 4K change alone is 2.26×** and halves the file size. Larger than the ~2×
  previously estimated.
- **The proposed quality profile spends about half of that back** — it is 1.59× slower
  than today's encoder settings at the same resolution. Quality is not free.
- **Net effect: 1.42× faster than today, 15 % smaller, and better per-pixel quality.**
  A clear win, but not the "half the time" figure earlier assumed.

**A full game takes about 11 hours on this Mac** with the proposed profile. That is the
honest number, and it is why the droplet exists. Extrapolating from the archived games'
~2-hour droplet runs at 5440×3060 implies the 32-core droplet is roughly 8× this machine,
which puts the proposed profile at **~1.4 h of droplet encode**.

### 3.2 Chunked parallel encoding: the configuration decides everything

Same 24 seconds of real footage, same encoder settings, machine otherwise idle:

| Config                                     | wall        | vs single               |
| ------------------------------------------ | ----------- | ----------------------- |
| single process, x265 default threading     | 203.4 s     | 1.00× (reference)       |
| 2 chunks × 12 s, `pools=8:frame-threads=2` | **166.0 s** | **1.24× faster**        |
| 4 chunks × 6 s, `pools=4:frame-threads=2`  | 257.3 s     | **0.80× — 20 % slower** |

**Chunking is not a free win; it is a tuning parameter that can go either way**, and the
spread between the best and worst configuration here is 1.55×. Too many concurrent
processes oversubscribe the cores, multiply the per-chunk seek-and-decode startup cost, and
multiply memory pressure. Note that a naive `jobs = physical_cores / 2` heuristic would
pick 4 jobs on this machine — the configuration that is *slower* than not chunking at all.

Both chunked runs concatenated correctly: 720 frames in every case, byte sizes within
0.3 % of the single-process encode. **Correctness is not in question; only the performance
premise is.**

Design consequence: **do not hardcode a jobs/pools heuristic.** See §9.1.

### 3.3 Encoder comparison — synthetic content, ratios only

A separate matrix was run on synthetic `testsrc2` + noise. **Absolute values are inflated
and not comparable to §3.1** — high-frequency noise is far more expensive to encode and
compresses very differently from real footage. Use these for *relative* comparison only.

| Config                                  | wall    | CPU core-s | peak RSS |
| --------------------------------------- | ------- | ---------- | -------- |
| filtergraph only (`-f null`)            | 14.7 s  | 205        | 821 MB   |
| `libx265 -preset fast -crf 24`          | 102.1 s | 1406       | 2.41 GB  |
| `libx265 -preset medium -crf 24`        | 171.2 s | 2332       | 2.64 GB  |
| `libx265 -preset medium -crf 24` 10-bit | 205.4 s | 2724       | 3.66 GB  |
| `libsvtav1 -preset 6 -crf 30`           | 90.6 s  | 1117       | 9.51 GB  |
| `libsvtav1 -preset 8 -crf 30`           | 70.5 s  | 882        | 6.85 GB  |
| `hevc_videotoolbox -b:v 40M`            | 17.6 s  | 212        | 931 MB   |

- **`hevc_videotoolbox` is roughly an order of magnitude faster** than x265 medium (9.7×
  here, 4.7× on the earlier 8.0.1 real-footage run — the ratio is content-dependent, the
  direction is not) and ~11× cheaper in CPU.
- **SVT-AV1 preset 6 is ~1.9× faster than x265 medium**, consistent with the 1.55×
  measured earlier on different content.
- **SVT-AV1 is extremely memory-hungry — 9.5 GB peak RSS** against x265's 2.6 GB. This is a
  hard constraint on how many AV1 jobs can run concurrently and must feed the sizing in
  §9.1.
- **10-bit costs +20 % wall, +17 % CPU** — squarely in the 15–25 % range previously
  estimated — and, notably, **produces marginally *smaller* files at matching CRF**
  (0.3–1.4 % smaller). The cost of 10-bit is time, not bitrate.
- A CRF sweep on this synthetic content showed ~35–38 % size change per CRF step, far
  steeper than the textbook ~11 %. **That table is not usable for file-size budgeting** —
  it is an artifact of noise-dominated content. Use §3.1 and §7.4 instead.
- `libx265 -preset slow` was not measured; it is not a candidate for any profile.
- **Decode + scale + tiling is a small fraction of the pipeline** — 205 of 2332 core-s
  here (8.8 %), and 16.3 of 659 core-s (2.4 %) on the earlier real-1080p run. Synthetic
  noise inflates decode disproportionately, so real footage sits at the low end. Either
  way it is comfortably under 10 %, which is what justifies doing all trimming inside the
  filtergraph and deleting the separate alignment pass (§5.4).

### 3.4 Verified ffmpeg behavior

Confirmed on 9.0.1:

- **`libvmaf` IS present in 9.0.1** (`--enable-libvmaf`, filter listed). **Absent in
  8.0.1** — a real change, and it means quality sweeps can use VMAF.
- **`drawtext` is absent** (no freetype in `-buildconf`). `drawbox` is present. No
  burned-in text labels without rebuilding ffmpeg.
- **`hevc_videotoolbox` still rejects constant quality** on this Intel/T2 Mac, with the
  same error as on 8.0.1: `-q:v qscale not available for encoder. Use -b:v bitrate
  instead.` — the encoder fails to open. ABR-only, no rate-distortion optimization. Fast,
  but not a master encoder. Apple Silicon accepts `-q:v` (§8). **This is load-bearing and
  was explicitly re-verified.**
- **`amerge` warns** `Input channel layouts overlap` even with an explicit mono `aformat`,
  and on 9.0.1 additionally emits `No channel layout for input 1` just before it.
  **`join=inputs=2:channel_layout=stereo` is completely silent.** Use `join`.
- **Raw HEVC Annex-B chunks concatenate exactly.** Confirmed twice: two synthetic encodes
  (2 s + 3 s) decoded to exactly 150 frames / 5.00 s, and both real-footage chunked runs in
  §3.2 produced exactly 720 frames with byte sizes within 0.3 % of the single-process
  encode. This is the safe substrate for chunked encoding; it sidesteps every mp4
  edit-list, encoder-delay and timescale hazard.
- **x265 at 4K self-selects only 4 frame threads** —
  `x265 [info]: frame threads / pool features : 4 / wpp(34 rows)`, byte-identical across
  8.0.1 and 9.0.1. This is the mechanism behind the "25–27 core ceiling" observed on
  32-core droplets, and the reason chunking can help on a large box even though it does
  not reliably help here.
- `tpad` supports `start_duration`, `stop_duration`, `start_mode`/`stop_mode`, `color`.
- `trim=start=N,setpts=PTS-STARTPTS` + `tpad` + `xstack` produced exactly the expected
  frame count. Frame-accurate.
- Plain `-ss T` before `-i` lands on the correct frame **and preserves the sub-frame
  residual** in the rebased PTS. Do not blindly `setpts=PTS-STARTPTS` after it or that
  residual is discarded.
- `-copyts -ss (T−2) -i f -vf "trim=start=T"` gives byte-identical frames to full-decode
  plus trim. Coarse input seek with exact in-graph trim is safe.
- Seek cost is nil: `-ss 400` plus decoding 2 frames took 0.17 s.
- Audio decode is nearly free: 473 s of AAC → mono 8 kHz PCM in 0.59 s.

---

## 4. Package layout

```
pyproject.toml            # project "csm"; deps numpy, scipy, PyYAML; dev: pytest
csm/
  cli.py            # argparse subcommands, flag -> config resolution
  ffmpeg.py         # locate + version gate + capability probe; run(argv); FfmpegError;
                    #   dry-run shell emitter (shlex.join)
  probe.py          # ffprobe -> FileInfo dataclasses + validation with specific errors
  sources.py        # camera discovery, segment ordering, concat lists, metadata.yaml
  sync.py           # offset sources, gcc_phat, topology solve, sync.yaml I/O
  timeline.py       # Timeline/Track model, head/tail math, alignment policy
  filtergraph.py    # PURE: Timeline + profile -> filter_complex string
  encoders.py       # EncoderProfile dataclass + built-in profiles
  host.py           # arch/CPU/RAM/encoder detection -> defaults
  plan.py           # PURE: Timeline + profile + host -> list[FfmpegJob]
  runner.py         # execute jobs with dependencies, pool, resume, progress
legacy/generate-ffmpeg.py   # frozen, unchanged, kept for one season
tests/
```

`runner.py`'s pool/resume responsibilities are the §13.5 chunked-render feature; in v1
`plan.py` emits a single `FfmpegJob` and `runner.py` just runs it, no pooling or resume.

**Hard rule, enforced by the structure:** `filtergraph.py` and `plan.py` are pure
functions returning strings and `list[str]` argv. They never touch the filesystem and
never spawn a process. This is what makes the whole thing testable without encoding
anything, and it is the direct answer to "any time I touch it I break it."

---

## 5. The timeline model

One abstraction makes missing and short cameras stop being special cases.

```python
@dataclass
class Track:
    cam: int; present: bool
    clips: list[Path]         # ordered segments; [] if absent
    start: float              # position on the COMMON timeline of this camera's own t=0
    duration: float | None
    width: int; height: int; fps: Fraction
    has_audio: bool; sample_rate: int; channels: int

@dataclass
class Timeline:
    t0: float; t1: float
    out_w: int; out_h: int              # from --out-size, default 3840x2160
    fps: Fraction                       # output rate, default 30
    tracks: list[Track]                 # always 4, cameras 1..4
    audio_cams: tuple[int, ...] = (2, 4)

    TRIPODS = ((1, 2), (3, 4))          # fixed rig
    LAYOUT  = "0_0|w0_0|0_h0|w0_h0"

    @property
    def tile(self) -> tuple[int, int]:
        return self.out_w // 2, self.out_h // 2
```

### 5.1 Alignment policy

- `t0 = max(track.start)` — `--align-start latest`, matching current behavior and
  confirmed by ground truth, where the last-starting camera always had trim 0.
- `t1 = max(start + duration)` — `--align-end longest`, **new default**, so a camera dying
  at 70 minutes no longer truncates the game.
- `--align-start earliest` and `--align-end shortest` available.
- `--start` / `--end` / `--duration` override explicitly, supporting the manual trim done
  by hand on 2026-04-26.

Two derived numbers drive everything:

```
head = t0 - track.start                       # >0 trim, <0 pad black at front
tail = t1 - (track.start + track.duration)    # >0 pad black at end
```

| Case       | Video                                                  | Audio (only for `audio_cams`)      |
| ---------- | ------------------------------------------------------ | ---------------------------------- |
| `head > 0` | `-ss {head}` before `-i`                               | same input seek                    |
| `head < 0` | `tpad=start_duration={-head}:color=black`              | `adelay=delays={-head*1000}:all=1` |
| `tail > 0` | `tpad=stop_duration={tail}:color=black`                | `apad=whole_dur={t1-t0}`           |
| absent     | `-f lavfi -i color=c=black:s={tile}:r={fps}:d={t1-t0}` | `-f lavfi -i anullsrc=...`         |

Tile with **no `shortest`** — that flag is exactly what made a dead camera truncate the
game — bounded instead by `-t`:

```
[v0][v1][v2][v3]xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0:fill=black[outv]
[m2][m4]join=inputs=2:channel_layout=stereo:map=0.0-FL|1.0-FR[outa]
```

### 5.2 Resolution: any input, any output

| Today                                                   | New                                                                                                                                           |
| ------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| tile hardcoded 1920×1080; scaling an opt-in global `-s` | **tile = out-size ÷ 2 in each dimension**; `scale` emitted **per track**, only when that camera's probed size differs                         |
| input assumed 1920×1080                                 | **any input resolution** — 1080p, 2.7K, 4K, and mixed resolutions within one game, since each track scales independently from its probed size |
| output whatever the inputs implied                      | **`--out-size`, default `3840x2160`**; `native` reproduces the old behavior                                                                   |

Scaling preserves aspect ratio rather than assuming 16:9 — costs nothing, and an
odd-aspect camera letterboxes instead of distorting:

```
scale={tw}:{th}:force_original_aspect_ratio=decrease:flags=lanczos+accurate_rnd
  :in_range=pc:out_range=tv,
pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color=black,format=yuv420p,setsar=1
```

Common cases: 1080p → 1080p tile is scale-free; 2720×1530 → 1920×1080 is a 1.42×
downscale (the supersampling win); 4K → 1920×1080 is a clean 2× downscale.
`--scaler {bicubic,lanczos,spline}` for the ringing-on-white-field-lines case.

### 5.3 Frame rate: input 30 or 60, output whatever you ask

Input rate is **probed per camera**; output rate is `--fps`, **defaulting to 30**
regardless of what was shot. `fps=fps={out_fps}` per track handles 60→30, 30→30, and
cameras shooting at *different* rates in one game. Because offsets are stored in seconds
throughout, none of the sync math changes.

Three things must derive from the output rate rather than stay hardcoded:

- **GOP** — `keyint`/`min-keyint` = `2 × out_fps`. Today's hardcoded 60 is correct by
  accident at 30 fps and would silently become a 1-second GOP at 60.
- **Chunk boundaries**, which must remain integer multiples of the GOP.
- **`--offset-frames`**, which converts using the output rate.

Notes worth documenting for users:

- **60→30 drops frames; it does not blend.** Correct for sports — you want crisp frames to
  pause on — but the retained frames were shot at a ≤1/60 s shutter, so motion reads
  slightly more stroboscopic than natively-30 footage. Interpolation (`framerate`,
  `tblend`) introduces motion blur and pan artifacts; not worth it here.
- **Shooting 60 doubles storage** (~160 GB/game) and doubles decode work — but decode is
  under 10 % of the pipeline, so encode cost is unchanged when output is 30. Cheap in CPU,
  expensive in card space.
- **60 fps *output* is a different bitrate regime.** YouTube's high-frame-rate 4K band is
  53–68 Mbps against 35–45 at 30 fps, so a CRF chosen at 30 will produce a proportionally
  larger file at 60. Re-run the sweep if output rate changes.

### 5.4 Intermediate files: four copies down to two

Today: originals + `concatinated.mp4` + `combined.mp4` + output. Both intermediates are
stream copies, so each is roughly the size of the originals: 76 + 76 + 76 + ~25 ≈ **250 GB
of disk for a 76 GB game.**

- **The alignment pass is deleted.** `ffmpeg -ss T -i in -c copy` cannot cut mid-GOP, so it
  snaps to the nearest keyframe — that *is* the residual sync error — and it writes a
  full-size duplicate per camera. The final stage re-encodes anyway, and decode+tile is
  under 10 % of the pipeline (§3.3), so trimming there is free and frame-accurate.
- **The concat pass is deleted** in the common case: the concat demuxer becomes an
  *input* (`-f concat -safe 0 -protocol_whitelist file -i {tmpdir}/cam1.txt`), with the
  list in a `TemporaryDirectory` that is actually cleaned up. Today's `filelist-*` files
  are left behind permanently and are written with `bytes(f, 'ascii')`, which raises
  `UnicodeEncodeError` on any non-ASCII path. `--precat` restores the old behavior.

Result: **originals + output only** — single-process rendering is the only mode in v1
(§9.1). Chunked mode, when §13.5 gets built, would add roughly 1× output size transiently,
deleted on success.

**Revised in Phase 3, from real ffmpeg behavior, not assumption**: head-trim (a camera that
starts before the common timeline t0) was originally meant to use `-ss` before the concat
demuxer's `-i`, matching §3.4's verified-accurate plain-file seek. Verified against real
footage that this is unreliable specifically for the concat demuxer whenever the source has
audio — real camera clips always do — failing outright (empty/video-less output, exit code
0, no error raised) for any nonzero seek, down to 0.1s. Fixed by trimming in-graph instead
(`trim=start=.../atrim=start=...`, the same frame-accurate pattern §3.4 already verified for
plain files) — correct, but decode-and-discard from the start of the concat sequence, which
is fine for a full render (head is at most the camera-to-camera sync offset) but was
initially unbounded for a `--preview` window deep into a long game (observed: 80+ minutes of
decode to reach a 60s window). Phase 4 bounded it: whole leading segment *files* are pruned
from the concat list first (free — fewer `-i` entries, no seeking), so the in-graph trim
only ever pays for at most one segment's worth of decode, never the camera's full history.

### 5.5 Audio source selection

`--audio-cams`, still defaulting to `2,4`. Which mics face usefully is a camera-model
property, so it must not be baked in. `csm probe --audio` prints per-camera levels
(`volumedetect` mean/peak) so the choice is informed rather than folklore, and
`--audio-gain 2=+6dB` fixes a quiet mic.

---

## 6. Sync

### 6.0 Validate before building

**Before any of §6.1–§6.6 gets built, spike the core claim.** Run GCC-PHAT directly against
the extracted audio of the two archived games (`2026-04-26`, `2026-05-03`) — no `sync.yaml`,
no CLI, no topology solve, just the correlation — and compare the recovered offsets against
the ground truth in §2.5. This is the highest-leverage check in the whole document, because
everything from §6.1 onward is infrastructure *around* that correlation, and none of it is
worth building if the correlation itself is not reliable enough to trust.

Success criterion: offsets recovered within 1/60 s of ground truth on the intra-tripod pairs
(1↔2, 3↔4), and reasonable agreement on the cross-tripod pairs (1↔3, 2↔4) given the ~120 ms
per-window σ discussed in §6.4. This is the same check as §16's first verification step,
just promoted to a gate that happens before implementation rather than after.

If it validates, proceed to build `csm sync` as described below. If it does not, stop here —
the manual method continues, and automatic sync stays unbuilt rather than half-built on a
correlation that hasn't earned trust. Either way, this spike is cheap: it needs the audio
extraction command from §6.2 and the `gcc_phat` function from §6.3, nothing else.

### 6.1 Pluggable offset sources, with audio as the authority

| Source                      | Accuracy    | Role                            |
| --------------------------- | ----------- | ------------------------------- |
| `TimecodeSource`            | frame-exact | **not implemented** — see §13.2 |
| `AudioSource` (GCC-PHAT)    | ~1 frame    | **the authority today**         |
| `CreationTimeSource`        | ~1 s        | search-window hint only         |
| `ManualSource` (`--offset`) | user's call | always wins                     |

Flow:

1. **Hint from `creation_time`**, when present and self-consistent. Narrows the audio
   search to ±`--prior-window` (default 5 s), including the 180–700 s cross-tripod gaps
   that would otherwise need a blind search over minutes of audio.
2. **Audio determines the offset** by GCC-PHAT within that window.
3. **Automatic blind fallback** — not an opt-in flag — whenever the hint is missing or
   correlation inside the hinted window falls below `--min-confidence`. Decimated coarse
   search over `--max-offset` (**default 1800 s**, sized from the observed 701 s worst
   case), then the same refinement. `--no-time-prior` forces this path for testing.

**The forgotten-camera case is a first-class path.** A camera whose clock was never synced
has a `creation_time` wrong by minutes or hours, so its hinted window contains nothing
correlated:

- Per link, if correlation inside the hinted window is below confidence, **silently
  escalate that link to blind search** and record `method: blind` in `sync.yaml`. Only the
  affected link pays the cost.
- After solving, compare each camera's final offset against its hint and report
  disagreement beyond `--prior-tolerance` (default 5 s) as
  `camera 3: clock appears unsynced (tag implies +182 s, audio found +2140 s)` — useful
  feedback for next game, never a failure.
- If *every* camera disagrees consistently, that is a clock-set problem, not a sync
  problem. Say so once rather than emitting four warnings.

**Invariant to state in code and docs: the final number always comes from audio or an
explicit `--offset`. `creation_time` only ever decides where to look.**

### 6.2 Getting the audio

```
ffmpeg -v error -ss {window_start} -t {window_len} -i {segment} -vn -map 0:a:0 \
       -ac 1 -ar 8000 -af "highpass=f=250,lowpass=f=3500" -c:a pcm_f32le -f f32le -
```

- **8 kHz** gives 125 µs resolution — 1/267 of a frame. 4 kHz Nyquist keeps referee
  whistles (2–4 kHz fundamentals) intact.
- **The bandpass is not optional.** Outdoor action cams pick up enormous uncorrelated wind
  rumble below ~200 Hz that dominates the PHAT spectrum. Filter it in ffmpeg, where it is
  free, rather than in scipy.
- Source audio is mono 48 kHz, so there is no channel-selection ambiguity.

### 6.3 GCC-PHAT

`sync.gcc_phat(a, b, fs, max_lag_s, interp=4) -> DelayEstimate`: rfft both signals,
`R = A · conj(B)`, PHAT-whiten by dividing by `|R|`, irfft with 4× zero-padding, restrict
to the lag window, take the peak, refine with a 3-point parabolic vertex. About 0.2 sample
accuracy (~25 µs). Precision is not the limiting factor; acoustics is.

Confidence is **peak-to-sidelobe ratio** — peak divided by the median absolute correlation
outside a ±5 ms guard band. `psr ≥ 8` confident, `4–8` marginal (warn but use), `< 4`
reject.

### 6.4 Exploiting the tripod topology

This belongs in a code comment, because it explains why many windows are used rather than
one good one.

- **Intra-tripod (1↔2, 3↔4):** mics inches apart, direct-path delay difference under 1 ms
  regardless of where the sound originated. Essentially exact — expect `psr` of 20–100 and
  σ of 1–3 ms.
- **Cross-tripod:** a sound at field position *p* biases each window by
  `(|p−B| − |p−A|)/343`, ranging over roughly **±200 ms** and near zero for midfield
  sources. Tripods are placed symmetrically and play is statistically centered, so the
  bias is close to zero-mean. Theil–Sen over K windows with σ ≈ 120 ms gives a standard
  error of ~σ/√K — **about 25 ms at K=24, under one frame.**

Take `--sync-windows` (default 24) windows of `--sync-window-len` (default 20 s) spread
across the mutual overlap, skipping the first 60 s after the last camera starts. Reject
windows below `--min-confidence` or below the 20th-percentile RMS (silence, halftime). Fit
with MAD outlier rejection then Theil–Sen (`scipy.stats.theilslopes`); the intercept is the
offset.

Measure **both** cross links (2↔4 and 1↔3), not one. They are independent samples of the
same tripod-to-tripod delay plus known intra-tripod offsets, which halves the error and
gives a consistency check.

**Solve.** Unknowns `t1..t4`, gauge-fixed at `t1 = 0`; each measurement gives
`t_j − t_i = m_ij ± σ_ij`; weighted least squares (`w = 1/σ²`, `numpy.linalg.lstsq`).
Return the solution **and per-measurement residuals**. Any residual beyond 3σ is reported
as an inconsistency (`"1→2→4 and 1→3→4 disagree by 210 ms; one cross-link is probably
wrong"`). This over-determination is the difference between "auto-sync worked" and
"auto-sync silently handed you a garbage number," and it costs nothing because the
correlations already exist.

**v1 uses PHAT only.** An `envelope` cross-method (frame at 10 ms hop, log-RMS,
mean-subtract, correlate envelopes — coarser but more robust when the two tripods hear
genuinely different spectra) is deferred to §13.3, added only if a real game shows PHAT
struggling on cross-tripod correlation. Until then there is no `--cross-method` flag.

### 6.5 Clock drift: diagnostic only

Measured under 2 frames over 90 minutes, which is acceptable. Report the Theil–Sen slope
as one line and warn only above 3 frames. **Build no correction machinery** — no
`asetrate`, no `setpts`, both of which the git history records as causing trouble.

If it ever matters, §13.5's chunked renderer (if built) already recomputes each track's
`head` per chunk, so feeding it `offset(t) = intercept + slope·t` at the chunk midpoint
would make drift correction piecewise-constant with no new machinery.

### 6.6 `sync.yaml` — the manual-override contract

**Auto-sync writes a file that can be edited; rendering reads that file and never silently
re-detects.**

```yaml
# GAMEDIR/sync.yaml — written by `csm sync`, edited freely, consumed by `csm render`
version: 1
method: creation-time-prior + audio-refine
cameras:
  1: {offset:   0.000, duration: 5312.4, confidence: ref,  prior:   0}
  2: {offset:   1.600, duration: 5309.8, confidence: 41.2, prior:   3}
  3: {offset: 701.400, duration: 5288.1, confidence:  9.8, prior: 702}
  4: {offset: 698.867, duration: 4102.6, confidence:  8.1, prior: 699}
measurements:
  - {pair: [1,2], lag:   1.600, sigma: 0.0021, psr: 41.2, windows: 24/24, kind: intra-tripod}
  - {pair: [3,4], lag:  -2.533, sigma: 0.0018, psr: 38.9, windows: 22/24, kind: intra-tripod}
  - {pair: [2,4], lag: 697.267, sigma: 0.0310, psr:  9.1, windows: 19/24, kind: cross-tripod}
  - {pair: [1,3], lag: 701.400, sigma: 0.0338, psr:  8.4, windows: 18/24, kind: cross-tripod}
residuals: {max: 0.014, ok: true}
prior_agreement: {max_delta: 1.40, ok: true}
```

Overrides layer in order — `sync.yaml` → `--offset N=SEC` → `--nudge N=±SEC`, with
`--offset-frames N=F` for muscle memory.

Degradation ladder: (1) no audio stream → drop from sync, warn, fall back to the hint alone
and say so; (2) low confidence → retry `envelope`, then longer windows, then blind search,
then fail that link; (3) a link fails but the graph stays connected → solve and say so;
(4) graph disconnected → hard error naming the unanchored camera and the flag to fix it;
(5) offset beyond `--max-offset` → hard error.

### 6.7 A clapper is worth it — but only if you clap at midfield

A sharp broadband transient is the ideal correlation target: high SNR, wide bandwidth,
unambiguous peak. It would take the cross-tripod estimate from a statistical result
(~25 ms standard error from averaging) to a direct measurement good to a few milliseconds.

**The non-obvious part is where and when, and getting it wrong makes it worse than
useless:**

- **Clap at the center of the field, not next to a tripod.** Sound takes ~200 ms to cross
  the 60–70 m between tripods, so a clap at one end produces a confident, precise, and
  *wrong* answer biased by the full propagation delay. Midfield is equidistant, so the
  bias cancels. This single detail decides whether a clapper helps or hurts.
- **Clap after all four cameras are recording** — at the end of setup, not the beginning.
  The archived games show the tripods started 182 s and 701 s apart because of the walk,
  so a clap during setup would land in only two cameras.
- **Any sharp impulsive source works**: a clapperboard, two blocks of wood, a single loud
  handclap, an air horn. Avoid phone speakers, which are neither loud nor impulsive enough
  outdoors. Two or three claps a second apart give redundancy and let the software confirm
  consistency.

**Software support in v1 is manual only:** `--clap-at MM:SS` points the correlator at the
clap directly, using it as the cross-tripod measurement for that link. Record which method
produced each measurement (`kind: clap` vs `kind: cross-tripod`) so a bad clap is
diagnosable rather than mysterious. **Automatic** detection of the sharpest broadband onset
common to all four cameras is deferred to §13.4 — it's real onset-detection engineering, and
manual `--clap-at` already captures the accuracy win at near-zero implementation cost.

Document the procedure in the recording instructions. It costs ten seconds per game and is
the cheapest available accuracy improvement — but it must stay optional, since the
correlation path has to keep working for games where nobody remembered.

---

## 7. Encoding and quality

### 7.1 What YouTube actually recommends

From YouTube's "Recommended upload encoding settings" Help Center page:

- **Container** MP4, `moov` atom at front (Fast Start), **no Edit Lists**.
- **Video** H.264 High profile, **2 consecutive B-frames**, **closed GOP of half the frame
  rate**, CABAC, variable bitrate, 4:2:0 chroma, progressive.
- **Audio** AAC-LC or Opus, **48 kHz**, stereo.
- **SDR 2160p at 24–30 fps: 35–45 Mbps.** (1440p 16, 1080p 8.) HDR 4K: 44–56.
- **Audio: stereo 384 kbps** (mono 128, 5.1 512).
- HEVC, VP9, AV1 and ProRes are accepted but not listed; the published table is
  H.264-oriented.

Four things follow, and they matter more than the table:

1. **Those are H.264 numbers.** At equal quality HEVC needs roughly 50–60 % of H.264's
   bitrate, AV1 roughly 45 %. The current 29–50 Mbps HEVC master is therefore *well above*
   YouTube's 4K band — consistent with it being oversized at 5440×3060.
2. **YouTube re-encodes everything**, so the master is input to their encoder and
   generation loss compounds. That is the argument for a lower CRF, not the bitrate table.
   Uploading at ≥2160p also earns the VP9/AV1 delivery ladder rather than H.264-only.
3. **GOP:** YouTube says half the frame rate (15 at 30 fps); this design uses `2 × fps`
   (60 at 30 fps). Since YouTube fully re-encodes with its own GOP structure the effect on
   delivered quality is minimal, and 60 is more efficient and is what chunking requires.
   **Keep 60** — recorded here as a decision, not an oversight.
4. **Audio is currently under spec** at 96 kbps / 32 kHz, and 32 kHz needlessly resamples
   the camera's native 48 kHz. Use **192 kbps AAC-LC at 48 kHz stereo**. YouTube's 384 kbps
   figure would cost ~250 MB on a 20 GB file if matching the letter of the spec matters.

### 7.2 Final profile (`final-h265`, default)

```
-c:v libx265 -preset fast -crf 23 -pix_fmt yuv420p10le -profile:v main10
-x265-params keyint={2*fps}:min-keyint={2*fps}:no-open-gop=1:repeat-headers=1:
             aq-mode=3:aq-strength=0.3:psy-rd=2.0:psy-rdoq=1.0:rc-lookahead=40:ref=4
-color_primaries bt709 -color_trc bt709 -colorspace bt709 -color_range tv
-tag:v hvc1
-c:a aac -b:a 192k -ar 48000 -ac 2
-movflags +faststart
```

**Phase 4 update, from a real `--sweep` against actual game footage** (not the synthetic
extrapolation this section was originally written against): `preset` moved from `medium` to
`fast`. Measured on 2026-05-03 (24s window, right as all four cameras go live): `medium`
costs 293.6s wall for a 78.04 VMAF full-4K encode; `fast` costs 211.0s (≈28% faster) for
77.55 VMAF — half a VMAF point, in the same "diminishing returns" territory the CRF
comparison below already shows. CRF 23 itself was kept as-is: CRF 21/23/26 scored
79.50/78.04/75.33 VMAF at sizes 45.6/31.7/17.3 GB extrapolated-full-game — CRF barely moves
quality relative to how much it moves size, echoing this document's own §3.1 finding that
resolution buys far more than CRF does.

**`aq-strength` moved from `1.0` to `0.3`**, from a real sweep against the worst-case-turf
game (2026-04-19, §7.7): 1.0→0.3 shrank the file ~13% for a 0.26 VMAF point cost, roughly
linear the whole way with no sign of a cliff by 0.3 — a real, favorable, monotonic tradeoff,
unlike `psy-rd`, which was flat across 0.5–2.0 on the same content (barely 1% size change).
See §13.6 for the TODO on testing further below 0.3 and exposing this as a real option
instead of a fixed value.

**Treat all of this as provisional**, not final: re-sweep after real experience with
`--field-cutoff` (§7.7) and again on any new hardware (droplet, Apple Silicon) — see
`docs/REFERENCE.md` §5.3 for the full table and `csm preview --sweep`'s usage (§7.3 below).

Changes from today, each with a reason:

- **Output 3840×2160 instead of 5440×3060** — the big one. ~2× faster, ~2× smaller, for
  detail YouTube's delivery discards.
- **Explicit full→limited range conversion and complete BT.709 tags** — fixes the
  `yuvj420p` / `color_range=pc` / `primaries=unknown` mismatch found in real output (§2.3).
- **Drop `fast-intra=1`.** A pure speed hack that degrades intra prediction. Grass is
  intra-expensive and a full I-frame is coded every 2 seconds. The clearest
  quality-for-nothing loss in the current settings.
- **10-bit (`main10`).** Encoding 8-bit source through a 10-bit pipeline is a
  well-established 5–10 % BD-rate win in x265 — it removes rounding error in the
  transform/prediction loop — and it eliminates banding in sky, shaded grass and evening
  light. Costs roughly 15–25 % encode time. YouTube accepts Main10.
- **CRF 26 → 23**, subject to the sweep below. Because resolution halves at the same time,
  this lands near today's file size and upload time at roughly half the encode time, with
  better per-pixel quality.
- **`aq-mode=3`** (default 2) adds a dark-scene bias, helping evening games and shaded
  halves. **`psy-rdoq=1.0`** (preset medium leaves it at 0) preserves grass texture instead
  of letting RDO smear it into flat green. **`rc-lookahead=40`** (default 20) allocates
  bits better across pan-induced scene changes.
- **Leave strictly alone:** `ctu`, `rd`, `me`, `subme`, `rect`/`amp`, `b-adapt`, `qcomp`,
  `sao`. Preset medium's choices reflect far more tuning than this project can replicate.
  In particular do **not** apply the widely circulated `--no-sao` tip — SAO helps on noisy
  content like this. (`ctu_size=64` was already correctly found to be a no-op; it is the
  default.)

### 7.3 Settle settings by measurement, not argument

The unreachable encoder profiles in the current code were speed/quality experiments that
were never resolved, because comparison was expensive. Make it a command:

```
csm preview GAMEDIR --sweep crf=21,23,25,26
csm preview GAMEDIR --sweep encoder=libx265,libsvtav1 --sweep preset=fast,medium
```

Each variant renders the same 60-second window and the tool prints **wall time, file size,
extrapolated full-game size, extrapolated upload time at 40 Mbps, and an objective quality
score** against a lossless reference of the same window.

Scoring prefers **`libvmaf`**, which ffmpeg 9.0.1 on Homebrew now provides — VMAF is the
metric large-scale encoding decisions are actually made with and correlates far better
with perceived quality than PSNR. Fall back to **`xpsnr`** (perceptually weighted, always
present) plus `ssim` when unavailable, which is still likely on Linux distro builds. Detect
via `ffmpeg.check_capabilities()` and always report which metric was used, so scores are
never silently compared across metrics.

Record the chosen numbers and their scores in the README so next season starts from
evidence.

### 7.4 Upload size and the 40 Mbps constraint

The two archived masters are 17.3 GB and 29.4 GB — **1.0 to 1.7 hours of upload each.**
Sizes below marked *measured* are extrapolated from the real-footage runs in §3.1, whose
method validated to within 10 % against the archived files:

| Master                                  | Size (78 min)   | Upload at 40 Mbps |
| --------------------------------------- | --------------- | ----------------- |
| today, 5440×3060 CRF 26 8-bit           | 32.4 GB *meas.* | 1.8 h             |
| today's settings @ 4K CRF 26 8-bit      | 16.3 GB *meas.* | **0.9 h**         |
| proposed @ 4K CRF 23 10-bit             | 27.4 GB *meas.* | 1.5 h             |
| 4K CRF 25 10-bit                        | ~17 GB est.     | ~0.9 h            |
| 4K SVT-AV1 preset 6, equivalent quality | ~14–22 GB est.  | ~0.8–1.2 h        |

Two things worth noticing, both of which argue for running the §7.3 sweep rather than
picking a number from this document:

- **Resolution buys far more than CRF here.** Dropping to 4K at today's CRF halves the file
  (32.4 → 16.3 GB) *and* is 2.26× faster. Going from CRF 26 to 23 then adds most of it back
  (16.3 → 27.4 GB) and costs 1.59× in time.
- **The proposed profile is not a size win over today's output** — 27.4 GB against the
  archived 29.4 GB is roughly a wash. Its wins are speed (1.42×) and per-pixel quality. If
  upload time is the binding constraint, **CRF 25 at 4K is the more interesting operating
  point**, and AV1 more interesting still.

### 7.5 Other profiles

**Both profiles below are deferred to Phase 5, alongside the Apple Silicon work.** Neither
has a compelling reason to exist in v1: `final-av1`'s blocking objection (no local
decode/playback on this Intel/T2 Mac) only resolves on Apple Silicon, and `draft-vt` has no
clear use case ahead of that either. The reasoning is kept here because it's still the right
reasoning when Phase 5 revisits them — it just isn't built yet.

- **`final-av1`** — a rare three-way win: ~20 % smaller than HEVC at equal quality,
  measured **1.55× faster** than x265 medium, and independent 2026 testing found AV1
  uploads score best against YouTube's own encoder. The usual objection (software AV1 is
  far slower than H.264) does not apply, because the comparison here is x265 medium.

  ```
  -c:v libsvtav1 -preset 6 -crf 30 -pix_fmt yuv420p10le -g {4*fps}
  -svtav1-params keyint={4*fps}:scd=0 -tag:v av01
  ```

  The real cost is **local playback**: this Intel Mac has no AV1 hardware decode and
  QuickTime cannot play AV1, which breaks the review habit. So `final-h265` stays the
  default today. **On Apple Silicon (M3 and later have AV1 hardware decode) the objection
  disappears and this becomes the recommended upload profile.**
  **Chunked AV1 is different** — raw AV1 OBUs do not concatenate like HEVC Annex-B, so it
  needs `.mkv` chunks plus the concat demuxer and its own test. Defer.
- **`draft-vt`** — `hevc_videotoolbox -b:v 45000k -maxrate 60000k -bufsize 90000k`.
  A full game in roughly 1.2 h on this Mac against 4–5 h. Docs must say plainly that it is
  a draft: no constant-quality mode on Intel/T2, no rate-distortion optimization, no
  adaptive quantization. Rule of thumb is it needs 2–2.5× the bitrate of x265 medium for
  equal perceptual quality — *a widely reported estimate, not measured here.*
- **`preview`** — `hevc_videotoolbox` on macOS when available, else
  `libx265 -preset veryfast`.

### 7.6 `-hwaccel videotoolbox` for decode: not worth building

The entire decode + scale + xstack stage measured **16.3 core-seconds against the x265
encode's 659, and 205 against 2332 on synthetic content** — under 10 % of the pipeline
either way. Eliminating it completely would move nothing. A
direct test also showed hardware decode *slower in wall clock* (18.3 s vs 10.9 s) when
contending with a VideoToolbox encode for the same T2 block, and `-hwaccel` without
`-hwaccel_output_format` forces a `hwdownload` before the filter graph anyway. Note it in
the docs; revisit only on Apple Silicon.

### 7.7 `--field-cutoff`: stop paying for sky and stands

Built in Phase 4 from a real observation: `aq-mode`/`psy-rd` (§7.2) spend bits on whatever
looks locally complex, which shiny turf glare and distant background supply as readily as a
player does. A blue-turf field (2026-04-19) measured 3.86 bits/pixel/s against a green-turf
field's (2026-04-26) 1.77 at identical encoder settings — over 2× — strongly suggesting a
specular-reflectivity difference in that turf product, though hue itself vs. reflectivity
wasn't isolated by controlled comparison.

True region-of-interest encoding (track the ball/players, feed x265 a per-frame QP-offset
map) was considered and scoped down: the cameras are tripod-fixed for the whole game, so the
field/sky boundary sits at a **constant height** the entire recording — a single per-camera
pixel cutoff, not a tracked region, covers the actual observed case (sky, stands, distant
parking lot) without needing any detection machinery. A trapezoid/arbitrary-polygon mask was
also considered (cameras are elevated and angled down, so the true field boundary isn't a
rectangle) and rejected for v1: expressible via a `geq` point-in-polygon pixel expression
without new dependencies, but fiddly and hard to verify short of rendering and eyeballing
where it lands, for a case (irregular boundary encroaching from the sides) that hasn't
actually come up. A plain horizontal split covers what's actually been observed.

**Mechanism**: `split` the camera's decoded frame into two copies; `crop` + `gblur` the
strip above the cutoff; `crop` the strip below unchanged; `vstack` them back together before
the existing scale/pad chain. Native-resolution pixel coordinates (matching what a still
frame from `csm probe --frame` shows), applied before any scaling to tile size. Lowering the
blurred strip's local complexity is what makes `aq-mode`/`psy-rd` spend fewer bits there —
no encoder feature beyond what `final-h265` already uses.

Not persisted by itself; see §12.1's `GAMEDIR/csm.yaml` — field size, tripod height, and
framing all vary game to game, so there's no sensible tool-wide default.

---

## 8. Apple Silicon

Treat the host as a capability set so the upgrade is a config change, not a rewrite.
`csm/host.py` returns a `HostProfile` — `arch`, `perf_cores`, `total_cores`, `ram_gb`,
`has_videotoolbox`, `videotoolbox_cq` (**probed, not assumed from arch**), `has_libsvtav1`,
`has_libvmaf` — and defaults derive from it.

What actually changes:

- **`hevc_videotoolbox` gains constant quality (`-q:v`).** Verified absent on this Intel/T2
  machine. ABR-only becomes CQ-capable, so the hardware encoder stops being draft-only.
  Still not x265's equal at a given file size — no comparable rate-distortion optimization —
  but the gap narrows to "acceptable fast master" rather than "visibly worse."
- **Media engines are much faster**, and Pro/Max/Ultra parts have one or two. A full-length
  hardware render plausibly drops from ~1.2 h to 20–40 min.
- **AV1 hardware decode on M3 and later** removes the local-playback objection to
  `final-av1`.
- **x265 and SVT-AV1 both ship NEON assembly**; if §13.5's chunked design gets built, it
  carries over unchanged, with performance-core count driving `jobs`.

**Strategically, a modern Apple Silicon Mac may make the DigitalOcean droplet
unnecessary — but the bar is higher than it first appears.** Measured (§3.1), this Xeon
needs **~11 hours** for a full game with the proposed profile at 4K, and ~15.8 h at
today's accidental 5440×3060. Moving to 4K is a measured 2.26×, but the quality profile
spends half of that back. For local rendering to beat the droplet round trip end to end
(§14.4) a new Mac needs to be roughly **3–4× this machine**, which an M4 Pro/Max plausibly
is but should not be assumed. Re-benchmark with `csm self-test --bench` on day one and
re-run the §14.4 comparison with real numbers before retiring anything.

---

## 9. Speed

### 9.1 Chunked parallel encoding — deferred

Splitting a render into time-sliced chunks encoded by independent ffmpeg processes was
designed in detail, then deferred: measured speedup is marginal-to-negative depending on
configuration (§3.2 — 1.24× best case, 0.80× worst), and its real value (resumability on a
multi-hour billed droplet render) is speculative until a render actually gets killed by a
dropped connection. See §13.5 for the full design, the reasoning, and the trigger for
building it.

**Single-process rendering is the only path in v1.** `plan.py` always emits one `FfmpegJob`
covering the whole timeline; there is no `--jobs`/`--chunk-seconds`/`--resume` surface until
§13.5 gets built. The reliable speed change is the **4K output resolution (2.26× measured,
§3.1)**, not chunking.

### 9.2 `--preview`

Deliberately **not** a special case: a preview is a `RenderPlan` with exactly one chunk
whose window is `[preview_t0, preview_t0 + preview_seconds]`, muxed straight to mp4 with
audio. Same code path, so a preview that looks right guarantees the full render is built
the same way.

**Revised in Phase 3, from what real footage found**: input-side `-ss` on the concat
demuxer turned out to be unreliable whenever the source has audio (§5.4's own note) — real
camera clips always do — so "efficiency comes entirely from input seek" (this section's
original claim, based on a plain-file measurement of 0.17s) doesn't hold for a preview
window positioned deep into a long game. The actual mechanism is bounded, not free: whole
leading segment *files* are pruned from the concat list first (free — no seeking, just
fewer `-i` entries), so the remaining in-graph `trim`/`atrim` only ever has to decode-and-
discard at most one segment's worth (§5.4), not the camera's full history. A window near the
start of a long game is still fast; one deep into it can mean a real, if bounded, minute-
scale decode cost before encode even starts (Sec 3.4 `docs/REFERENCE.md`'s note on window
position and speed).

Default `--preview-at middle`, plus an automatic second window at the first moment all four
cameras are live, where a sync error is most visible. Expected wall time: ~43 s with
`hevc_videotoolbox`, 60–90 s with `libx265 -preset veryfast`, **plus** the decode-discard
cost above if the window sits far from the timeline's start.

No quadrant marking needed: the cameras already burn in a name and date overlay in
hardware, and since the four cameras point in four different directions at the field, a
misplaced or wrong-angle quadrant is obvious on sight. `drawtext`/`drawbox` overlays were
considered and dropped as unnecessary.

This converts "wait hours to discover the sync is a frame off" into "check in 45 seconds,"
and is worth more than any encoder tuning.

---

## 10. Robustness and tests

### 10.1 Structural

- `subprocess.run(argv, shell=False)` everywhere. `shlex.join` renders `--dry-run` output
  **only** — never builds a command that is executed. This kills every quoting bug at once;
  today a filename with a space or a metadata value containing a quote produces a broken
  script.
- `ffmpeg.run()` captures stderr; on non-zero exit it raises `FfmpegError` carrying the
  exit code, the last 40 stderr lines, and `shlex.join(argv)` — so when it breaks you get
  the exact failing command back to paste into a terminal.
- `ffmpeg.check_capabilities()` at startup: version gate (≥ 7.0), `libx265` present, and
  probe the optional pieces (`libsvtav1`, `libvmaf`, `hevc_videotoolbox`, and
  whether VideoToolbox accepts `-q:v`) so features degrade with a message instead of a
  filtergraph parse error 40 minutes into a render. This is what catches "the droplet's
  ffmpeg is different from mine."
- **`probe.py` measures everything the filtergraph assumes**, per file: size,
  `r_frame_rate`, `duration`, `nb_frames`, `codec_name`, `pix_fmt`, color range and tags,
  `sample_aspect_ratio`, rotation side data, audio `channels`/`sample_rate`,
  `creation_time`. Validations with specific messages:
  - all segments of a camera agree on size/fps/codec — name the offending file
  - fps within tolerance of the timeline fps
  - every camera in `audio_cams` actually has an audio stream
  - `nb_frames/fps` vs container `duration` per segment — the frame-drop detector, kept as
    a diagnostic even though the Brave 7 lacks the LE's rollover bug, because it is how a
    future camera problem would first show up
  - video-vs-audio duration delta per segment — the concat-drift canary
  - segment filename order cross-checked against `creation_time` monotonicity
  - free disk ≥ 2× estimated output
- The filtergraph exists in exactly one place, is a pure function of `Timeline` +
  `EncoderProfile`, and is unit-tested as a string.
- `metadata.yaml` format and semantics preserved, with one fix: today a single non-string
  value causes *all* metadata to be silently dropped. Coerce with `str(v)` and warn once.

### 10.2 Test suite

`tests/conftest.py` provides `synthetic_game(tmp_path, offsets, durations, missing=(),
sizes=None, segments=1, creation_times=None, fps=30)` building real `cameraN/*.mp4` with
lavfi: a shared deterministic impulse track
(`aevalsrc='0.9*sin(2*PI*1000*t)*exp(-40*mod(t,1.7))'` — reproducible, unlike `anoisesrc`,
which has no `seed` option in this build) shifted per camera and mixed with independent
noise, plus `testsrc2` video, which carries a built-in moving timestamp so eyeball checks
work. 640×360 at `-preset ultrafast` for speed; 1920×1080 for the shape tests.

| #   | Test                                      | Asserts                                                                                                                                                                                          |
| --- | ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 1   | `gcc_phat_known_integer_delay`            | pure numpy; `np.roll` by N → recovered lag == N ±1 sample                                                                                                                                        |
| 2   | `gcc_phat_fractional_delay`               | FFT phase-ramp delay → recovered within 0.2 samples                                                                                                                                              |
| 3   | `gcc_phat_reports_low_confidence`         | hopeless SNR → `psr` below threshold; it *reports* failure rather than returning a plausible wrong number                                                                                        |
| 4   | `sync_detects_injected_offsets`           | offsets (0, 1.6, 701.4, 698.9), mirroring the real game → every solved offset within **1/60 s**                                                                                                  |
| 5   | `sync_creation_time_prior_narrows_search` | with tags present, search window ≤ ±5 s and the result matches the blind search to within a frame — the hint changes cost, not the answer                                                        |
| 6   | `sync_unsynced_camera_clock`              | **forgotten-camera case**: cam 3's tag wrong by 900 s → that link silently escalates to blind search, solved offset still within 1/60 s, `sync.yaml` records `method: blind`, report names cam 3 |
| 7   | `sync_cross_tripod_bias_averages_out`     | cam 3/4 windows with delays varying ±180 ms → estimate within 35 ms of the mean                                                                                                                  |
| 8   | `sync_disconnected_graph`                 | cam 3 audio-less, no override → error names cam 3 and the flag to fix it                                                                                                                         |
| 9   | `sync_overrides`                          | `--offset 3=2.5` beats the file; `--nudge 3=+0.033` adds to auto                                                                                                                                 |
| 10  | `timeline_fragments`                      | pure unit: head>0, head<0, tail>0, absent → exact expected filter strings                                                                                                                        |
| 11  | `render_output_shape`                     | 6 s game → 3840×2160, 180 frames, 1 video + 1 audio, 2 ch, 48 kHz, `hevc`, duration within a frame                                                                                               |
| 12  | `render_color_range`                      | `yuvj420p` full-range input → output tagged bt709/tv with all three tags set, and a known grey patch lands at the expected limited-range code value                                              |
| 13  | `render_missing_camera`                   | camera 3 absent → renders; `crop=1920:1080:0:1080,blackframe` reports 100 % black                                                                                                                |
| 14  | `render_short_camera`                     | cam 2 is 3 s of a 6 s timeline → output is 6 s; top-right black at t=4, not black at t=1                                                                                                         |
| 15  | `render_mixed_resolution`                 | cam 4 at 2720×1530, others 1080p → correct shape, quadrant not letterboxed                                                                                                                       |
| 16  | `render_out_size_arbitrary`               | `--out-size 2560x1440` → output 2560×1440, tiles 1280×720                                                                                                                                        |
| 17  | `render_native_size`                      | `--out-size native` with 2720×1530 sources → 5440×3060, no scale emitted                                                                                                                         |
| 18  | `scale_skipped_when_source_matches_tile`  | 1080p sources at 4K out-size → no `scale` in the graph; 4K sources → a 2× downscale is emitted                                                                                                   |
| 19  | `non_16x9_source_letterboxed`             | 4:3 source → padded, not stretched; a known edge pixel is black                                                                                                                                  |
| 20  | `fps_derived_from_source`                 | 25 fps fixture, no `--fps` → 30 fps output by default, `--fps 25` gives 25; `--offset-frames` converts at the output rate                                                                        |
| 21  | `gop_scales_with_fps`                     | `--fps 60` → `keyint=120` in the emitted params, and chunk boundaries stay GOP multiples                                                                                                         |
| 22  | `chunked_matches_single` *(deferred with §13.5)* | `--jobs 1` vs `--jobs 3 --chunk-seconds 2` → identical frame count and duration, `psnr` > 40 dB. Written when chunking is, not before.                                                  |
| 23  | `preview_window`                          | `--preview --preview-at 50%` → duration matches; PSNR vs the same slice of a full render > 35 dB                                                                                                 |
| 24  | `dry_run_is_valid_shell`                  | output passes `bash -n`; fixture directory literally named `game dir with 'quote' and "dquote"`; every path round-trips                                                                          |
| 25  | `metadata_roundtrip`                      | quotes, ampersands, unicode → ffprobe reads back identical values                                                                                                                                |
| 26  | `probe_rejects_mismatched_fps`            | one segment at a different rate → error naming that file                                                                                                                                         |
| 27  | `concat_segments`                         | 3 segments per camera → correct total duration, sync unaffected                                                                                                                                  |

Tests 1–3, 8–10, 24 are pure Python and run in **under 2 seconds total**. Targets:
`pytest -m "not slow"` **under 5 seconds** — fast enough to run on every save, which is the
actual goal — and the full suite **under 2 minutes**.

Plus a `csm self-test` subcommand that runs the integration fixtures in a temp directory,
so a droplet or a new Mac can be validated in a minute before committing billed hours.

---

## 11. Portability

Supporting other people on Mac or Linux is the lowest-priority goal, but it constrains real
choices:

- **Stock ffmpeg only.** Everything must work with `brew install ffmpeg` or
  `apt install ffmpeg`. This rules out *depending* on `drawtext` (still no freetype in
  Homebrew core as of 9.0.1), `libfdk_aac` (already abandoned for this reason), or
  `libvmaf` (present in Homebrew 9.0.1, not guaranteed on Linux). All three are used **when
  present** and degrade with a message when not.
- **No ffmpeg source builds anywhere**, including the droplet.
- **Pure-wheel Python dependencies.** numpy, scipy and PyYAML all ship manylinux and macOS
  wheels, so `pip install .` never compiles. Recommend `pipx install .` so users get a
  `csm` on PATH without thinking about virtualenvs; document the venv alternative.
  **numpy and scipy are not currently installed** on this Mac — this is README step 1.
- **Version gate at ffmpeg 7.0**, with actionable messages naming the missing piece and the
  install command.
- **README rewrite**: copy-pasteable prerequisites per platform, the workflow end to end,
  corrected size expectations (**~76 GB input per game, not 20 GB**), and the recorded
  encoder decisions with their measured scores. Fix the broken link — the README points at
  `DigitalOceanVideo/` while the directory is `DigitialOcean/` (itself misspelled; renaming
  is a separate optional cleanup).
- **Restructure the docs so camera knowledge is quarantined.** `README.md` describes the
  software with no camera assumptions; `cameras.md` and `soccer.md` become explicitly
  labelled "observations for specific hardware, may be stale." A new user with a different
  camera brand must not have to read around Akaso-specific advice to get started.

---

## 12. CLI

```
csm probe   GAMEDIR [--json] [--audio] [--frame] [--frame-at SEC]
csm sync    GAMEDIR [--write] [sync options]
csm render  GAMEDIR [render options]
csm preview GAMEDIR [preview options]
csm self-test [--bench]

Global    -v/--verbose  -q/--quiet  --ffmpeg PATH  --ffprobe PATH
Sync      --sync-file PATH  --no-time-prior
          --prior-window 5  --prior-tolerance 5
          --offset N=SEC  --offset-frames N=F  --nudge N=±SEC
          --sync-windows 24  --sync-window-len 20
          --max-offset 1800  --min-confidence 6.0
          --clap-at MM:SS
Timeline  --align-start {latest,earliest}  --align-end {longest,shortest}
          --start T --end T  (T = SEC|MM:SS|CAM@SEC|CAM@MM:SS)  --duration D
          --fps 30  --audio-cams 2,4
          --out-size 3840x2160|native|WxH
          --field-cutoff N=PIXELS (repeatable, §7.7)
Encode    --profile {final-h265,preview-videotoolbox,preview-x265}
          --crf N --preset NAME  (final-h265 only; §12.1 on persistence)
Run       --output PATH  --work-dir PATH  --dry-run
Preview   --preview-seconds 60  --preview-at middle|MM:SS|PCT (repeatable)
          --preview-encoder auto|NAME
          --sweep KEY=v1,v2,... (repeatable: crf, preset, encoder, psy-rd, aq-strength)
          --upload-mbps N  (§7.4; default 40, not a safe assumption for anyone else's connection)
```

v1 has no `--jobs`/`--threads-per-job`/`--chunk-seconds`/`--no-chunks`/`--resume`,
`--cross-method`, or `--no-clap`, and `--profile` has no `final-av1`/`draft-vt`,
`--encoder`/`--bitrate`/`--x265-param`/`--scaler` (a generic escape hatch onto arbitrary
x265 params was considered and deferred — `--sweep psy-rd=...`/`aq-strength=...` covers the
concrete case that came up in practice). These return if §13.3–§13.5, Phase 5, or real use
motivate them.

### 12.1 `GAMEDIR/csm.yaml` — persisted render settings

Built in Phase 4, once `--field-cutoff` made "retype four numbers on every command against
this game" a real annoyance. Same override layering as `sync.yaml`'s offsets: the file
provides the default, an explicit CLI flag on any given command still wins for that one run.

```yaml
field_cutoff: {1: 880, 2: 910, 3: 800, 4: 845}
crf: 25
preset: fast
```

`field_cutoff` applies to both `render` and `preview` (it changes the actual composite,
same in both). `crf`/`preset` apply to `render`'s `final-h265` profile and become
`--sweep`'s baseline for any dimension not explicitly swept over — not to plain `preview`'s
fast preview-encoder path, which is deliberately fixed/quick and unrelated to final-quality
tuning. Written by hand today; no `csm ... --write-defaults` yet.

`--dry-run` emits a `set -euo pipefail` script generated with `shlex.join(argv)` — so the
printed script is *provably* the same command, not a re-derivation. This preserves the
inspect-before-run habit exactly, including the hand-edit-and-rerun pattern seen in
`2026-04-26/runme.sh`.

---

## 13. TODOs — recorded, not built

### 13.1 Dedicated audio recorders

Current audio is two cheap mono action-cam mics, chosen mainly so a whistle is audible. A
separate recorder would be a large quality jump, and a recorder worn by a coach or a player
is a genuinely interesting idea.

- Accept audio-only inputs alongside the cameras: `--audio-track PATH[:label]`, any format
  ffmpeg can decode.
- **Sync comes free from the existing machinery.** A recorder near the field correlates
  against camera audio exactly as another camera does — it becomes one more node in the
  same weighted-least-squares solve, with the same GCC-PHAT measurement and confidence
  reporting. A *body-worn* recorder is the awkward case: its acoustic delay to each tripod
  varies continuously as the wearer moves, so the windowed-median approach matters more
  there, and a midfield clap (§6.7) would help significantly.
- Mixing policy needs a decision that does not exist yet: replace camera audio entirely, or
  mix (`amix` with weights, or the recorder centered with camera audio as ambience). Worth
  an `--audio-mix` policy flag when built.
- Consumer recorders are often 48 kHz nominal but not crystal-accurate, and run for hours.
  The drift machinery deliberately skipped for cameras would likely be needed here — a good
  reason to have kept the piecewise-per-chunk approach documented in §6.5 rather than
  forgotten.
- **Privacy matters** for a recorder worn by a coach or a minor. Whatever gets built should
  make clear in the docs that everyone involved should know they are being recorded.

### 13.2 Cameras with frame-accurate time sync

GoPro Hero 12/13 support a timecode sync feature; other cameras support genlock or external
timecode. When present it is frame-exact and would make audio correlation unnecessary for
the primary path.

- **Detection.** GoPro writes a `tmcd` data track. `ffprobe -v error -select_streams d
  -show_entries stream=codec_tag_string -show_entries stream_tags=timecode` should surface
  it; `format_tags=timecode` is the other place to look. **Unverified — no GoPro footage
  available.** Confirm against a real file before writing any code.
- **Interpretation.** Timecode is `HH:MM:SS:FF` at a stated rate, with drop-frame semantics
  at 29.97. For 30 fps non-drop, `t = (((HH*60 + MM)*60 + SS)*fps + FF) / fps`. Handle
  rollover past midnight, and reject a timecode track whose rate disagrees with the video
  stream.
- **Integration.** Slot in as `TimecodeSource` at the top of the ranking in §6.1. When every
  camera has usable timecode, skip correlation and note `method: timecode` in `sync.yaml`.
  When only some do, use it for those and audio for the rest — the weighted solve already
  handles mixed sigmas, so a timecode link is simply a measurement with a very small sigma.
- **Verify anyway.** Even with timecode, run one audio correlation and report disagreement.
  Timecode sync can be misconfigured, and silently trusting it would reintroduce exactly the
  failure mode this design exists to avoid.

The reason this is cheap to defer: the source-ranking abstraction is the only thing that
needs to exist now, and it costs nothing.

### 13.3 Envelope cross-method fallback

`--cross-method envelope`: frame at 10 ms hop, take log-RMS, mean-subtract, and correlate
envelopes — coarser than PHAT but more robust when the two tripods hear genuinely different
spectra. v1 ships PHAT only (§6.4).

- **Trigger to build:** a real game where cross-tripod PHAT correlation comes back low
  confidence or wrong, and retrying with more/longer windows doesn't fix it.
- **Why cheap to defer:** the `--cross-method` flag and the source-ranking/confidence
  machinery around it already accommodate a second method; adding `envelope` is a new
  correlation function, not a redesign.

### 13.4 Automatic clap-onset detection

Auto-detect the sharpest broadband onset common to all four cameras within the first few
minutes after the last camera starts, use it as the cross-tripod measurement when
confidence is high, and fall back to windowed correlation silently. v1 ships manual
`--clap-at MM:SS` only (§6.7).

- **Trigger to build:** the manual workflow (watch the preview, find the clap, type the
  timestamp) proves annoying enough in practice to be worth automating.
- **Why cheap to defer:** `kind: clap` vs `kind: cross-tripod` measurement tagging already
  exists in the design (§6.7); automatic detection only needs to produce the same
  `--clap-at`-equivalent value earlier in the pipeline.

### 13.5 Chunked parallel encoding and resume

A chunk is a time slice of the same filtergraph encoded by an independent ffmpeg process.
Deferred from §9.1 — full design kept here so it's ready to build without re-deriving it.

**Read §3.2 first.** On real footage the best configuration was 1.24× faster and the worst
was 20 % *slower* than not chunking at all — a 1.55× spread between two plausible-looking
settings. Chunking is a tuning parameter, not a free multiplier, and its value is entirely
conditional on getting jobs and pools right for the specific machine.

- **Boundaries** in integer frames, always a multiple of the GOP. `--chunk-seconds 120` →
  3600 frames at 30 fps; a 78-minute game is ~39 chunks.
- **Per-chunk command:** identical filtergraph, each track's `-ss` recomputed as
  `track_source_time(t0 + chunk_start)`, `-t {chunk_len + 2}` on each input, head/tail pads
  recomputed for the chunk window. Output **raw Annex-B, no container**:
  `-f hevc .csm/chunks/chunk-%03d.hevc`. `repeat-headers=1` guarantees VPS/SPS/PPS at each
  chunk's leading IDR.
- **Audio in one job**, parallel with the chunks — eliminates per-chunk A/V drift.
- **Assemble** — one `-c copy` job muxing the concatenated chunks with the audio track.
- **Sizing — measure, do not derive.** `jobs = perf_cores // 2` picks 4 jobs on the
  benchmark Mac, which measured 20 % *slower* than a single process, while 2 jobs measured
  1.24× faster — a heuristic that inverts the right answer on the only machine tested.
  `csm self-test --bench` should sweep `jobs ∈ {1, 2, 4, 8}` (capped by cores and RAM)
  against `pools = total_cores // jobs` and record the fastest. `--jobs 1` (no chunking)
  must stay a first-class, well-tested path, since it beats a badly-configured chunked run
  on this hardware. Memory budget from measurement, not assumption: single-process x265 4K
  10-bit peaked at 5.96 GB, SVT-AV1 at 9.5 GB — the per-worker budget must be probed per
  encoder.
- **Resume.** `.csm/manifest.json` hashes (sync.yaml, profile, timeline, chunk boundaries,
  tool version); matching non-empty chunks are skipped. **This is the actual argument for
  building this at all** — for a multi-hour render killed by a dropped SSH session on a
  billed droplet, resumability is worth more than the uncertain speedup, and it is a
  robustness feature rather than a performance one.
- **Disk.** Chunks total roughly the final video; budget 2× output.

**Trigger to build:** a droplet run is actually killed mid-render by a dropped connection or
similar, or encode time becomes a real bottleneck once §14's droplet economics (whole-hour
billing, §14.3) are being felt in practice. Ship test 22 (`chunked_matches_single`, §10.2)
first, before the feature, once building it — that's what makes chunking safe to touch.

**Why cheap to defer:** none of Phases 1–4 depend on it. `plan.py` already separates "what
jobs to run" from "how to execute them," so adding a chunked planner later is additive, not
a rewrite.

### 13.6 `aq-strength` below 0.3, and making it a real option

Phase 4's real `--sweep aq-strength=...` run against the worst-case-turf game (2026-04-19)
found a genuine, favorable, roughly linear tradeoff, unlike `psy-rd` (flat, no real effect
in the same test): 1.0→0.3 shrank the file ~13% for a 0.26 VMAF point cost, with no sign of
a cliff by 0.3. `aq-strength` moved to `0.3` as the new default (§7.2) on that evidence, but
only four points were measured (1.0/0.8/0.5/0.3), all at CRF 23.

- **Test values below 0.3.** The trend held all the way down to the lowest value tried; where
  it actually stops being a good trade is unmeasured. Worth another `--sweep aq-strength=...`
  pass before assuming 0.3 is the true optimum rather than just "as far as we happened to
  test."
- **Make it a real CLI/`csm.yaml` option**, the same way `--crf`/`--preset` already are
  (§12.1) rather than a fixed value in `_final_h265_args`. `psy_rd`/`aq_strength` are already
  threaded through `sweep_args` for `--sweep`; the remaining work is the same pattern already
  built for crf/preset — thread them through `final_h265_profile()`, add `--aq-strength`/
  `--psy-rd` flags, and let `GAMEDIR/csm.yaml` persist them.
- **Trigger to build the option:** as soon as a specific game's footage seems to want a
  different value than the global default — e.g. unusually uniform (low-motion, evenly-lit)
  content where a still-lower value might win more, or content where 0.3 turns out to be
  already too aggressive.

---

## 14. DigitalOcean

### 14.1 Script changes

**`droplet-step2-install-software.sh` — delete the ffmpeg source build.** It exists solely
for `--enable-libfdk-aac`, and commit `71d1585` ("Switch to libaac") removed the only reason
for it. It costs 20–40 minutes of billed time per game and is the main thing making the
project hard for others to adopt:

```bash
apt-get install -y ffmpeg python3-numpy python3-scipy python3-yaml python3-venv s3cmd git
```

Ubuntu 25.04 ships ffmpeg 7.1.x. If the version gate fails, fall back to a BtbN static build
(~100 MB, ~30 s) rather than a 40-minute compile.

**`droplet-step4-process.video.sh`** — drop the six positional frame integers and the
`generate > tmpfile; bash tmpfile` dance:

```bash
csm probe  "$GAMEDIR"
csm sync   "$GAMEDIR" --write "$@"
csm render "$GAMEDIR" --dry-run "$@" | tee "$GAMEDIR/.csm/render.sh"   # audit trail
csm render "$GAMEDIR" --profile final-h265 "$@"
```

**Pre-existing bugs to fix.** `droplet-destroy.sh` is missing a line continuation *and*
passes a droplet name where the API requires a numeric ID, so it has probably never worked —
consistent with the README telling you to verify destruction in the UI. `droplet-create.sh`'s
`jq` reads `.droplets[]` (list-endpoint shape) from a single-droplet endpoint that returns
`.droplet`, and its fixed `sleep 15` is frequently too short. The hardcoded bucket
`loudoun-25-26-2012g-silver` appears in steps 3 and 5. Keep the locally modified spec
(containing a real SSH key ID) uncommitted — add `droplet-spec.local.json` to `.gitignore`
and have `droplet-create.sh` prefer it.

### 14.2 Full automation: stand-up → run → tear-down

Hourly billing makes this matter: **every minute a human is not at the keyboard is billed
anyway.** Today the flow is six interactive steps with an SSH reconnect after a reboot in
the middle, so wall-clock time includes however long it takes someone to notice each step
finished. The goal is one command that returns when the video is in Spaces and the droplet
is gone.

```bash
./droplet-run.sh GAMEDIR    # create → provision → fetch → encode → upload → destroy
```

**v1 scope:** the orchestrator script and the tear-down safety below, built on top of the
§14.1 bug fixes, using the existing from-scratch `apt-get install` provisioning (already
sped up by deleting the ffmpeg source build). **Deferred:** pre-baked snapshot and
cloud-init/`user_data` provisioning — genuine wins, but a second axis of infrastructure on
top of getting the orchestrator and its safety net right first. Revisit either if
provisioning time is still the dominant cost once the orchestrator is in real use.

Time savings, in order of value:

- **Pre-bake a snapshot** *(deferred)*. Build a DO custom image once with ffmpeg, `csm` and
  `s3cmd` installed, and create every droplet from it. This removes provisioning from
  *every* run rather than optimizing it. A ~5 GB snapshot costs about $0.30/month — trivial
  against one wasted billed hour. Largest single automation win, and the first thing to
  build if provisioning time turns out to matter in practice.
- **`user_data` / cloud-init instead of steps 1–2** *(deferred)*, when not using a snapshot,
  so provisioning runs during boot rather than after an SSH login.
- **Drop the `apt upgrade` and reboot.** Several billed minutes to update a disposable box.
- **Start the Spaces download at boot**, concurrent with remaining installs.
- **Replace the fixed `sleep 15`** with polling until status is `active` *and* network
  addresses are assigned.

Tear-down needs more care, because the expensive failure is the opposite one:

- **A droplet that is never destroyed costs ~$950/month at $1.30/hr.** Treat runaway billing,
  not a lost render, as the primary risk.
- **Powering off does not stop DigitalOcean billing — only destroying does.** Any failsafe
  must call the API to delete, not merely `shutdown -h`.
- **Three independent layers:** the droplet deletes itself on completion (success *or*
  failure); the local orchestrator destroys it after a timeout; a hard deadline armed at
  creation deletes it regardless. Any one failing must not leave a box running.
- **Ship logs to Spaces before and after each risky step** (provision, fetch, encode,
  upload), not only at the end. A self-destructing droplet takes its logs with it, so an
  unattended failure is undiagnosable unless the log is already elsewhere. This is what
  makes unattended operation safe to trust — it doesn't need to be a continuous stream, just
  never lost with the box.
- **Report elapsed wall time and billed hours** at the end of every run, so droplet-size
  decisions stay evidence-based.

Given the runaway-billing risk, **fix and test `droplet-destroy.sh` first**, before building
any automation on top of it.

### 14.3 Sizing: optimize billed hours, not minutes

DigitalOcean bills in **whole-hour increments**. Going from 1h50m to 1h10m saves nothing —
both bill two hours. **The only speedups worth paying for are ones that cross an hour
boundary.** Hourly rates (monthly ÷ 672):

| Type                | 16 core | 32 core | 48 core | 60 core |
| ------------------- | ------- | ------- | ------- | ------- |
| CPU-Optimized, SSD  | $0.50   | $1.00   | $1.50   | —       |
| Premium Intel, NVMe | $0.65   | $1.30   | $1.95   | $2.44   |

Doubling cores doubles the hourly rate, so a bigger droplet pays **only** when it moves the
job into a lower billed-hour bucket. If 16 cores finishes in 1h50m (2 h billed, $1.30
Premium) and 32 cores in 1h05m (still 2 h billed, $2.60), the bigger box costs twice as much
for the same bill.

**Total droplet time is download + encode + upload**, not encode alone. Getting a 65-minute
total under the line may be better achieved by overlapping the Spaces download with setup
than by buying cores.

The prior guidance ("32 cores is the sweet spot") was formed at 5440×3060. **Moving to 4K
halves the work**, so a one-hour total is now plausible and the right droplet may be
*smaller* than before.

#### The minimal benchmark set

Do **not** benchmark all seven variants. Three cheap measurements answer the question:

1. **Ubuntu 25.04 stock ffmpeg capability check** — version, and whether `libx265` and
   `libsvtav1` are present. Decides whether the from-source build can be deleted. A
   **two-minute check** on the cheapest droplet; do it before touching any droplet script.
   *(Could not be confirmed remotely — the packages.ubuntu.com lookup failed.)*
2. **`csm self-test --bench` on Premium Intel 16-core and 32-core.** Same synthetic
   20-second 4K workload used on the Mac, so numbers are directly comparable. Two minutes
   per box — spin up, bench, destroy. Gives the core-scaling factor and finally *measures*
   the 25–27 core ceiling instead of assuming it. In v1 this is single-process wall time
   only — the jobs/pools sizing sweep described under §13.5 applies once chunking exists.
3. **One full real-game run** on whichever size lands closest to the hour boundary, timing
   **download, encode and upload separately.** The phase breakdown says whether to spend on
   cores or on data movement.

Total cost roughly $5, since each short-lived droplet bills one hour.

**Skip entirely:** the 48- and 60-core boxes (poor marginal return against a doubling hourly
rate), and the SSD-vs-NVMe comparison unless step 3 shows the job is I/O bound. The workload
is overwhelmingly encoder-bound — decode plus tiling is 2.4 % of pipeline cost — so NVMe's
30 % premium is unlikely to pay for itself. Note that "Premium Intel" also means a newer CPU
generation, not just faster disk, which is the more plausible reason it helped at 5.4K; step
2 will confirm or overturn that at 4K.

### 14.4 Data movement: the raw upload dominates everything

The current workflow uploads raw footage from home to a Space (which doubles as team
sharing), the droplet pulls it to local SSD, encodes, and pushes the final video back to
Spaces and optionally YouTube.

At **~76 GB of raw footage over a 40 Mbps home connection, that upload alone is ~4.2
hours.** (76 GB is measured: `du` across all four camera directories of 2026-05-03, the
game with no deleted files.) Using the measured encode times from §3.1, and a 27.4 GB
master at 4K CRF 23:

| Path                  | Home upload       | Encode            | Final upload  | Wall total  | DO cost |
| --------------------- | ----------------- | ----------------- | ------------- | ----------- | ------- |
| Droplet, 4K           | 4.2 h (76 GB raw) | ~1.4 h            | minutes       | **~5.7 h**  | $2–3    |
| Local on this Mac, 4K | —                 | **11.1 h** (§3.1) | 1.5 h (27 GB) | **~12.6 h** | $0      |

**The droplet wins end-to-end by more than 2×, and this reverses an earlier assumption in
this document's own drafting.** An earlier estimate put local encoding at ~2 hours, which
would have made the raw upload the dominant cost and the droplet pointless. The measured
figure is 11.1 hours — five times that — so the 4.2-hour raw upload is *not* the dominant
cost, and the droplet earns its place on throughput rather than merely riding along on a
sharing decision.

Two things follow that would not have followed from the wrong number:

- **Do not retire the droplet on the strength of the 4K change alone.** Halving the pixels
  does not bring an 11-hour local render under the round-trip time.
- **The raw upload is worth attacking on its own terms**, but for its own reasons (it
  blocks the home connection for four hours) rather than because it dominates the pipeline.

Worth considering, none of it blocking:

- **Does the team actually need the raws?** If sharing the finished video covers most
  requests, uploading 76 GB per game to serve a rare need is expensive. But note that
  dropping the raw upload does **not** by itself make local rendering competitive — that
  needs faster hardware, not less transfer.
- **Overlap download with setup.** With the source build deleted, setup drops to about a
  minute, so there is little left to overlap with — but starting the pull immediately on
  boot still shaves real minutes off a billed hour.
- **Read from Spaces directly** via an `rclone` VFS mount instead of copying 76 GB to local
  disk. Chunked encoding is a good fit — each worker reads a different time range, which
  maps well onto HTTP range requests — and it removes the download phase and the local disk
  requirement entirely. Genuinely promising and genuinely risky (seek behavior, cache
  tuning, partial-read failures mid-render); prototype before trusting, and keep copy-first
  as the default.
- **Apple Silicon is the variable that actually decides this.** A new Mac needs to be
  roughly **3–4× this Xeon** — bringing an 11-hour render down to under ~4 hours — before
  local rendering beats the ~5.7 h round trip. At that point the whole Spaces/droplet path
  becomes optional infrastructure existing purely for sharing. An M4 Pro/Max plausibly
  clears that bar, but measure on arrival rather than assuming.

---

## 15. Branch strategy and build order

**All of this is built on a branch. `main` must stay usable for real games throughout.**
The season is starting and the current tool works; nothing should force a switch before the
replacement has been validated on real footage.

- Branch off the latest `origin/main`, e.g. `csm-rewrite`. All phases land there.
- **Phase 0 is the exception and goes straight to `main`** — small fixes to the tool in use
  right now. Rebase the branch onto `main` afterward.
- The branch stays mergeable by keeping `legacy/generate-ffmpeg.py` intact, so `main`'s
  behavior remains reachable after the merge and switching over is reversible.
- Merge only after the branch has produced a full game that has actually been watched and
  compared against the archived masters.

| Phase                                      | Content                                                                                                                                                                                                                                                                         | Tests         |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------- |
| **0** — on `main`, same day — **done** (`b98736b`) | Freeze `legacy/generate-ffmpeg.py`. Fix `scale=1900:1080` → `1920:1080` and the duplicated `-movflags faststart`. Add `.gitignore` for `filelist-*`, `concatinated.mp4`, `combined.mp4`. Document that `-s` must be passed, since 5.4K output is happening on every game today. | —             |
| **1** — headline feature, zero render risk — **done** (`acc8980`) | **Spike first (§6.0):** raw GCC-PHAT against the two archived games, checked against ground truth. Only if that validates, build `csm probe` + `csm sync` (PHAT-only, creation-time hint, weighted-solve topology, manual `--clap-at`) and confirm against the same two games. Writes `sync.yaml`; add an `--offsets-seconds` entry point to the legacy script so it consumes them. Frame-hunting stops immediately; the render path is untouched. If the spike doesn't validate, stop here — manual sync continues and the rest of this phase isn't built. | 1–9           |
| **2** — new pipeline, single process — **done** (`5d60742`) | `timeline`/`filtergraph`/`plan`/`runner`, one ffmpeg job, both intermediate passes deleted, 4K default, color-range fix, missing/short cameras working, `final-h265` only.                                                                                                      | 10–21, 26, 27 |
| **3** — preview and polish — **done** (`6b80188`) | `--preview`, `--sweep` (with VMAF/xpsnr scoring), `--dry-run`. Small, and what makes phase 4 safe to iterate on.                                                                                                                                                                | 23–25         |
| **4** — quality and droplet — **quality half done** (`c5fbf74`) | Run the sweep on real footage, lock CRF/preset for `final-h265`, record in README — done, plus `--field-cutoff`, `GAMEDIR/csm.yaml` persistence, and `--upload-mbps`, none of which were in the original phase scope but came directly out of real footage testing. Droplet: fix §14.1's bugs, build the `droplet-run.sh` orchestrator with three-layer self-destruct safety (no snapshot/cloud-init yet — see §14.2) — **not started**.                                          | —             |
| **5** — Apple Silicon                      | Re-benchmark on arrival, re-tune `host.py`. Reconsider `final-av1`/`draft-vt` (§7.5) now that AV1 gets local playback, and reconsider chunking (§13.5) and droplet snapshot/cloud-init (§14.2) if either is still worth building.                                              | —             |

Chunked encoding (§13.5), the envelope cross-method (§13.3), and automatic clap-detection
(§13.4) are not phases — they're conditional TODOs, built only if real use shows they're
needed.

**Keep from the old script, deliberately:** the `cameraN/` directory convention, the
`metadata.yaml` format and semantics, the `0_0|w0_0|0_h0|w0_h0` xstack layout, cameras 2 and
4 for audio, `-tag:v hvc1`, `+faststart`, and above all the generate-inspect-run habit — now
expressed as `--dry-run` with correct quoting.

---

## 16. Verification

1. **Real-footage sync validation, first — this is the §6.0 gate, not a follow-up check.**
   Run raw GCC-PHAT against `2026-04-26` and `2026-05-03`, comparing against the ground
   truth in §2.5, *before* building `sync.yaml`/CLI/topology-solve machinery around it.
   Needs no rendering, and is the highest-value check available.
2. **Unit and integration suite**: `pytest -m "not slow"` on every save, full suite before
   each commit.
3. **`csm self-test`** on the droplet and on any new machine.
4. **Legacy-vs-new equivalence** (Phase 2): render the same 60-second window through both
   and compare with `ffmpeg -i a -i b -lavfi psnr`. Expect > 40 dB where settings match.
5. **Chunking equivalence** (test 22) — only applicable once §13.5 is built; not part of
   the v1 critical path.
6. **The CRF / preset / encoder decision** (Phase 4): `csm preview --sweep` on a real game;
   record the choice and reasoning in the README. Also compare 4K CRF 23 against the
   archived 5.4K CRF 26 master on a real render.
7. **First real game**: `csm probe` → `csm sync --write` → inspect `sync.yaml` →
   `csm preview` → `csm render --dry-run | less` → `csm render`.

---

## 17. Risks and open questions

1. **Concat-demuxer audio across 7 AAC segments** could accumulate A/V error over 6
   boundaries; untested. Mitigated by the probe diagnostics and the `--precat` fallback.
   Test on an archived game before trusting it.
2. **`-ss` across a concat-demuxer segment boundary** — works from a pipe in testing, not
   verified across a boundary, and both `--preview` and chunking depend on it. Fallback:
   resolve global time → `(segment_index, local_offset)` in Python and build the input as
   that segment with `-ss local_offset` followed by the rest.
3. **Chunk-boundary visible quality steps** — quantified by test 22 rather than assumed.
   Not a live risk in v1 since chunking is deferred (§13.5); applies once it's built.
4. **HEVC Main10 + `hvc1` playback in QuickTime on this T2 Mac** — expected fine, not
   verified. Check with a preview before making 10-bit the default.
5. **A camera whose clock was never synced** makes its `creation_time` hint useless. The
   automatic per-link escalation covers it, but that path is the least exercised one — test
   6 must cover it deliberately, since it will happen in the field.
6. **AV1 chunk concatenation** differs fundamentally from HEVC Annex-B; deferred with its
   own test.
7. **numpy and scipy are not installed** on this Mac; `pipx install .` is README step 1.
8. **Chunked encoding's speedup is configuration-dependent and can be negative** (§3.2) —
   measured spread 1.55× between two reasonable-looking settings, with the obvious
   heuristic picking the losing one on this machine. This is exactly why it's deferred to
   §13.5 rather than built: there's no assumed multiplier to build around yet, and there
   won't be one until `csm self-test --bench` measures a specific host. If §13.5 is ever
   built, keep `--jobs 1` first-class regardless.
9. **The proposed quality profile costs 1.59×** over today's encoder settings at the same
   resolution, eating about half the gain from dropping to 4K, and is not a file-size win
   over today's output. §7.2 is a starting point, not a verdict; the §7.3 sweep settles it.
10. **A full game is ~11 hours on this Mac.** Any plan assuming local rendering is quick —
    including retiring the droplet — must confront that number first (§14.4). An earlier
    draft of this document assumed ~2 hours and drew the opposite conclusion.
11. **GoPro timecode detection is unverified** — no GoPro footage available. Validate
    against a real file before writing code against §13.2.
12. **Ubuntu 25.04's ffmpeg build flags are unconfirmed**; the capability probe is the real
    safeguard, and §14.3 step 1 settles it.
13. **The encoder comparison in §3.3 used synthetic content**, which inflates absolute times
    and distorts CRF-to-size behaviour badly. Only its ratios are usable, and its CRF table
    must not be used for size budgeting.
14. **The whole automatic-sync feature is gated on the §6.0 spike, not assumed.** §2.4's
    creation-time analysis and §6's design are strong reasoning, but reasoning isn't the
    same as GCC-PHAT actually reproducing §2.5's ground truth on real archived audio. If the
    spike doesn't validate closely enough, Phase 1 stops at "manual sync continues" — the
    rewrite still delivers Phase 2's pipeline correctness and speed wins, but the headline
    "easier to use" goal would need a different approach than audio correlation.

---

## Sources

- [YouTube: Recommended upload encoding settings](https://support.google.com/youtube/answer/1722171)
- [Codec comparison: bitrate at equal quality](https://www.forasoft.com/learn/video-quality/articles-vqm/codec-comparison-real-content)
- [H.264 vs H.265 vs AV1 (2026)](https://dominatetools.com/blog/h264-h265-av1-comparison/)
- [Best video codec for YouTube in 2026](https://www.free-codecs.com/guides/selecting_the_optimal_video_codec_for_youtube.htm)
