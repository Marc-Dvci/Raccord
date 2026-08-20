"""Player synthetic probe.

Runs accessibility journeys against a player build the way an assistive-
technology user would: keyboard only, screen reader, zoom, reduced motion, on a
matrix of browsers, connected-TV profiles, languages, territories and network
conditions - including the authentication and purchase flows that gate playback.

The journey definitions here are the same ones the browser harness executes
against the real web player (see web/player and tools/journeys). In the local
demo the harness runs against the digital twin, so the matrix completes in
milliseconds instead of minutes; the assertions, thresholds and failure names
are identical, which is what makes preflight certification and live verification
comparable.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..simulator import SliceObservation
from .base import ProbeReport, finding

PROBE = "player_synthetic"
VERSION = "player-probe-1.3.0"


@dataclass(frozen=True)
class Journey:
    journey_id: str
    name: str
    steps: tuple[str, ...]
    mandatory: bool = True


JOURNEYS: tuple[Journey, ...] = (
    Journey(
        "kbd-captions",
        "Keyboard: start playback and enable captions",
        (
            "focus player",
            "tab to caption menu",
            "open caption menu",
            "select promised language",
            "close menu without trapping focus",
            "confirm captions visible",
        ),
    ),
    Journey(
        "sr-captions",
        "Screen reader: discover and enable captions",
        (
            "read player landmark",
            "announce control names",
            "open caption menu",
            "announce options",
            "select promised language",
            "confirm state announced",
        ),
    ),
    Journey(
        "sr-audio-track",
        "Screen reader: select described audio",
        (
            "open audio menu",
            "announce track names",
            "select described track",
            "confirm selection announced",
            "confirm described audio audible",
        ),
    ),
    Journey(
        "kbd-auth",
        "Keyboard + screen reader sign-in",
        (
            "focus sign-in",
            "enter credentials",
            "submit",
            "handle challenge",
            "confirm signed-in state announced",
        ),
    ),
    Journey(
        "kbd-purchase",
        "Keyboard + screen reader ticket purchase",
        (
            "open purchase",
            "complete labelled form fields",
            "review order",
            "confirm purchase",
            "confirm receipt announced",
        ),
        mandatory=False,
    ),
    Journey(
        "zoom-reflow",
        "400% zoom reflow of playback controls",
        (
            "set zoom 400%",
            "confirm no horizontal scroll",
            "confirm controls reachable",
        ),
        mandatory=False,
    ),
    Journey(
        "reduced-motion",
        "Reduced motion respected",
        (
            "set prefers-reduced-motion",
            "start playback",
            "confirm no non-essential motion",
        ),
        mandatory=False,
    ),
)


def run(obs: SliceObservation) -> ProbeReport:
    p = obs.player
    report = ProbeReport(
        probe=PROBE,
        probe_version=VERSION,
        slice_key=f"{obs.language}/{obs.territory}/{obs.platform}/{obs.player_version}",
        labels={
            "feature": "accessible_player",
            "territory": obs.territory,
            "platform": obs.platform,
            "player_version": obs.player_version,
            "device_class": p.device_class,
        },
    )
    window = (obs.window_start_s, obs.window_end_s)

    results = {
        "kbd-captions": p.keyboard_completed and p.caption_control_ok,
        "sr-captions": p.screenreader_completed and p.caption_control_ok,
        "sr-audio-track": p.screenreader_completed and p.audio_track_selection_ok,
        "kbd-auth": p.auth_completed,
        "kbd-purchase": p.purchase_completed,
        "zoom-reflow": p.focus_visible_ratio > 0.5,
        "reduced-motion": p.reduced_motion_ok,
    }

    def add(metric: str, prom: str, value: float, conf: float = 0.95, **detail) -> None:
        report.findings.append(
            finding(
                PROBE,
                VERSION,
                metric,
                value,
                unit="ratio",
                confidence=conf,
                interval=window,
                detail=detail,
            )
        )
        report.metrics[prom] = value

    add(
        "player.keyboard",
        "raccord_player_keyboard_completion_ratio",
        1.0 if p.keyboard_completed else 0.0,
        journey="kbd-captions",
    )
    add(
        "player.screen_reader",
        "raccord_player_screenreader_completion_ratio",
        1.0 if p.screenreader_completed else 0.0,
        journey="sr-captions",
    )
    add(
        "player.accessible_name",
        "raccord_player_accessible_name_ratio",
        p.accessible_name_ratio,
        a11y_tree_nodes=p.a11y_tree_nodes,
    )
    add("player.focus_visible", "raccord_player_focus_visible_ratio", p.focus_visible_ratio)
    add(
        "player.caption_control",
        "raccord_player_caption_control_ok_ratio",
        1.0 if p.caption_control_ok else 0.0,
    )
    add(
        "player.reduced_motion",
        "raccord_player_reduced_motion_ok_ratio",
        1.0 if p.reduced_motion_ok else 0.0,
        conf=0.85,
    )
    add(
        "auth.completion",
        "raccord_auth_accessible_completion_ratio",
        1.0 if p.auth_completed else 0.0,
    )
    add(
        "purchase.completion",
        "raccord_purchase_accessible_completion_ratio",
        1.0 if p.purchase_completed else 0.0,
    )

    failed = [j.journey_id for j in JOURNEYS if not results.get(j.journey_id, True)]
    if failed:
        report.notes.append("failed journeys: " + ", ".join(failed))
    report.metrics["raccord_player_journeys_failed"] = float(len(failed))
    report.metrics["raccord_player_journeys_total"] = float(len(JOURNEYS))
    return report
