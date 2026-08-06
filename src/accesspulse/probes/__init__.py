"""Probe fleet.

Probes measure the *rendered audience experience*, not server health. Each probe
returns a ProbeReport: typed model findings with confidence and abstention, plus
the Prometheus series the Grafana stack scrapes.
"""

from __future__ import annotations

from ..contracts import FeatureType
from ..simulator import MediaSimulator, SliceObservation
from . import audio_description, caption, player, sign
from .base import ProbeReport, finding

__all__ = [
    "ProbeReport",
    "finding",
    "caption",
    "audio_description",
    "sign",
    "player",
    "run_all",
    "run_for_feature",
]


def run_all(obs: SliceObservation, promised_language: str | None = None) -> list[ProbeReport]:
    return [
        caption.run(obs, promised_language),
        audio_description.run(obs, promised_language),
        sign.run(obs),
        player.run(obs),
    ]


def run_for_feature(
    obs: SliceObservation, feature: FeatureType, promised_language: str | None = None
) -> ProbeReport:
    if feature in (FeatureType.CAPTIONS, FeatureType.SUBTITLES):
        return caption.run(obs, promised_language)
    if feature in (FeatureType.AUDIO_DESCRIPTION, FeatureType.ALTERNATE_AUDIO):
        return audio_description.run(obs, promised_language)
    if feature is FeatureType.SIGN_LANGUAGE:
        return sign.run(obs)
    return player.run(obs)


def sweep(
    sim: MediaSimulator,
    languages: list[str] | None = None,
    territories: list[str] | None = None,
    player_versions: list[str] | None = None,
    window_s: float = 10.0,
) -> list[tuple[SliceObservation, list[ProbeReport]]]:
    """Run the whole fleet across a slice matrix - the live-assurance pass."""
    from ..simulator import _platform_of
    from ..twin import LANGUAGES, PLAYER_VERSIONS, TERRITORIES

    out = []
    for language in languages or LANGUAGES:
        for territory in territories or TERRITORIES:
            for pv in player_versions or PLAYER_VERSIONS:
                obs = sim.observe(language, territory, _platform_of(pv), pv, window_s)
                out.append((obs, run_all(obs, language)))
    return out
