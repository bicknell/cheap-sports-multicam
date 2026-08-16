"""Host capability detection (DESIGN.md Sec 8). Phase 2 needs only enough
to exist as a seam for `plan.py`'s signature; full capability probing
(`has_videotoolbox`, `has_libsvtav1`, ...) lands with the Apple Silicon work
in Phase 5.
"""
from __future__ import annotations

import os
import platform
from dataclasses import dataclass


@dataclass
class HostProfile:
    arch: str
    total_cores: int


def detect_host() -> HostProfile:
    return HostProfile(arch=platform.machine(), total_cores=os.cpu_count() or 1)
