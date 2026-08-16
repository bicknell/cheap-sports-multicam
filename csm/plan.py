"""PURE: Timeline + EncoderProfile + host -> list[FfmpegJob] (DESIGN.md
Sec 4, 9.1). v1 always emits exactly one job -- `runner.py` just runs it,
no pooling or resume. Chunked planning is deferred to Sec 13.5; `host` is
accepted now so that signature doesn't need to change when it lands.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from csm.encoders import EncoderProfile
from csm.filtergraph import FilterGraph, build_filtergraph
from csm.host import HostProfile
from csm.timeline import Timeline


@dataclass
class FfmpegJob:
    graph: FilterGraph
    output_args: list[str]
    output_path: Path
    metadata: dict[str, str] = field(default_factory=dict)


def build_plan(
    timeline: Timeline,
    profile: EncoderProfile,
    output_path: Path,
    host: HostProfile | None = None,
    metadata: dict[str, str] | None = None,
) -> list[FfmpegJob]:
    graph = build_filtergraph(timeline)
    return [FfmpegJob(
        graph=graph, output_args=profile.output_args(timeline), output_path=output_path,
        metadata=metadata or {},
    )]
