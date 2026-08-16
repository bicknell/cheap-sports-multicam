# csm

Sync and render tooling for a 4-camera, 2-tripod multicam rig (cameras 1+2 on
one tripod, 3+4 on the other, tiled 2x2 with camera 1 top-left). Works with
any camera brand, resolution, or frame rate — nothing here assumes a
specific model.

**Status: Phase 1.** `csm probe` and `csm sync` are implemented and
validated against real archived games. The render pipeline itself
(`csm render`, `csm preview`) hasn't been rewritten yet — for now, feed
`csm sync`'s output into `legacy/generate-ffmpeg.py` (see below). See
`../docs/DESIGN.md` for the full plan and `SYNCHRONIZATION.md` for how the sync
detection actually works.

## Setup

```
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Needs `ffmpeg`/`ffprobe` >= 7.0 on `PATH` (`brew install ffmpeg` or
`apt install ffmpeg`).

## Directory layout

Point every command at a game directory shaped like this:

```
GAMEDIR/
  camera1/*.mp4   (or .mov, .mts, .m4v)
  camera2/*.mp4
  camera3/*.mp4
  camera4/*.mp4
  metadata.yaml   (optional)
```

A camera can be missing entirely, or have fewer/shorter segments than the
others — both are handled, not treated as errors.

## Usage

**Inspect footage:**

```
csm probe GAMEDIR
csm probe GAMEDIR --audio      # per-camera audio levels (volumedetect)
csm probe GAMEDIR --json
```

Reports resolution, fps, codec, duration, and audio presence per camera, and
flags problems (segments that disagree on size/fps/codec, an audio-cam
missing its audio stream, likely dropped frames, A/V duration drift).

**Detect sync offsets:**

```
csm sync GAMEDIR --write
```

Prints each camera's offset (seconds, relative to camera 1) and writes
`GAMEDIR/sync.yaml`. Useful flags:

- `--clap-at MM:SS` — anchor the cross-tripod measurement on a deliberate
  clap/clapperboard at that timestamp (camera 1's clock), for near-exact
  sync instead of the statistical average. Clap at midfield, after all four
  cameras are rolling — see `SYNCHRONIZATION.md` for why that placement is
  the whole point.
- `--offset N=SEC` — override a camera's offset outright (`sync.yaml` and
  any measurement are ignored for that camera).
- `--nudge N=+-SEC` — add a small correction on top of whatever offset is
  currently in effect.
- `--no-time-prior` — force blind search everywhere, ignoring `creation_time`
  tags (mostly useful for testing).
- `--min-confidence`, `--max-offset`, `--sync-windows`, `--sync-window-len`,
  `--prior-window`, `--prior-tolerance` — tune the search; defaults match
  `../docs/DESIGN.md` Sec 12.

`sync.yaml` is meant to be read and edited by a human: it records not just
the final offsets but every measurement, its confidence, and any
disagreement with the camera clocks, so a bad auto-sync is diagnosable
rather than mysterious.

**Render (bridge to the legacy script, until Phase 2):**

```
csm sync GAMEDIR --write
legacy/generate-ffmpeg.py --offsets-seconds "$(python3 -c "
import yaml
d = yaml.safe_load(open('GAMEDIR/sync.yaml'))
print(','.join(str(d['cameras'][c]['offset']) for c in (1,2,3,4)))
")" -s GAMEDIR > /tmp/runme.sh
less /tmp/runme.sh   # inspect before running, as always
```

`--offsets-seconds CAM1,CAM2,CAM3,CAM4` on the legacy script accepts exactly
the offsets `csm sync` computes, skipping the old manual frame-hunting step
entirely. `-s`/`--scale` is still required today — see the root `README.md`
for why.

## Tests

```
pytest                    # full suite
pytest -m "not slow"      # fast subset, safe to run on every save
```

`tests/conftest.py`'s `synthetic_game` fixture builds real, short `.mp4`
fixtures with a known injected offset, so the sync tests exercise the real
ffmpeg/ffprobe pipeline without needing real footage.

## Package layout

```
csm/
  cli.py       argparse subcommands
  ffmpeg.py    locate/run ffmpeg+ffprobe, version gate, FfmpegError
  probe.py     ffprobe -> per-segment info, cross-segment validation
  sources.py   camera discovery, segment ordering, metadata.yaml
  sync.py      GCC-PHAT, creation_time hints, topology solve, sync.yaml
```

`sync.py` is the interesting part — see `SYNCHRONIZATION.md`.
