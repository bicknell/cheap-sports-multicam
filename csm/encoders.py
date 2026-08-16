"""EncoderProfile + the final-h265 profile (DESIGN.md Sec 7.2). v1 ships
only this profile -- preview/final-av1/draft-vt are deferred (Sec 7.5, 15).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from csm.timeline import Timeline


@dataclass
class EncoderProfile:
    name: str
    build_args: Callable[[Timeline], list[str]]

    def output_args(self, timeline: Timeline) -> list[str]:
        return self.build_args(timeline)


def _final_h265_args(timeline: Timeline) -> list[str]:
    gop = 2 * round(float(timeline.fps))
    # colorprim/transfer/colormatrix/range are repeated here in
    # -x265-params, not just the generic -color_* flags below: on this
    # ffmpeg/libx265 build the generic flags land -colorspace (matrix) in
    # the VUI but silently leave primaries/transfer as "unknown" (verified
    # directly against a plain ffmpeg invocation, not just this codebase).
    # The -x265-params form is what actually round-trips all four tags.
    x265_params = (
        f"keyint={gop}:min-keyint={gop}:no-open-gop=1:repeat-headers=1:"
        f"aq-mode=3:aq-strength=1.0:psy-rd=2.0:psy-rdoq=1.0:rc-lookahead=40:ref=4:"
        f"colorprim=bt709:transfer=bt709:colormatrix=bt709:range=limited"
    )
    # A +0.5-frame epsilon on -t: ffmpeg's output truncation drops the
    # final frame when -t lands exactly on a frame boundary (verified: -t
    # equal to nb_frames/fps yields nb_frames-1; a small excess doesn't).
    duration = timeline.duration + 0.5 / float(timeline.fps)
    return [
        "-c:v", "libx265", "-preset", "medium", "-crf", "23",
        "-pix_fmt", "yuv420p10le", "-profile:v", "main10",
        "-x265-params", x265_params,
        "-color_primaries", "bt709", "-color_trc", "bt709",
        "-colorspace", "bt709", "-color_range", "tv",
        "-tag:v", "hvc1",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        "-t", f"{duration:.6f}",
    ]


FINAL_H265 = EncoderProfile(name="final-h265", build_args=_final_h265_args)

PROFILES: dict[str, EncoderProfile] = {
    FINAL_H265.name: FINAL_H265,
}


def get_profile(name: str) -> EncoderProfile:
    try:
        return PROFILES[name]
    except KeyError:
        raise ValueError(f"unknown encoder profile: {name!r} (available: {sorted(PROFILES)})")
