"""GAMEDIR/csm.yaml -- persisted per-game render settings (Sec 12), so
choices that don't change between commands (--field-cutoff, a CRF/preset
picked via --sweep) don't have to be retyped on every render/preview.
Same layering as sync.yaml's offset overrides: this file provides the
default, an explicit CLI flag on any given command still wins for that
one run.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class RenderConfig:
    field_cutoff: dict[int, int]
    crf: str | None = None
    preset: str | None = None


def load_render_config(game_dir: Path) -> RenderConfig:
    path = game_dir / "csm.yaml"
    if not path.exists():
        return RenderConfig(field_cutoff={})
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    field_cutoff = {int(k): int(v) for k, v in (raw.get("field_cutoff") or {}).items()}
    crf = str(raw["crf"]) if raw.get("crf") is not None else None
    preset = str(raw["preset"]) if raw.get("preset") is not None else None
    return RenderConfig(field_cutoff=field_cutoff, crf=crf, preset=preset)
