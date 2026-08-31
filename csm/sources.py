"""Camera discovery and segment ordering.

Keeps the `cameraN/` directory convention and `metadata.yaml` format/semantics
from the legacy script (DESIGN.md Sec 15, "keep from the old script,
deliberately"), with one fix: a single non-string metadata value used to
silently drop *all* metadata (Sec 10.1); it's now coerced with a warning.
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from pathlib import Path

import yaml

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mts", ".m4v"}
IGNORED_NAMES = {"concatinated.mp4", "combined.mp4"}


@dataclass
class Camera:
    cam: int
    dir: Path | None
    segments: list[Path] = field(default_factory=list)

    @property
    def present(self) -> bool:
        return bool(self.segments)


def camera_dir_default(game_dir: Path, cam: int) -> Path:
    return game_dir / f"camera{cam}"


def list_segments(camera_dir: Path) -> list[Path]:
    """Ordered segment files in a camera directory. Filename order is the
    recording order (NORM_00001.MP4, NORM_00002.MP4, ...); Sec 10.1 wants
    this cross-checked against creation_time monotonicity, which probe.py
    does once it has ffprobe data for each file."""
    if not camera_dir.is_dir():
        return []
    segs = [
        p
        for p in camera_dir.iterdir()
        if p.is_file()
        and p.suffix.lower() in VIDEO_EXTENSIONS
        and p.name not in IGNORED_NAMES
        and not p.name.startswith("filelist-")
    ]
    return sorted(segs, key=lambda p: p.name)


def discover_cameras(
    game_dir: Path,
    cams=(1, 2, 3, 4),
    camera_dir_overrides: dict[int, Path] | None = None,
) -> dict[int, Camera]:
    """Discover the 4 fixed camera slots (Sec 5, "4 cameras on 2 tripods").
    A camera with no segments is present in the result with `segments=[]` --
    missing cameras are a first-class case, not an error, per Sec 5."""
    overrides = camera_dir_overrides or {}
    cameras = {}
    for cam in cams:
        cam_dir = overrides.get(cam, camera_dir_default(game_dir, cam))
        segs = list_segments(cam_dir)
        cameras[cam] = Camera(cam=cam, dir=cam_dir if cam_dir.is_dir() else None, segments=segs)
    return cameras


def load_metadata(game_dir: Path) -> dict[str, str]:
    """GAMEDIR/metadata.yaml -> str/str mapping. A non-string value used to
    silently drop ALL metadata in the legacy script; here it's coerced with
    a single warning instead (Sec 10.1)."""
    path = game_dir / "metadata.yaml"
    if not path.exists():
        warnings.warn(
            f"{path} not found -- rendering with no video metadata (title, "
            f"description, etc). Create it if that's not intentional."
        )
        return {}
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    result = {}
    coerced_any = False
    for k, v in raw.items():
        if not isinstance(v, str):
            coerced_any = True
            v = str(v)
        result[str(k)] = v
    if coerced_any:
        warnings.warn(
            f"{path}: one or more metadata values were not strings; coerced with str()"
        )
    return result
