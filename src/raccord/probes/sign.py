"""Sign-language feed probe - technical quality only.

Measures whether the interpreter feed is *deliverable and readable*: continuity,
frozen and black frames, frame rate, whether the signing space stays inside the
crop, picture-in-picture overlap, and synchronisation with programme audio.

This probe deliberately makes no claim about the linguistic content of the
signing. Sign languages are full natural languages with their own grammar;
automated sign-to-text comparison is a research problem that requires
language-specific calibrated models and Deaf-community validation. Raccord
therefore treats any semantic sign signal as assistive evidence for a qualified
human reviewer, never as an authoritative translation, and ships that path
disabled by default (see docs/model_card.md).
"""

from __future__ import annotations

from ..simulator import SliceObservation
from .base import ProbeReport, finding

PROBE = "sign_language"
VERSION = "sign-probe-1.1.0"


def run(obs: SliceObservation) -> ProbeReport:
    report = ProbeReport(
        probe=PROBE,
        probe_version=VERSION,
        slice_key=f"fr-lsf/{obs.territory}/{obs.platform}/{obs.player_version}",
        labels={
            "feature": "sign_language",
            "language": "fr-lsf",
            "territory": obs.territory,
            "platform": obs.platform,
            "player_version": obs.player_version,
            "cdn_region": obs.cdn_region,
        },
    )
    window = (obs.window_start_s, obs.window_end_s)

    if not obs.sign_declared:
        report.notes.append("no sign-language promise for this slice")
        return report

    s = obs.sign
    if s is None or s.frames_expected == 0:
        report.findings.append(
            finding(
                PROBE,
                VERSION,
                "sign.availability",
                0.0,
                unit="ratio",
                confidence=0.9,
                interval=window,
                limitations=("feed declared but no frames observed",),
            )
        )
        report.metrics["raccord_sign_feed_available_ratio"] = 0.0
        return report

    availability = s.frames_delivered / s.frames_expected
    report.findings.append(
        finding(
            PROBE,
            VERSION,
            "sign.availability",
            availability,
            unit="ratio",
            confidence=0.97,
            interval=window,
            detail={"frames_expected": s.frames_expected, "frames_delivered": s.frames_delivered},
        )
    )
    report.metrics["raccord_sign_feed_available_ratio"] = availability

    frozen = s.frozen_frames / max(1, s.frames_delivered)
    report.findings.append(
        finding(
            PROBE,
            VERSION,
            "sign.frozen",
            frozen,
            unit="ratio",
            confidence=0.93,
            interval=window,
            limitations=("frame-difference threshold; a genuinely still hold reads as frozen",),
        )
    )
    report.metrics["raccord_sign_frozen_frame_ratio"] = frozen

    black = s.black_frames / max(1, s.frames_delivered)
    report.findings.append(
        finding(
            PROBE,
            VERSION,
            "sign.black",
            black,
            unit="ratio",
            confidence=0.97,
            interval=window,
        )
    )
    report.metrics["raccord_sign_black_frame_ratio"] = black

    report.findings.append(
        finding(
            PROBE,
            VERSION,
            "sign.framerate",
            s.fps,
            unit="fps",
            confidence=0.98,
            interval=window,
            detail={"promised_fps": 50.0},
        )
    )
    report.metrics["raccord_sign_fps"] = s.fps

    report.findings.append(
        finding(
            PROBE,
            VERSION,
            "sign.visibility",
            s.interpreter_visible_ratio,
            unit="ratio",
            confidence=0.85,
            interval=window,
            detail={"pip_overlap_ratio": s.pip_overlap_ratio},
            limitations=("pose-based signing-space estimate; unusual framing lowers confidence",),
        )
    )
    report.metrics["raccord_sign_interpreter_visible_ratio"] = s.interpreter_visible_ratio
    report.metrics["raccord_sign_pip_overlap_ratio"] = s.pip_overlap_ratio

    report.findings.append(
        finding(
            PROBE,
            VERSION,
            "sign.sync",
            s.sync_drift_s,
            unit="s",
            confidence=0.9,
            interval=window,
        )
    )
    report.metrics["raccord_sign_sync_drift_seconds"] = s.sync_drift_s

    # Semantic consistency is intentionally not asserted.
    report.notes.append("semantic consistency of signing is not asserted; technical quality only")
    return report
