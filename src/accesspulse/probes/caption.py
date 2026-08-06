"""Caption probe.

Measures the caption experience that was actually delivered to one audience
slice, against the programme audio that slice was hearing at the same moment.

Independent methods are combined so that an infrastructure delay is never
confused with a semantic-quality defect:

1. monotonic token alignment (align.py) -> drift, omission, insertion
2. hashed multilingual embedding similarity -> semantic preservation
3. character n-gram language identification -> wrong-language routing
4. deterministic timing rules -> reading speed, flicker, persistence
5. rendered-output check from the synthetic player -> device render success

Every output carries a confidence and can abstain. A window with too little
dialogue produces `data_quality="insufficient"` rather than a confident zero.
"""

from __future__ import annotations

from ..simulator import SliceObservation
from . import text
from .accelerated import align_tokens_accelerated
from .align import drift_from_alignment
from .base import ProbeReport, finding

PROBE = "caption"
VERSION = "caption-probe-1.4.0"
MIN_TOKENS_FOR_CONFIDENCE = 12


# A caption cue is held on screen longer than the words take to say - that is
# what makes it readable. Token timestamps are therefore spread at a bounded
# speaking rate from the cue's in-point rather than across its whole display
# duration, otherwise a well-behaved long-hold cue would look like late speech.
MAX_TOKEN_SPAN_S = 0.34  # ~2.9 tokens per second, the upper bound for speech
MIN_TOKEN_SPAN_S = 0.08


def _cue_token_times(obs: SliceObservation) -> tuple[list[str], list[float], list[str]]:
    tokens: list[str] = []
    times: list[float] = []
    speakers: list[str] = []
    for cue in obs.cues:
        toks = text.tokenise(cue.text)
        if not toks:
            continue
        span = min(MAX_TOKEN_SPAN_S,
                   max(MIN_TOKEN_SPAN_S, (cue.end_s - cue.start_s) / len(toks)))
        for i, tok in enumerate(toks):
            tokens.append(tok)
            times.append(cue.start_s + i * span)
            speakers.append(cue.speaker)
    return tokens, times, speakers


def run(obs: SliceObservation, promised_language: str | None = None) -> ProbeReport:
    lang = promised_language or obs.language
    report = ProbeReport(
        probe=PROBE,
        probe_version=VERSION,
        slice_key=f"{obs.language}/{obs.territory}/{obs.platform}/{obs.player_version}",
        labels={
            "feature": "captions",
            "language": obs.language,
            "territory": obs.territory,
            "platform": obs.platform,
            "player_version": obs.player_version,
            "cdn_region": obs.cdn_region,
        },
    )

    ref_tokens = [t for _, t in obs.reference_tokens]
    ref_times = [ts for ts, _ in obs.reference_tokens]
    hyp_tokens, hyp_times, hyp_speakers = _cue_token_times(obs)
    window = (obs.window_start_s, obs.window_end_s)

    # -- availability ------------------------------------------------------
    track_declared = lang in obs.manifest_caption_tracks
    available = 1.0 if (track_declared and obs.cues) else 0.0
    report.findings.append(finding(
        PROBE, VERSION, "cap.availability", available, unit="ratio",
        confidence=0.99, interval=window,
        detail={"track_declared": track_declared, "cue_count": len(obs.cues)},
    ))
    report.metrics["accesspulse_caption_track_available_ratio"] = available

    if not obs.cues or not ref_tokens:
        report.findings.append(finding(
            PROBE, VERSION, "cap.drift", 0.0, unit="s", confidence=0.0, abstained=True,
            data_quality="insufficient", interval=window,
            limitations=("no caption cues or no dialogue in window",),
        ))
        report.notes.append("abstained on quality metrics: no comparable content")
        report.metrics["accesspulse_caption_omission_ratio"] = 1.0 if ref_tokens else 0.0
        return report

    # -- language ----------------------------------------------------------
    joined = " ".join(c.text for c in obs.cues)
    detected, lang_conf = text.identify_language(joined)
    wrong_language = 1.0 if (detected != "unknown" and detected != lang and lang_conf > 0.25) \
        else 0.0
    report.findings.append(finding(
        PROBE, VERSION, "cap.wrong_language", wrong_language, unit="ratio",
        confidence=max(0.4, lang_conf), interval=window,
        detail={"promised": lang, "detected": detected, "detector_confidence": lang_conf},
        limitations=("trigram identifier; short cues below 12 characters are skipped",),
    ))
    report.metrics["accesspulse_caption_wrong_language_ratio"] = wrong_language

    # -- alignment: drift + omission --------------------------------------
    # Compare against the reference in the *promised* language. When the wrong
    # language is being delivered the alignment collapses, which is why drift is
    # reported with reduced confidence in that case.
    # The dispatcher picks the fastest backend that produces the reference's
    # exact alignment for this input size - see docs/PERFORMANCE.md.
    alignment = align_tokens_accelerated(ref_tokens, hyp_tokens)
    drift, mad, ci = drift_from_alignment(alignment, ref_times, hyp_times)
    enough = len(alignment.pairs) >= MIN_TOKENS_FOR_CONFIDENCE
    drift_conf = 0.0 if not enough else max(0.35, min(0.98, 1.0 - mad / 2.0))
    if wrong_language:
        drift_conf *= 0.4

    worst_interval = None
    if alignment.pairs:
        worst = max(alignment.pairs, key=lambda p: abs(hyp_times[p[1]] - ref_times[p[0]]))
        worst_interval = (ref_times[worst[0]], hyp_times[worst[1]])

    report.findings.append(finding(
        PROBE, VERSION, "cap.drift", abs(drift), unit="s",
        confidence=drift_conf, ci=(abs(ci[0]), abs(ci[1])),
        interval=worst_interval or window,
        abstained=not enough,
        data_quality="ok" if enough else "insufficient",
        limitations=("token-identity alignment; heavy paraphrase reduces matched pairs",),
        detail={
            "signed_drift_s": round(drift, 3),
            "dispersion_mad_s": round(mad, 3),
            "matched_tokens": len(alignment.pairs),
            "reference_tokens": len(ref_tokens),
        },
    ))
    report.metrics["accesspulse_caption_drift_seconds"] = abs(drift)

    omission = len(alignment.unmatched_reference) / max(1, len(ref_tokens))
    report.findings.append(finding(
        PROBE, VERSION, "cap.omission_rate", omission, unit="ratio",
        confidence=0.9 if enough else 0.4, interval=window,
        detail={"unmatched_reference_tokens": len(alignment.unmatched_reference)},
    ))
    report.metrics["accesspulse_caption_omission_ratio"] = omission

    # -- semantic preservation --------------------------------------------
    ref_text = " ".join(ref_tokens)
    sem = text.similarity(ref_text, " ".join(hyp_tokens))
    report.findings.append(finding(
        PROBE, VERSION, "cap.semantic", sem, unit="score",
        confidence=0.75, interval=window,
        limitations=("hashed n-gram similarity, not a trained multilingual encoder",),
    ))
    report.metrics["accesspulse_caption_semantic_score"] = sem

    # -- speaker attribution ----------------------------------------------
    # Evaluated per cue, at the cue's in-point: a cue carries one speaker label,
    # and the question is whether that label names whoever is speaking when the
    # cue appears. Under drift this degrades on purpose - a viewer reading the
    # wrong character's name is a real accessibility failure, not a probe error.
    from .. import media as _media

    correct = 0
    total_labelled = 0
    for cue in obs.cues:
        line = _media.line_at(cue.start_s)
        if not line:
            continue
        total_labelled += 1
        if cue.speaker == line.speaker:
            correct += 1
    speaker_acc = correct / total_labelled if total_labelled else 1.0
    # A 30 s window over this programme carries three to five cues, so the
    # confidence band is set by how many labelled cues were actually available.
    report.findings.append(finding(
        PROBE, VERSION, "cap.speaker_accuracy", speaker_acc, unit="ratio",
        confidence=0.8 if total_labelled >= 5 else 0.5 if total_labelled >= 3 else 0.25,
        abstained=total_labelled < 3, interval=window,
        data_quality="ok" if total_labelled >= 3 else "insufficient",
        limitations=("speaker ground truth taken from the authored script timing",),
        detail={"labelled_cues": total_labelled, "correct": correct},
    ))
    report.metrics["accesspulse_caption_speaker_accuracy_ratio"] = speaker_acc

    # -- duplicates, reading speed, flicker --------------------------------
    dupes = sum(
        1 for a, b in zip(obs.cues, obs.cues[1:])
        if text.normalise(a.text) == text.normalise(b.text)
    )
    dup_ratio = dupes / max(1, len(obs.cues) - 1)
    report.findings.append(finding(
        PROBE, VERSION, "cap.duplicate", dup_ratio, unit="ratio",
        confidence=0.95, interval=window,
    ))
    report.metrics["accesspulse_caption_duplicate_ratio"] = dup_ratio

    ok_speed = sum(
        1 for c in obs.cues
        if text.reading_speed_cps(c.text, c.end_s - c.start_s) <= 20.0
    )
    speed_ratio = ok_speed / len(obs.cues)
    report.findings.append(finding(
        PROBE, VERSION, "cap.reading_speed", speed_ratio, unit="ratio",
        confidence=0.95, interval=window,
        detail={"limit_cps": 20.0},
    ))
    report.metrics["accesspulse_caption_reading_speed_ok_ratio"] = speed_ratio

    flicker = sum(1 for c in obs.cues if (c.end_s - c.start_s) < 1.0) / len(obs.cues)
    report.findings.append(finding(
        PROBE, VERSION, "cap.flicker", flicker, unit="ratio",
        confidence=0.95, interval=window,
    ))
    report.metrics["accesspulse_caption_flicker_ratio"] = flicker

    # -- first caption latency --------------------------------------------
    first_latency = 0.0
    if ref_times and hyp_times:
        first_latency = max(0.0, hyp_times[0] - ref_times[0])
    report.findings.append(finding(
        PROBE, VERSION, "cap.first_caption_latency", first_latency, unit="s",
        confidence=0.8, interval=window,
    ))
    report.metrics["accesspulse_caption_first_latency_seconds"] = first_latency

    # -- rendered output ---------------------------------------------------
    rendered = sum(1 for c in obs.cues if c.rendered) / len(obs.cues)
    render_success = min(rendered, 1.0 if obs.player.captions_rendered else 0.0) \
        if obs.player.console_errors else rendered
    report.findings.append(finding(
        PROBE, VERSION, "cap.render_success", render_success, unit="ratio",
        confidence=0.85, interval=window,
        detail={"console_errors": obs.player.console_errors},
    ))
    report.metrics["accesspulse_caption_render_success_ratio"] = render_success

    return report
