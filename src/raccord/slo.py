"""Accessibility reliability objectives and error budgets.

Accessibility features are treated as production services: each has an SLI with
a measurable definition, an objective per event tier, an error budget, and a
burn-rate policy that decides alert severity. This module is the single source
of truth used by the probes, the alert rules generated for Grafana, and the
post-incident error-budget accounting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .contracts import FeatureType, Severity, SLOTier


class Comparator(str, Enum):
    LOWER_IS_BETTER = "lower_is_better"
    HIGHER_IS_BETTER = "higher_is_better"


@dataclass(frozen=True)
class SLODefinition:
    slo_id: str
    feature: FeatureType
    name: str
    description: str
    sli_metric: str  # Prometheus metric produced by the probe fleet
    unit: str
    comparator: Comparator
    objectives: dict[SLOTier, float]  # threshold per event tier
    window_minutes: int = 60
    budget_minutes: dict[SLOTier, float] = field(default_factory=dict)
    hard_gate: bool = True  # blocks preflight certification when failing

    def threshold(self, tier: SLOTier) -> float:
        return self.objectives.get(tier, list(self.objectives.values())[0])

    def budget(self, tier: SLOTier) -> float:
        if self.budget_minutes:
            return self.budget_minutes.get(tier, list(self.budget_minutes.values())[0])
        return {
            SLOTier.TIER_0_GLOBAL_LIVE: 1.0,
            SLOTier.TIER_1_REGIONAL_LIVE: 3.0,
            SLOTier.TIER_2_VOD_PREMIUM: 10.0,
            SLOTier.TIER_3_CATALOG: 30.0,
        }[tier]

    def breached(self, observed: float, tier: SLOTier) -> bool:
        thr = self.threshold(tier)
        if self.comparator is Comparator.LOWER_IS_BETTER:
            return observed > thr
        return observed < thr


def _tiered(t0: float, t1: float, t2: float, t3: float) -> dict[SLOTier, float]:
    return {
        SLOTier.TIER_0_GLOBAL_LIVE: t0,
        SLOTier.TIER_1_REGIONAL_LIVE: t1,
        SLOTier.TIER_2_VOD_PREMIUM: t2,
        SLOTier.TIER_3_CATALOG: t3,
    }


# ---------------------------------------------------------------------------
# Caption SLOs
# ---------------------------------------------------------------------------

CAPTION_SLOS: list[SLODefinition] = [
    SLODefinition(
        "cap.availability",
        FeatureType.CAPTIONS,
        "Caption track availability",
        "Fraction of 10 s windows in which the promised caption track is present and non-empty.",
        "raccord_caption_track_available_ratio",
        "ratio",
        Comparator.HIGHER_IS_BETTER,
        _tiered(0.999, 0.998, 0.995, 0.99),
    ),
    SLODefinition(
        "cap.first_caption_latency",
        FeatureType.CAPTIONS,
        "First caption latency",
        "Seconds from first spoken word to first rendered caption.",
        "raccord_caption_first_latency_seconds",
        "s",
        Comparator.LOWER_IS_BETTER,
        _tiered(3.0, 4.0, 6.0, 10.0),
    ),
    SLODefinition(
        "cap.drift",
        FeatureType.CAPTIONS,
        "End-to-end caption drift",
        "Median signed offset between rendered caption and aligned spoken dialogue.",
        "raccord_caption_drift_seconds",
        "s",
        Comparator.LOWER_IS_BETTER,
        _tiered(1.5, 2.0, 3.0, 5.0),
    ),
    SLODefinition(
        "cap.omission_rate",
        FeatureType.CAPTIONS,
        "Word omission rate",
        "Fraction of spoken tokens with no aligned caption token.",
        "raccord_caption_omission_ratio",
        "ratio",
        Comparator.LOWER_IS_BETTER,
        _tiered(0.03, 0.05, 0.08, 0.12),
    ),
    SLODefinition(
        "cap.semantic",
        FeatureType.CAPTIONS,
        "Semantic preservation",
        "Embedding similarity between caption text and reference transcript.",
        "raccord_caption_semantic_score",
        "score",
        Comparator.HIGHER_IS_BETTER,
        _tiered(0.90, 0.88, 0.85, 0.80),
    ),
    SLODefinition(
        "cap.speaker_accuracy",
        FeatureType.CAPTIONS,
        "Speaker attribution accuracy",
        "Fraction of speaker-change events labelled correctly.",
        "raccord_caption_speaker_accuracy_ratio",
        "ratio",
        Comparator.HIGHER_IS_BETTER,
        _tiered(0.95, 0.93, 0.90, 0.85),
        hard_gate=False,
    ),
    SLODefinition(
        "cap.wrong_language",
        FeatureType.CAPTIONS,
        "Wrong-language rate",
        "Fraction of caption cues detected in a language other than the promised one.",
        "raccord_caption_wrong_language_ratio",
        "ratio",
        Comparator.LOWER_IS_BETTER,
        _tiered(0.001, 0.002, 0.005, 0.01),
    ),
    SLODefinition(
        "cap.duplicate",
        FeatureType.CAPTIONS,
        "Duplicate cue rate",
        "Fraction of consecutive identical caption cues.",
        "raccord_caption_duplicate_ratio",
        "ratio",
        Comparator.LOWER_IS_BETTER,
        _tiered(0.01, 0.02, 0.04, 0.08),
        hard_gate=False,
    ),
    SLODefinition(
        "cap.reading_speed",
        FeatureType.CAPTIONS,
        "Reading-speed compliance",
        "Fraction of cues at or below 20 characters per second.",
        "raccord_caption_reading_speed_ok_ratio",
        "ratio",
        Comparator.HIGHER_IS_BETTER,
        _tiered(0.95, 0.93, 0.90, 0.85),
        hard_gate=False,
    ),
    SLODefinition(
        "cap.flicker",
        FeatureType.CAPTIONS,
        "Caption persistence / flicker",
        "Rate of cues displayed for less than 1 s.",
        "raccord_caption_flicker_ratio",
        "ratio",
        Comparator.LOWER_IS_BETTER,
        _tiered(0.02, 0.03, 0.05, 0.10),
        hard_gate=False,
    ),
    SLODefinition(
        "cap.render_success",
        FeatureType.CAPTIONS,
        "Device render success",
        "Fraction of synthetic player journeys in which captions were observed on screen.",
        "raccord_caption_render_success_ratio",
        "ratio",
        Comparator.HIGHER_IS_BETTER,
        _tiered(0.999, 0.998, 0.995, 0.99),
    ),
]

# ---------------------------------------------------------------------------
# Audio description SLOs
# ---------------------------------------------------------------------------

AD_SLOS: list[SLODefinition] = [
    SLODefinition(
        "ad.track_present",
        FeatureType.AUDIO_DESCRIPTION,
        "Described track declared",
        "Manifest declares the described audio track for the promised language.",
        "raccord_ad_track_declared_ratio",
        "ratio",
        Comparator.HIGHER_IS_BETTER,
        _tiered(1.0, 1.0, 0.999, 0.995),
    ),
    SLODefinition(
        "ad.audio_present",
        FeatureType.AUDIO_DESCRIPTION,
        "Described audio audible",
        "Fraction of description windows containing audio above the silence floor.",
        "raccord_ad_audio_present_ratio",
        "ratio",
        Comparator.HIGHER_IS_BETTER,
        _tiered(0.99, 0.98, 0.97, 0.95),
    ),
    SLODefinition(
        "ad.drift",
        FeatureType.AUDIO_DESCRIPTION,
        "Description timeline drift",
        "Offset between description events and their target scene intervals.",
        "raccord_ad_drift_seconds",
        "s",
        Comparator.LOWER_IS_BETTER,
        _tiered(0.8, 1.0, 1.5, 2.5),
    ),
    SLODefinition(
        "ad.loudness",
        FeatureType.AUDIO_DESCRIPTION,
        "Loudness within range",
        "Integrated loudness of the described track within -24..-20 LUFS.",
        "raccord_ad_loudness_in_range_ratio",
        "ratio",
        Comparator.HIGHER_IS_BETTER,
        _tiered(0.98, 0.97, 0.95, 0.90),
        hard_gate=False,
    ),
    SLODefinition(
        "ad.language",
        FeatureType.AUDIO_DESCRIPTION,
        "Correct description language",
        "Detected language of the described track matches the promise.",
        "raccord_ad_language_match_ratio",
        "ratio",
        Comparator.HIGHER_IS_BETTER,
        _tiered(0.999, 0.998, 0.995, 0.99),
    ),
    SLODefinition(
        "ad.selection",
        FeatureType.AUDIO_DESCRIPTION,
        "Track selection success",
        "Synthetic journeys able to select the described track from the player menu.",
        "raccord_ad_selection_success_ratio",
        "ratio",
        Comparator.HIGHER_IS_BETTER,
        _tiered(0.999, 0.998, 0.995, 0.99),
    ),
]

# ---------------------------------------------------------------------------
# Sign-language SLOs
# ---------------------------------------------------------------------------

SIGN_SLOS: list[SLODefinition] = [
    SLODefinition(
        "sign.availability",
        FeatureType.SIGN_LANGUAGE,
        "Sign feed availability",
        "Fraction of windows in which the interpreter feed is delivering frames.",
        "raccord_sign_feed_available_ratio",
        "ratio",
        Comparator.HIGHER_IS_BETTER,
        _tiered(0.99, 0.99, 0.98, 0.95),
    ),
    SLODefinition(
        "sign.frozen",
        FeatureType.SIGN_LANGUAGE,
        "Frozen-frame rate",
        "Fraction of frames identical to the previous frame beyond motion tolerance.",
        "raccord_sign_frozen_frame_ratio",
        "ratio",
        Comparator.LOWER_IS_BETTER,
        _tiered(0.01, 0.02, 0.04, 0.08),
    ),
    SLODefinition(
        "sign.black",
        FeatureType.SIGN_LANGUAGE,
        "Black-frame rate",
        "Fraction of frames below the luminance floor.",
        "raccord_sign_black_frame_ratio",
        "ratio",
        Comparator.LOWER_IS_BETTER,
        _tiered(0.002, 0.005, 0.01, 0.02),
    ),
    SLODefinition(
        "sign.framerate",
        FeatureType.SIGN_LANGUAGE,
        "Frame-rate stability",
        "Delivered frames per second on the interpreter feed.",
        "raccord_sign_fps",
        "fps",
        Comparator.HIGHER_IS_BETTER,
        _tiered(45.0, 45.0, 40.0, 24.0),
    ),
    SLODefinition(
        "sign.visibility",
        FeatureType.SIGN_LANGUAGE,
        "Interpreter visibility",
        "Fraction of frames with the interpreter's signing space fully inside the crop.",
        "raccord_sign_interpreter_visible_ratio",
        "ratio",
        Comparator.HIGHER_IS_BETTER,
        _tiered(0.995, 0.99, 0.98, 0.95),
    ),
    SLODefinition(
        "sign.sync",
        FeatureType.SIGN_LANGUAGE,
        "Sign feed A/V sync",
        "Offset between the interpreter feed and programme audio.",
        "raccord_sign_sync_drift_seconds",
        "s",
        Comparator.LOWER_IS_BETTER,
        _tiered(0.5, 0.8, 1.2, 2.0),
    ),
]

# ---------------------------------------------------------------------------
# Accessible player / access flow SLOs
# ---------------------------------------------------------------------------

PLAYER_SLOS: list[SLODefinition] = [
    SLODefinition(
        "player.keyboard",
        FeatureType.ACCESSIBLE_PLAYER,
        "Keyboard journey completion",
        "Synthetic keyboard-only journeys reaching playback with captions enabled.",
        "raccord_player_keyboard_completion_ratio",
        "ratio",
        Comparator.HIGHER_IS_BETTER,
        _tiered(1.0, 1.0, 0.99, 0.98),
    ),
    SLODefinition(
        "player.screen_reader",
        FeatureType.ACCESSIBLE_PLAYER,
        "Screen-reader completion",
        "Synthetic screen-reader journeys completing the same task.",
        "raccord_player_screenreader_completion_ratio",
        "ratio",
        Comparator.HIGHER_IS_BETTER,
        _tiered(1.0, 0.99, 0.98, 0.95),
    ),
    SLODefinition(
        "player.accessible_name",
        FeatureType.ACCESSIBLE_PLAYER,
        "Accessible name completeness",
        "Fraction of interactive controls exposing a non-empty accessible name.",
        "raccord_player_accessible_name_ratio",
        "ratio",
        Comparator.HIGHER_IS_BETTER,
        _tiered(1.0, 1.0, 0.99, 0.98),
    ),
    SLODefinition(
        "player.focus_visible",
        FeatureType.ACCESSIBLE_PLAYER,
        "Visible focus",
        "Fraction of focusable controls with a visible focus indicator.",
        "raccord_player_focus_visible_ratio",
        "ratio",
        Comparator.HIGHER_IS_BETTER,
        _tiered(1.0, 1.0, 0.99, 0.98),
    ),
    SLODefinition(
        "player.caption_control",
        FeatureType.ACCESSIBLE_PLAYER,
        "Caption control operability",
        "Caption menu discoverable and operable in synthetic journeys.",
        "raccord_player_caption_control_ok_ratio",
        "ratio",
        Comparator.HIGHER_IS_BETTER,
        _tiered(1.0, 1.0, 0.99, 0.98),
    ),
    SLODefinition(
        "player.reduced_motion",
        FeatureType.ACCESSIBLE_PLAYER,
        "Reduced motion respected",
        "Fraction of journeys where prefers-reduced-motion suppressed non-essential motion.",
        "raccord_player_reduced_motion_ok_ratio",
        "ratio",
        Comparator.HIGHER_IS_BETTER,
        _tiered(1.0, 0.99, 0.98, 0.95),
        hard_gate=False,
    ),
    SLODefinition(
        "auth.completion",
        FeatureType.ACCESSIBLE_AUTH,
        "Accessible authentication completion",
        "Screen-reader + keyboard sign-in journeys completing successfully.",
        "raccord_auth_accessible_completion_ratio",
        "ratio",
        Comparator.HIGHER_IS_BETTER,
        _tiered(1.0, 0.99, 0.99, 0.98),
    ),
    SLODefinition(
        "purchase.completion",
        FeatureType.ACCESSIBLE_PURCHASE,
        "Accessible purchase completion",
        "Screen-reader + keyboard purchase journeys completing successfully.",
        "raccord_purchase_accessible_completion_ratio",
        "ratio",
        Comparator.HIGHER_IS_BETTER,
        _tiered(1.0, 0.99, 0.98, 0.95),
    ),
]

# ---------------------------------------------------------------------------
# Operational SLOs (about Raccord itself)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OperationalSLO:
    slo_id: str
    name: str
    metric: str
    max_seconds: float | None = None
    max_ratio: float | None = None


OPERATIONAL_SLOS: list[OperationalSLO] = [
    OperationalSLO("ops.ttd", "Time to detect", "raccord_time_to_detect_seconds", 30.0),
    OperationalSLO("ops.tts", "Time to scope", "raccord_time_to_scope_seconds", 45.0),
    OperationalSLO(
        "ops.tte", "Time to evidence complete", "raccord_time_to_evidence_seconds", 90.0
    ),
    OperationalSLO("ops.tta", "Time to approved action", "raccord_time_to_approval_seconds", 180.0),
    OperationalSLO("ops.ttr", "Time to recovery", "raccord_time_to_recovery_seconds", 300.0),
    OperationalSLO(
        "ops.ttc", "Time to public status update", "raccord_time_to_communication_seconds", 420.0
    ),
    OperationalSLO(
        "ops.false_closure", "False closure rate", "raccord_false_closure_ratio", None, 0.01
    ),
    OperationalSLO(
        "ops.unsafe_action", "Unsafe action rate", "raccord_unsafe_action_ratio", None, 0.0
    ),
]


ALL_SLOS: list[SLODefinition] = CAPTION_SLOS + AD_SLOS + SIGN_SLOS + PLAYER_SLOS
SLO_BY_ID: dict[str, SLODefinition] = {s.slo_id: s for s in ALL_SLOS}
SLO_BY_METRIC: dict[str, SLODefinition] = {s.sli_metric: s for s in ALL_SLOS}


def slos_for(feature: FeatureType) -> list[SLODefinition]:
    return [s for s in ALL_SLOS if s.feature is feature]


def hard_gates() -> list[SLODefinition]:
    return [s for s in ALL_SLOS if s.hard_gate]


# ---------------------------------------------------------------------------
# Error budget accounting
# ---------------------------------------------------------------------------


@dataclass
class ErrorBudget:
    slo_id: str
    tier: SLOTier
    budget_minutes: float
    consumed_minutes: float = 0.0

    @property
    def remaining_minutes(self) -> float:
        return max(0.0, self.budget_minutes - self.consumed_minutes)

    @property
    def consumed_fraction(self) -> float:
        if self.budget_minutes <= 0:
            return 1.0
        return min(1.0, self.consumed_minutes / self.budget_minutes)

    def consume(self, seconds: float) -> None:
        self.consumed_minutes += seconds / 60.0


class ErrorBudgetLedger:
    def __init__(self, tier: SLOTier = SLOTier.TIER_0_GLOBAL_LIVE) -> None:
        self.tier = tier
        self._budgets: dict[str, ErrorBudget] = {
            s.slo_id: ErrorBudget(s.slo_id, tier, s.budget(tier)) for s in ALL_SLOS
        }

    def consume(self, slo_id: str, seconds: float) -> ErrorBudget:
        b = self._budgets[slo_id]
        b.consume(seconds)
        return b

    def get(self, slo_id: str) -> ErrorBudget:
        return self._budgets[slo_id]

    def all(self) -> list[ErrorBudget]:
        return list(self._budgets.values())

    def worst(self, n: int = 5) -> list[ErrorBudget]:
        return sorted(self._budgets.values(), key=lambda b: -b.consumed_fraction)[:n]

    def reset(self) -> None:
        for b in self._budgets.values():
            b.consumed_minutes = 0.0


def burn_severity(consumed_fraction: float, breach_magnitude: float) -> Severity:
    """Multi-window burn-rate policy condensed to a severity.

    breach_magnitude is observed/threshold (lower-is-better) normalised so that
    1.0 means exactly at the objective.
    """
    if consumed_fraction >= 1.0 or breach_magnitude >= 4.0:
        return Severity.SEV1
    if consumed_fraction >= 0.5 or breach_magnitude >= 2.0:
        return Severity.SEV2
    if consumed_fraction >= 0.2 or breach_magnitude >= 1.3:
        return Severity.SEV3
    return Severity.SEV4
