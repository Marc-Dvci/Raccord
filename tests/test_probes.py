"""The probe fleet must be quiet when the experience is correct, and specific
when it is not. A monitoring system that cries wolf is worse than none."""

from __future__ import annotations

import pytest

from accesspulse import probes
from accesspulse.probes.align import align_tokens, drift_from_alignment
from accesspulse.probes.text import identify_language, similarity
from accesspulse.simulator import MediaSimulator


def _obs(sim, language="en", territory="FR", platform="ctv", pv="ctv-9.4.0", window=30.0):
    return sim.observe(language, territory, platform, pv, window)


def test_healthy_slice_breaches_nothing():
    sim = MediaSimulator(seed=99)
    sim.advance(200)
    for report in probes.run_all(_obs(sim), "en"):
        for f in report.findings:
            if f.abstained:
                continue
            if f.metric in ("cap.drift", "cap.omission_rate", "cap.wrong_language",
                            "cap.duplicate", "cap.flicker", "ad.drift",
                            "sign.frozen", "sign.black", "sign.sync"):
                assert f.score < 0.5, (f.metric, f.score)
            if f.metric in ("cap.availability", "cap.render_success", "player.keyboard",
                            "player.screen_reader", "ad.audio_present",
                            "sign.availability"):
                assert f.score >= 0.99, (f.metric, f.score)


def test_progressive_drift_is_measured_accurately():
    sim = MediaSimulator(seed=99)
    sim.advance(120)
    sim.inject("cap.progressive_drift")
    sim.advance(220)  # past the 180 s ramp, so intensity is saturated
    report = probes.caption.run(_obs(sim), "en")
    drift = report.by_metric("cap.drift")
    assert not drift.abstained
    assert 7.0 <= drift.score <= 9.0
    assert drift.confidence > 0.5
    assert drift.evidence_interval is not None


def test_drift_does_not_leak_to_unaffected_slices():
    sim = MediaSimulator(seed=99)
    sim.advance(120)
    sim.inject("cap.progressive_drift")  # scope: en / western europe / CTV
    sim.advance(220)
    for language, territory, platform, pv in (
        ("en", "US", "ctv", "ctv-9.4.0"),     # outside the territory scope
        ("fr", "FR", "ctv", "ctv-9.4.0"),     # outside the language scope
        ("en", "FR", "web", "web-4.12.0"),    # outside the platform scope
    ):
        report = probes.caption.run(_obs(sim, language, territory, platform, pv), language)
        assert report.value("cap.drift") < 0.5, (language, territory, platform)


def test_wrong_language_is_identified():
    sim = MediaSimulator(seed=7)
    sim.advance(100)
    sim.inject("cap.wrong_language")
    sim.advance(60)
    report = probes.caption.run(_obs(sim, "de", "DE", "web", "web-4.12.0"), "de")
    assert report.value("cap.wrong_language") == 1.0
    assert report.by_metric("cap.wrong_language").detail["detected"] == "es"


def test_probe_abstains_rather_than_guessing_when_there_is_no_content():
    sim = MediaSimulator(seed=3)
    sim.advance(100)
    sim.inject("cap.source_loss")
    sim.advance(40)
    report = probes.caption.run(_obs(sim), "en")
    drift = report.by_metric("cap.drift")
    assert drift.abstained
    assert drift.data_quality == "insufficient"
    assert report.value("cap.availability") == 0.0


def test_silent_description_is_caught():
    sim = MediaSimulator(seed=13)
    sim.advance(100)
    sim.inject("ad.silent_segments")
    sim.advance(90)
    report = probes.audio_description.run(_obs(sim, "en", "US", "web", "web-4.12.0"), "en")
    assert report.value("ad.audio_present") < 0.9


def test_frozen_interpreter_feed_is_caught():
    sim = MediaSimulator(seed=11)
    sim.advance(100)
    sim.inject("sign.frozen")
    sim.advance(40)
    report = probes.sign.run(_obs(sim, "fr", "FR", "web", "web-4.12.0"))
    assert report.value("sign.frozen") > 0.1


def test_sign_probe_makes_no_semantic_claim():
    sim = MediaSimulator(seed=11)
    sim.advance(100)
    report = probes.sign.run(_obs(sim, "fr", "FR", "web", "web-4.12.0"))
    assert all("semantic" not in f.metric for f in report.findings)
    assert any("semantic consistency" in n for n in report.notes)


def test_keyboard_trap_fails_the_player_journey():
    sim = MediaSimulator(seed=17)
    sim.advance(100)
    sim.inject("player.keyboard_trap")
    sim.advance(40)
    report = probes.player.run(_obs(sim, "en", "GB", "web", "web-4.12.0"))
    assert report.value("player.keyboard") == 0.0


# --- alignment ------------------------------------------------------------

def test_alignment_recovers_a_known_offset():
    tokens = ["the", "projector", "has", "been", "running", "for", "eleven", "hours"]
    times = [float(i) * 0.3 for i in range(len(tokens))]
    shifted = [t + 4.0 for t in times]
    alignment = align_tokens(tokens, tokens)
    drift, mad, _ci = drift_from_alignment(alignment, times, shifted)
    assert drift == pytest.approx(4.0, abs=1e-6)
    assert mad == pytest.approx(0.0, abs=1e-6)


def test_alignment_reports_omissions():
    reference = ["a", "b", "c", "d", "e", "f"]
    hypothesis = ["a", "c", "e"]
    alignment = align_tokens(reference, hypothesis)
    assert len(alignment.unmatched_reference) == 3
    assert alignment.match_ratio == pytest.approx(0.5)


def test_language_identifier_separates_the_programme_languages():
    from accesspulse import media

    for lang in ("en", "fr", "de", "es"):
        corpus = " ".join(line.text[lang] for line in media.SCRIPT[:4])
        detected, confidence = identify_language(corpus)
        assert detected == lang, (lang, detected)
        assert confidence > 0.2


def test_similarity_is_higher_for_the_same_text():
    a = "the projector has been running for eleven hours"
    b = "the projector has been running for eleven hours"
    c = "reel four is missing since nineteen sixty two"
    assert similarity(a, b) > similarity(a, c)
