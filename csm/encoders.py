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


DEFAULT_CRF = "23"
DEFAULT_PRESET = "fast"
DEFAULT_PSY_RD = "2.0"
DEFAULT_AQ_STRENGTH = "0.3"


def _final_h265_args(
    timeline: Timeline, *, crf: str, preset: str,
    psy_rd: str = DEFAULT_PSY_RD, aq_strength: str = DEFAULT_AQ_STRENGTH,
) -> list[str]:
    gop = 2 * round(float(timeline.fps))
    # colorprim/transfer/colormatrix/range are repeated here in
    # -x265-params, not just the generic -color_* flags below: on this
    # ffmpeg/libx265 build the generic flags land -colorspace (matrix) in
    # the VUI but silently leave primaries/transfer as "unknown" (verified
    # directly against a plain ffmpeg invocation, not just this codebase).
    # The -x265-params form is what actually round-trips all four tags.
    x265_params = (
        f"keyint={gop}:min-keyint={gop}:no-open-gop=1:repeat-headers=1:"
        f"aq-mode=3:aq-strength={aq_strength}:psy-rd={psy_rd}:psy-rdoq=1.0:"
        f"rc-lookahead=40:ref=4:pmode=1:"
        f"colorprim=bt709:transfer=bt709:colormatrix=bt709:range=limited"
    )
    # A +0.5-frame epsilon on -t: ffmpeg's output truncation drops the
    # final frame when -t lands exactly on a frame boundary (verified: -t
    # equal to nb_frames/fps yields nb_frames-1; a small excess doesn't).
    duration = timeline.duration + 0.5 / float(timeline.fps)
    return [
        "-c:v", "libx265", "-preset", preset, "-crf", crf,
        "-pix_fmt", "yuv420p10le", "-profile:v", "main10",
        "-x265-params", x265_params,
        "-color_primaries", "bt709", "-color_trc", "bt709",
        "-colorspace", "bt709", "-color_range", "tv",
        "-tag:v", "hvc1",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        "-t", f"{duration:.6f}",
    ]


def final_h265_profile(
    *, crf: str = DEFAULT_CRF, preset: str = DEFAULT_PRESET,
    psy_rd: str = DEFAULT_PSY_RD, aq_strength: str = DEFAULT_AQ_STRENGTH,
) -> EncoderProfile:
    """CRF/preset/psy-rd/aq-strength picked via a real --sweep (Sec 7.2,
    13.6) can be persisted per game in GAMEDIR/csm.yaml rather than retyped
    on every render -- this builds a fresh profile with those values baked
    in, instead of the one fixed FINAL_H265 instance. Name stays
    "final-h265" either way; only the tuning underneath changes."""
    return EncoderProfile(
        name="final-h265",
        build_args=lambda timeline: _final_h265_args(
            timeline, crf=crf, preset=preset, psy_rd=psy_rd, aq_strength=aq_strength,
        ),
    )


FINAL_H265 = final_h265_profile()


def _preview_common_args(timeline: Timeline) -> list[str]:
    duration = timeline.duration + 0.5 / float(timeline.fps)
    return [
        "-tag:v", "hvc1",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        "-t", f"{duration:.6f}",
    ]


def _preview_videotoolbox_args(timeline: Timeline) -> list[str]:
    # No constant-quality mode on Intel/T2 (Sec 3.4: hevc_videotoolbox
    # rejects -q:v there), so preview uses a fixed ABR bitrate -- fine for a
    # throwaway sync check, not meant to be judged on quality.
    return ["-c:v", "hevc_videotoolbox", "-b:v", "20000k"] + _preview_common_args(timeline)


def _preview_x265_args(timeline: Timeline) -> list[str]:
    return (
        ["-c:v", "libx265", "-preset", "veryfast", "-crf", "23"]
        + _preview_common_args(timeline)
    )


PREVIEW_VIDEOTOOLBOX = EncoderProfile(
    name="preview-videotoolbox", build_args=_preview_videotoolbox_args
)
PREVIEW_X265 = EncoderProfile(name="preview-x265", build_args=_preview_x265_args)

PROFILES: dict[str, EncoderProfile] = {
    FINAL_H265.name: FINAL_H265,
    PREVIEW_VIDEOTOOLBOX.name: PREVIEW_VIDEOTOOLBOX,
    PREVIEW_X265.name: PREVIEW_X265,
}


def get_profile(name: str) -> EncoderProfile:
    try:
        return PROFILES[name]
    except KeyError:
        raise ValueError(f"unknown encoder profile: {name!r} (available: {sorted(PROFILES)})")


SWEEP_ENCODERS = ("libx265", "libsvtav1")

# SVT-AV1's -preset is a bare number (0-13, lower = slower/better), not a
# named string like x265's -- unlike x265, ffmpeg does not accept
# "fast"/"medium" here at all (it fails at encoder-open time, not with a
# helpful error). Named presets stay usable across both encoders in a
# shared --sweep preset=... dimension (Sec 7.3's own example) by mapping
# to the numeric preset x265's equivalent name roughly corresponds to; 6
# for "medium" matches Sec 7.5's own reference SVT-AV1 invocation.
_SVTAV1_PRESET_ALIASES = {
    "veryslow": "2", "slow": "4", "medium": "6", "fast": "8",
    "veryfast": "10", "ultrafast": "12",
}


def _svtav1_preset(preset: str) -> str:
    if preset.lstrip("-").isdigit():
        return preset
    try:
        return _SVTAV1_PRESET_ALIASES[preset]
    except KeyError:
        raise ValueError(
            f"unknown SVT-AV1 preset: {preset!r} -- pass a number (0-13, lower=slower/better "
            f"quality) or one of {sorted(_SVTAV1_PRESET_ALIASES)}"
        )


def sweep_args(
    timeline: Timeline, *, encoder: str = "libx265", crf: str = DEFAULT_CRF, preset: str = DEFAULT_PRESET,
    psy_rd: str = DEFAULT_PSY_RD, aq_strength: str = DEFAULT_AQ_STRENGTH,
) -> list[str]:
    """Sec 7.3: `--sweep encoder=...,crf=...,preset=...,psy-rd=...,aq-strength=...`
    -- variant output args built the same shape as the real profiles above
    (including the same aq-mode/psy-rd/rc-lookahead/ref tuning as
    final-h265, not just keyint), so measured numbers transfer directly to
    a real EncoderProfile. psy_rd/aq_strength only apply to libx265 --
    SVT-AV1's tuning knobs are a different set entirely, out of scope
    here."""
    duration = timeline.duration + 0.5 / float(timeline.fps)
    if encoder == "libsvtav1":
        gop = 4 * round(float(timeline.fps))
        video = [
            "-c:v", "libsvtav1", "-preset", _svtav1_preset(preset), "-crf", crf,
            "-pix_fmt", "yuv420p10le",
            "-svtav1-params", f"keyint={gop}:scd=0",
            "-tag:v", "av01",
        ]
    elif encoder == "libx265":
        gop = 2 * round(float(timeline.fps))
        x265_params = (
            f"keyint={gop}:min-keyint={gop}:no-open-gop=1:repeat-headers=1:"
            f"aq-mode=3:aq-strength={aq_strength}:psy-rd={psy_rd}:psy-rdoq=1.0:"
            f"rc-lookahead=40:ref=4:pmode=1"
        )
        video = [
            "-c:v", "libx265", "-preset", preset, "-crf", crf,
            "-pix_fmt", "yuv420p10le", "-profile:v", "main10",
            "-x265-params", x265_params,
            "-tag:v", "hvc1",
        ]
    else:
        raise ValueError(f"unknown sweep encoder: {encoder!r} (available: {SWEEP_ENCODERS})")
    return video + [
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        "-t", f"{duration:.6f}",
    ]


def resolve_preview_profile(name: str, capabilities) -> EncoderProfile:
    """Sec 7.5: `--preview-encoder auto|NAME` -- auto picks
    hevc_videotoolbox when available (fast, macOS-only), else
    libx265 -preset veryfast."""
    if name != "auto":
        return get_profile(name)
    if capabilities.has_hevc_videotoolbox:
        return PREVIEW_VIDEOTOOLBOX
    return PREVIEW_X265
