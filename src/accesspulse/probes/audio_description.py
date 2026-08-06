"""Audio-description probe.

Checks the described-audio experience end to end: is the track declared, is it
actually audible, is it in the promised language, is it mixed inside the
loudness window, does it land in the dialogue gaps it was authored for, and can
a real player select it.

The semantic-coverage signal is deliberately advisory. It flags scenes with an
important visual event and no corresponding description window, for a human to
review. It never asserts that a description is editorially sufficient - that
judgement stays with the accessibility specialist.
"""

from __future__ import annotations

from .. import media
from ..simulator import SliceObservation
from . import text
from .base import ProbeReport, finding

PROBE = "audio_description"
VERSION = "ad-probe-1.2.0"
SILENCE_FLOOR_DBFS = -50.0
LOUDNESS_MIN, LOUDNESS_MAX = -24.0, -20.0


def run(obs: SliceObservation, promised_language: str | None = None) -> ProbeReport:
    lang = (promised_language or obs.language)
    if lang not in ("en", "fr"):
        lang = "en"
    report = ProbeReport(
        probe=PROBE, probe_version=VERSION,
        slice_key=f"{lang}/{obs.territory}/{obs.platform}/{obs.player_version}",
        labels={
            "feature": "audio_description", "language": lang,
            "territory": obs.territory, "platform": obs.platform,
            "player_version": obs.player_version, "cdn_region": obs.cdn_region,
        },
    )
    window = (obs.window_start_s, obs.window_end_s)
    track_tag = f"{lang}-desc"
    declared = 1.0 if track_tag in obs.manifest_audio_tracks else 0.0

    report.findings.append(finding(
        PROBE, VERSION, "ad.track_present", declared, unit="ratio", confidence=0.99,
        interval=window, detail={"manifest_audio_tracks": obs.manifest_audio_tracks},
    ))
    report.metrics["accesspulse_ad_track_declared_ratio"] = declared

    if not obs.described:
        insufficient = declared == 1.0
        report.findings.append(finding(
            PROBE, VERSION, "ad.audio_present", 0.0 if not insufficient else 0.0,
            unit="ratio", confidence=0.9 if not insufficient else 0.5,
            abstained=insufficient, data_quality="insufficient" if insufficient else "ok",
            interval=window,
            limitations=("no described window overlapped this observation window",),
        ))
        report.metrics["accesspulse_ad_audio_present_ratio"] = 0.0
        report.metrics["accesspulse_ad_selection_success_ratio"] = (
            1.0 if obs.player.audio_track_selection_ok else 0.0
        )
        return report

    # -- audible content ---------------------------------------------------
    audible = sum(1 for w in obs.described if w.peak_dbfs > SILENCE_FLOOR_DBFS)
    audible_ratio = audible / len(obs.described)
    report.findings.append(finding(
        PROBE, VERSION, "ad.audio_present", audible_ratio, unit="ratio",
        confidence=0.95, interval=window,
        detail={"windows": len(obs.described), "silent_windows": len(obs.described) - audible,
                "silence_floor_dbfs": SILENCE_FLOOR_DBFS},
    ))
    report.metrics["accesspulse_ad_audio_present_ratio"] = audible_ratio

    # -- language ----------------------------------------------------------
    described_text = " ".join(
        media.SCENES[w.covers_scene_index].text.get(w.language, "") for w in obs.described
    )
    detected, conf = text.identify_language(described_text)
    match = 1.0 if (detected == "unknown" or detected == lang) else 0.0
    report.findings.append(finding(
        PROBE, VERSION, "ad.language", match, unit="ratio",
        confidence=max(0.4, conf), interval=window,
        detail={"promised": lang, "detected": detected,
                "delivered_track_language": obs.described_language},
    ))
    report.metrics["accesspulse_ad_language_match_ratio"] = match

    # -- timeline drift ----------------------------------------------------
    drifts = [abs(w.start_s - w.target_scene_start) for w in obs.described]
    drift = sum(drifts) / len(drifts)
    report.findings.append(finding(
        PROBE, VERSION, "ad.drift", drift, unit="s", confidence=0.9,
        ci=(min(drifts), max(drifts)), interval=window,
        detail={"windows": len(obs.described)},
    ))
    report.metrics["accesspulse_ad_drift_seconds"] = drift

    # -- dialogue masking: description must sit in an authored gap ---------
    gaps = media.dialogue_gaps(obs.window_start_s - 5.0, obs.window_end_s + 5.0)
    in_gap = 0
    for w in obs.described:
        if any(g0 - 0.4 <= w.start_s and w.end_s <= g1 + 0.4 for g0, g1 in gaps):
            in_gap += 1
    masking_ok = in_gap / len(obs.described)
    report.findings.append(finding(
        PROBE, VERSION, "ad.dialogue_masking_ok", masking_ok, unit="ratio",
        confidence=0.8, interval=window,
        detail={"windows_in_gap": in_gap, "gaps_considered": len(gaps)},
        limitations=("gap model derived from the authored script, not from VAD on a mix",),
    ))
    report.metrics["accesspulse_ad_dialogue_masking_ok_ratio"] = masking_ok

    # -- loudness ----------------------------------------------------------
    in_range = sum(
        1 for w in obs.described
        if w.peak_dbfs > SILENCE_FLOOR_DBFS and LOUDNESS_MIN <= w.loudness_lufs <= LOUDNESS_MAX
    )
    loud_ratio = in_range / len(obs.described)
    report.findings.append(finding(
        PROBE, VERSION, "ad.loudness", loud_ratio, unit="ratio", confidence=0.9,
        interval=window,
        detail={"target_lufs": [LOUDNESS_MIN, LOUDNESS_MAX],
                "observed": [round(w.loudness_lufs, 1) for w in obs.described]},
    ))
    report.metrics["accesspulse_ad_loudness_in_range_ratio"] = loud_ratio

    # -- channel layout ----------------------------------------------------
    layout_ok = sum(1 for w in obs.described if w.channels in (1, 2)) / len(obs.described)
    report.metrics["accesspulse_ad_channel_layout_ok_ratio"] = layout_ok

    # -- selection ---------------------------------------------------------
    selection = 1.0 if obs.player.audio_track_selection_ok else 0.0
    report.findings.append(finding(
        PROBE, VERSION, "ad.selection", selection, unit="ratio", confidence=0.9,
        interval=window,
    ))
    report.metrics["accesspulse_ad_selection_success_ratio"] = selection

    # -- advisory semantic coverage ---------------------------------------
    scenes = media.scenes_in(obs.window_start_s, obs.window_end_s)
    covered = {w.covers_scene_index for w in obs.described}
    important = [s for _, s in scenes if s.importance >= 0.8]
    uncovered = [s.index for s in important if s.index not in covered]
    coverage = 1.0 - (len(uncovered) / len(important)) if important else 1.0
    report.findings.append(finding(
        PROBE, VERSION, "ad.semantic_coverage", coverage, unit="score",
        confidence=0.45, interval=window,
        limitations=(
            "advisory only: flags important visual events with no description window; "
            "does not judge editorial sufficiency, which requires specialist review",
        ),
        detail={"uncovered_important_scenes": uncovered},
    ))
    report.metrics["accesspulse_ad_semantic_coverage_advisory"] = coverage
    if uncovered:
        report.notes.append(
            f"advisory: {len(uncovered)} important visual event(s) without description - "
            "queue for accessibility specialist review"
        )
    return report
