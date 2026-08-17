# csm

Sync and render tooling for a 4-camera, 2-tripod multicam rig (cameras 1+2 on
one tripod, 3+4 on the other, tiled 2x2 with camera 1 top-left). Works with
any camera brand, resolution, or frame rate — nothing here assumes a
specific model.

**See [`../docs/REFERENCE.md`](../docs/REFERENCE.md) for the complete
reference** — every command and flag, directory layout, how to run a quality
sweep on your own footage, and what to do if automatic sync can't find the
sync point. This file is just a quick pointer. `SYNCHRONIZATION.md` covers
how the sync detection actually works, and `../docs/DESIGN.md` has the full
design and the reasoning behind it.

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

```
csm probe GAMEDIR                          # sanity-check the footage
csm sync  GAMEDIR --write                  # detect offsets, writes GAMEDIR/sync.yaml
csm preview GAMEDIR                        # optional: 60s sample, watch before committing
csm render GAMEDIR                         # writes GAMEDIR/final_video.mp4
```

`sync.yaml` is meant to be read and edited by a human: it records not just
the final offsets but every measurement, its confidence, and any
disagreement with the camera clocks, so a bad auto-sync is diagnosable
rather than mysterious. See `../docs/REFERENCE.md` for every flag on every
command, including how to run a quality sweep (`csm preview --sweep
crf=...`) and what to do if `csm sync` can't find a sync point on its own.

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

`sync.py` is the interesting part — see `SYNCHRONIZATION.md`.
