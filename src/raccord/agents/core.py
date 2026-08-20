"""Scope, quality, change-correlation, diagnosis, communication and learning agents.

These agents are deterministic by construction. They read typed records, apply
explicit rules and produce typed records with citations. When a Gemini reasoning
plane is configured (see `adk.py`) it is layered *on top* of these outputs to
synthesise explanation and role-specific language - it never replaces the
evidence, the ranking arithmetic or the policy decision.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..assurance import BreachGroup, LiveAssurance
from ..contracts import (
    Alert,
    ChangeEvent,
    Communication,
    Evidence,
    EvidenceKind,
    FailureClass,
    FeatureType,
    Hypothesis,
    Incident,
    ModelFinding,
    Platform,
    PostIncidentReview,
    Scope,
    utcnow,
)
from ..registry import PromiseRegistry
from ..twin import DigitalTwin

# ---------------------------------------------------------------------------
# Scope agent
# ---------------------------------------------------------------------------


class ScopeAgent:
    """Determines what is broken, where, for whom - and what is still healthy."""

    name = "scope_agent"

    def __init__(
        self, twin: DigitalTwin, registry: PromiseRegistry, assurance: LiveAssurance
    ) -> None:
        self.twin = twin
        self.registry = registry
        self.assurance = assurance

    def run(self, incident: Incident, alert: Alert, group: BreachGroup) -> Scope:
        languages = group.dimension("language")
        territories = group.dimension("territory")
        platforms = [p for p in group.dimension("platform")]
        player_versions = group.dimension("player_version")
        regions = group.dimension("cdn_region")

        # Topology closure from the components that could produce this symptom.
        origins = self._origin_components(group)
        blast = self.twin.blast_radius(origins, at=self.assurance.sim.wall_clock)

        # The observed slice matrix is the authority; topology adds owners,
        # providers and remediation candidates it cannot see from telemetry.
        violated: list[str] = []
        for promise in self.registry.for_event(
            self.assurance.event_id, at=self.assurance.sim.wall_clock
        ):
            if promise.feature is not group.feature:
                continue
            if (
                languages
                and promise.language not in languages
                and promise.feature is not FeatureType.SIGN_LANGUAGE
            ):
                continue
            if territories and not set(promise.territories) & set(territories):
                continue
            violated.append(promise.promise_id)

        affected, protected = self.assurance.affected_sessions(group)
        blast_class = self._classify(territories, regions, platforms, blast.providers)

        owners = {
            "technical": "owner-streaming-sre",
            "accessibility": "owner-a11y-ops",
            "escalation": "owner-technical-director",
        }
        owners.update({k: v for k, v in blast.owners.items()})

        return Scope(
            incident_id=incident.incident_id,
            features=(group.feature,),
            languages=tuple(languages),
            territories=tuple(territories),
            platforms=tuple(Platform(p) for p in platforms if p in Platform._value2member_map_),
            device_classes=tuple(blast.device_classes),
            player_versions=tuple(player_versions),
            cdn_regions=tuple(regions),
            providers=tuple(blast.providers),
            components=tuple(origins),
            violated_promise_ids=tuple(violated),
            affected_sessions=affected,
            protected_sessions=protected,
            blast_class=blast_class,
            owners=owners,
        )

    def _origin_components(self, group: BreachGroup) -> list[str]:
        feature = group.feature
        pvs = group.dimension("player_version")
        if feature is FeatureType.CAPTIONS:
            base = ["capenc-pool-a"]
            if group.slo_id in ("cap.render_success", "cap.flicker") and pvs:
                base = [f"pv-{p}" for p in pvs]
            if group.slo_id == "cap.availability":
                base = ["capenc-pool-a", "manifest-main"]
            return base
        if feature is FeatureType.AUDIO_DESCRIPTION:
            return ["adsrc-en", "manifest-main"]
        if feature is FeatureType.SIGN_LANGUAGE:
            return ["signsrc-lsf"]
        if feature is FeatureType.ACCESSIBLE_AUTH:
            return ["auth-svc"]
        if feature is FeatureType.ACCESSIBLE_PURCHASE:
            return ["purchase-flow"]
        return [f"pv-{p}" for p in pvs] or ["pv-web-4.12.0"]

    @staticmethod
    def _classify(territories, regions, platforms, providers) -> str:
        if len(territories) >= 6:
            return "systemic"
        if len(providers) > 1 and len(territories) > 3:
            return "provider_wide"
        if len(territories) > 1 or len(regions) > 1:
            return "regional"
        return "local"


# ---------------------------------------------------------------------------
# Multimodal quality agent
# ---------------------------------------------------------------------------


class MultimodalQualityAgent:
    """Compares independent probe outputs and surfaces disagreement."""

    name = "multimodal_quality_agent"

    def run(self, incident: Incident, findings: list[ModelFinding]) -> list[str]:
        notes: list[str] = []
        by_metric: dict[str, list[ModelFinding]] = {}
        for f in findings:
            by_metric.setdefault(f.metric, []).append(f)

        for metric, group in by_metric.items():
            live = [f for f in group if not f.abstained]
            if not live:
                notes.append(
                    f"{metric}: every probe abstained - insufficient data, requesting "
                    "specialist review rather than asserting a value"
                )
                continue
            values = [f.score for f in live]
            spread = max(values) - min(values)
            mean_conf = sum(f.confidence for f in live) / len(live)
            if spread > 0.35 * max(1e-6, abs(max(values))) and len(live) > 1:
                notes.append(
                    f"{metric}: probes disagree ({min(values):.3f}..{max(values):.3f}); "
                    "treating the conservative value as operational and flagging for review"
                )
            if mean_conf < 0.5:
                notes.append(
                    f"{metric}: mean model confidence {mean_conf:.2f} is low; "
                    "not sufficient on its own to justify a production change"
                )
        # cross-modal corroboration
        drift = [f for f in findings if f.metric == "cap.drift" and not f.abstained]
        sign_sync = [f for f in findings if f.metric == "sign.sync" and not f.abstained]
        if drift and sign_sync and drift[0].score > 1.5 and sign_sync[0].score > 0.5:
            notes.append(
                "captions and the interpreter feed drifted together - consistent with a "
                "shared timing reference rather than a caption-specific defect"
            )
        return notes


# ---------------------------------------------------------------------------
# Change correlation agent
# ---------------------------------------------------------------------------


@dataclass
class CausalCandidate:
    change: ChangeEvent
    score: float
    supporting: list[str]
    contradicting: list[str]


class ChangeCorrelationAgent:
    """Ranks change events as causal candidates.

    Deterministic dependency analysis (does the change touch a component the
    symptom depends on?) is combined with temporal proximity and change-point
    alignment. Correlation is never reported as proof: every candidate carries
    both supporting and contradicting evidence, and a counterfactual check.
    """

    name = "change_correlation_agent"

    def __init__(self, twin: DigitalTwin) -> None:
        self.twin = twin

    def run(
        self,
        incident: Incident,
        changes: list[ChangeEvent],
        scope: Scope,
        onset: datetime,
        lookback_minutes: int = 45,
    ) -> list[CausalCandidate]:
        window_start = onset - timedelta(minutes=lookback_minutes)
        upstream = set(self.twin.upstream(list(scope.components)))
        upstream |= set(scope.components)
        candidates: list[CausalCandidate] = []

        for change in changes:
            if not (window_start <= change.at <= onset + timedelta(minutes=2)):
                continue
            supporting: list[str] = []
            contradicting: list[str] = []
            score = 0.0

            # dependency
            if change.component in upstream:
                score += 0.45
                supporting.append(
                    f"{change.component} is upstream of {', '.join(scope.components)}"
                )
            elif change.component in scope.components:
                score += 0.5
                supporting.append(f"{change.component} is the implicated component")
            else:
                contradicting.append(
                    f"{change.component} is not on the delivery path for this symptom"
                )
                score -= 0.25

            # temporal proximity: exponential decay over 20 minutes
            lag_s = max(0.0, (onset - change.at).total_seconds())
            proximity = math.exp(-lag_s / 1200.0)
            score += 0.35 * proximity
            if lag_s <= 300:
                supporting.append(f"occurred {int(lag_s)} s before symptom onset")
            elif lag_s > 1800:
                contradicting.append(
                    f"occurred {int(lag_s / 60)} min before onset, outside the usual latency"
                )

            # change kind priors
            prior = {
                "config": 0.20,
                "deployment": 0.18,
                "provider": 0.12,
                "routing": 0.12,
                "manifest": 0.14,
                "traffic": 0.06,
            }.get(change.kind, 0.05)
            score += prior

            # remediation changes are ours, never a cause
            if change.actor == "raccord.remediation":
                score = -1.0
                contradicting.append("this change is Raccord's own remediation")

            candidates.append(
                CausalCandidate(
                    change=change,
                    score=round(max(0.0, min(1.0, score)), 3),
                    supporting=supporting,
                    contradicting=contradicting,
                )
            )

        candidates.sort(key=lambda c: -c.score)
        return candidates

    @staticmethod
    def counterfactual(candidate: CausalCandidate, scope: Scope) -> str:
        return (
            f"If {candidate.change.component} were not the cause, the symptom would also "
            f"appear on slices that do not depend on it. Observed scope is "
            f"{', '.join(scope.territories) or 'all territories'} on "
            f"{', '.join(scope.player_versions) or 'all builds'} - "
            "check this holds before acting."
        )


# ---------------------------------------------------------------------------
# Diagnosis agent
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Signature:
    failure_class: FailureClass
    feature: FeatureType
    slos: tuple[str, ...]
    log_keywords: tuple[str, ...] = ()
    change_kinds: tuple[str, ...] = ()
    change_components: tuple[str, ...] = ()
    scope_hint: str = ""
    weight: float = 1.0
    statement: str = ""
    # Shape of the SLI over the incident window. A fixed offset and a
    # progressive drift breach the same SLO with the same magnitude; only the
    # trajectory separates them, so it is a first-class discriminator.
    trend_hint: str = ""  # "ramp" | "step" | ""


TAXONOMY: tuple[Signature, ...] = (
    Signature(
        FailureClass.INFRA_CLOCK_SOURCE_CHANGE,
        FeatureType.CAPTIONS,
        ("cap.drift", "sign.sync"),
        ("resync", "clock", "grandmaster", "pts realigned"),
        ("config",),
        ("clock-ptp-primary", "clock-ntp-fallback"),
        weight=1.5,
        statement="A change of timing reference desynchronised the caption chain "
        "from programme audio.",
    ),
    Signature(
        FailureClass.CAPTION_PROGRESSIVE_DRIFT,
        FeatureType.CAPTIONS,
        ("cap.drift",),
        ("resync", "clock", "pts realigned", "offset"),
        ("config", "deployment"),
        ("capenc-pool-a", "clock-ptp-primary"),
        weight=1.4,
        statement="Caption timing is drifting progressively behind spoken dialogue.",
    ),
    Signature(
        FailureClass.CAPTION_CLOCK_OFFSET,
        FeatureType.CAPTIONS,
        ("cap.drift",),
        ("offset", "clock"),
        ("config",),
        ("clock-ptp-primary",),
        weight=0.9,
        trend_hint="step",
        statement="A fixed timing offset was introduced into the caption path.",
    ),
    Signature(
        FailureClass.CAPTION_ENCODER_FAILURE,
        FeatureType.CAPTIONS,
        ("cap.availability", "cap.omission_rate"),
        ("worker exited", "sigsegv", "restart", "crash"),
        ("deployment",),
        ("capenc-pool-a",),
        weight=1.3,
        statement="Caption encoder workers are failing, producing intermittent cues.",
    ),
    Signature(
        FailureClass.CAPTION_SOURCE_LOSS,
        FeatureType.CAPTIONS,
        ("cap.availability",),
        ("source", "no input"),
        (),
        ("capsrc-en", "capsrc-fr"),
        weight=1.0,
        statement="The upstream caption source stopped delivering cues.",
    ),
    Signature(
        FailureClass.CAPTION_MANIFEST_OMISSION,
        FeatureType.CAPTIONS,
        ("cap.availability", "cap.render_success"),
        ("rendition dropped", "track_map", "manifest"),
        ("manifest",),
        ("manifest-main",),
        weight=1.3,
        statement="The promised caption track is no longer declared in the manifest.",
    ),
    Signature(
        FailureClass.CAPTION_WRONG_LANGUAGE,
        FeatureType.CAPTIONS,
        ("cap.wrong_language",),
        ("track_map", "routing"),
        ("routing", "config"),
        ("packager-main", "capenc-pool-a"),
        weight=1.4,
        statement="A routing or track-map change is delivering the wrong language.",
    ),
    Signature(
        FailureClass.CAPTION_RENDER_FAILURE,
        FeatureType.CAPTIONS,
        ("cap.render_success",),
        ("renderer", "layout", "canary"),
        ("deployment",),
        ("pv-ctv-9.4.0", "pv-web-4.12.0"),
        weight=1.3,
        scope_hint="player_version",
        statement="A player build renders cues off-screen; the data is correct.",
    ),
    Signature(
        FailureClass.CAPTION_WORD_DROP,
        FeatureType.CAPTIONS,
        ("cap.omission_rate", "cap.semantic"),
        (),
        ("provider",),
        ("capsrc-en",),
        weight=0.9,
        statement="Cues are losing tokens, degrading semantic preservation.",
    ),
    Signature(
        FailureClass.INFRA_PROVIDER_DEGRADATION,
        FeatureType.CAPTIONS,
        ("cap.drift", "cap.omission_rate", "cap.semantic"),
        ("provider", "migration"),
        ("provider",),
        ("capsrc-en", "cdn-primary"),
        weight=1.2,
        statement="The caption provider is degrading globally.",
    ),
    Signature(
        FailureClass.INFRA_ENCODER_CPU,
        FeatureType.CAPTIONS,
        ("cap.drift", "cap.first_caption_latency"),
        ("cpu", "saturation", "queue_depth"),
        ("traffic",),
        ("capenc-pool-a",),
        weight=1.2,
        trend_hint="ramp",
        statement="Encoder saturation is delaying cue emission.",
    ),
    Signature(
        FailureClass.INFRA_CDN_REGIONAL,
        FeatureType.CAPTIONS,
        ("cap.availability", "sign.availability"),
        ("origin fetch", "503", "region"),
        ("provider", "routing"),
        ("cdn-primary", "region-eu-west"),
        weight=1.3,
        scope_hint="cdn_region",
        statement="One CDN region stopped serving accessibility renditions.",
    ),
    Signature(
        FailureClass.AD_TRACK_OMISSION,
        FeatureType.AUDIO_DESCRIPTION,
        ("ad.track_present", "ad.selection"),
        ("rendition dropped", "manifest"),
        ("manifest",),
        ("manifest-main",),
        weight=1.4,
        statement="The described audio track is missing from the manifest.",
    ),
    Signature(
        FailureClass.AD_SILENT_SEGMENT,
        FeatureType.AUDIO_DESCRIPTION,
        ("ad.audio_present",),
        ("silence", "no audio"),
        (),
        ("adsrc-en", "adsrc-fr"),
        weight=1.3,
        statement="The described track is delivered but carries no audible description.",
    ),
    Signature(
        FailureClass.AD_TIMELINE_DRIFT,
        FeatureType.AUDIO_DESCRIPTION,
        ("ad.drift",),
        ("clock", "resync"),
        ("config",),
        ("clock-ptp-primary",),
        weight=1.2,
        trend_hint="ramp",
        statement="Descriptions are landing over dialogue instead of in the gaps.",
    ),
    Signature(
        FailureClass.AD_WRONG_LANGUAGE,
        FeatureType.AUDIO_DESCRIPTION,
        ("ad.language",),
        ("track_map",),
        ("routing",),
        ("packager-main",),
        weight=1.3,
        statement="The wrong description language is routed onto the track.",
    ),
    Signature(
        FailureClass.AD_LOUDNESS_DEFECT,
        FeatureType.AUDIO_DESCRIPTION,
        ("ad.loudness",),
        ("loudness", "mix"),
        (),
        ("adsrc-en", "adsrc-fr"),
        weight=1.3,
        statement="The described track is mixed outside the loudness window and is "
        "masked by programme dialogue.",
    ),
    Signature(
        FailureClass.AD_CHANNEL_LAYOUT,
        FeatureType.AUDIO_DESCRIPTION,
        ("ad.selection", "ad.audio_present"),
        ("channel", "layout"),
        ("manifest", "config"),
        ("packager-main",),
        weight=1.25,
        statement="The described track is published with a channel layout the "
        "promised players cannot select.",
    ),
    Signature(
        FailureClass.AD_SELECTION_FAILURE,
        FeatureType.AUDIO_DESCRIPTION,
        ("ad.selection",),
        ("assertion", "canary", "build"),
        ("deployment",),
        ("pv-ctv-9.4.0", "pv-web-4.12.0"),
        weight=1.35,
        scope_hint="player_version",
        statement="The audio-track menu lists the described track but selecting it "
        "fails on one player build.",
    ),
    Signature(
        FailureClass.CAPTION_DUPLICATE,
        FeatureType.CAPTIONS,
        ("cap.duplicate", "cap.reading_speed"),
        ("segment published", "manifest"),
        ("manifest", "deployment"),
        ("packager-main",),
        weight=1.1,
        statement="The packager is repeating the previous cue, producing stuttering captions.",
    ),
    Signature(
        FailureClass.CAPTION_READING_SPEED,
        FeatureType.CAPTIONS,
        ("cap.reading_speed", "cap.flicker"),
        (),
        (),
        ("capenc-pool-a",),
        weight=1.05,
        statement="Cue durations are too short to be read at the promised limit.",
    ),
    Signature(
        FailureClass.CAPTION_SPEAKER_CORRUPTION,
        FeatureType.CAPTIONS,
        ("cap.speaker_accuracy",),
        (),
        (),
        ("capenc-pool-a",),
        weight=1.2,
        statement="Speaker attribution labels are being swapped between characters.",
    ),
    Signature(
        FailureClass.CAPTION_FLICKER,
        FeatureType.CAPTIONS,
        ("cap.flicker",),
        ("canary", "renderer"),
        ("deployment",),
        ("pv-ctv-9.4.0",),
        weight=1.2,
        scope_hint="player_version",
        statement="A player build is tearing cues down and re-rendering them within a second.",
    ),
    Signature(
        FailureClass.INFRA_STALE_CONFIG,
        FeatureType.CAPTIONS,
        ("cap.wrong_language",),
        ("track-map", "track_map", "partial", "rollout"),
        ("config",),
        ("capenc-pool-a",),
        weight=1.15,
        statement="One encoder node is still running an old track map after a "
        "partial configuration rollout.",
    ),
    Signature(
        FailureClass.INFRA_PACKET_LOSS,
        FeatureType.CAPTIONS,
        ("cap.availability", "cap.drift"),
        ("origin fetch", "503", "retransmit"),
        ("routing", "provider"),
        ("cdn-primary",),
        weight=1.05,
        scope_hint="cdn_region",
        statement="Transport loss on the contribution link is causing cue gaps and timing spread.",
    ),
    Signature(
        FailureClass.SIGN_BLACK_FRAMES,
        FeatureType.SIGN_LANGUAGE,
        ("sign.black", "sign.availability"),
        ("interpreter feed degraded",),
        (),
        ("signsrc-lsf",),
        weight=1.25,
        statement="The interpreter feed is dropping to black between segments.",
    ),
    Signature(
        FailureClass.SIGN_PIP_OBSTRUCTION,
        FeatureType.SIGN_LANGUAGE,
        ("sign.visibility",),
        ("overlay", "branding"),
        ("deployment",),
        ("pv-web-4.12.0",),
        weight=1.15,
        statement="Burned-in branding is overlapping the interpreter panel.",
    ),
    Signature(
        FailureClass.SIGN_REGIONAL_DELIVERY,
        FeatureType.SIGN_LANGUAGE,
        ("sign.availability",),
        ("origin fetch", "503", "region"),
        ("provider", "routing"),
        ("cdn-primary", "region-eu-west"),
        weight=1.3,
        scope_hint="cdn_region",
        statement="The interpreter rendition is not cached in one CDN region.",
    ),
    Signature(
        FailureClass.INFRA_GPU_SATURATION,
        FeatureType.SIGN_LANGUAGE,
        ("sign.framerate", "sign.frozen"),
        ("gpu_util", "degraded"),
        (),
        ("signsrc-lsf",),
        weight=1.2,
        statement="The interpreter transcode is falling behind a saturated GPU pool.",
    ),
    Signature(
        FailureClass.PLAYER_MISSING_NAME,
        FeatureType.ACCESSIBLE_PLAYER,
        ("player.accessible_name", "player.screen_reader"),
        ("assertion", "build"),
        ("deployment",),
        ("pv-web-4.12.0",),
        weight=1.15,
        statement="Icon-only playback controls have lost their accessible names.",
    ),
    Signature(
        FailureClass.PLAYER_INACCESSIBLE_ERROR,
        FeatureType.ACCESSIBLE_PLAYER,
        ("player.screen_reader",),
        ("assertion", "canary"),
        ("deployment",),
        ("pv-ctv-9.4.0",),
        weight=1.05,
        scope_hint="player_version",
        statement="Playback errors render visually but are never announced to "
        "assistive technology.",
    ),
    Signature(
        FailureClass.SIGN_FROZEN_FRAMES,
        FeatureType.SIGN_LANGUAGE,
        ("sign.frozen", "sign.availability"),
        ("interpreter feed degraded", "freeze"),
        (),
        ("signsrc-lsf",),
        weight=1.3,
        statement="The interpreter feed is freezing on repeated frames.",
    ),
    Signature(
        FailureClass.SIGN_LOW_FRAMERATE,
        FeatureType.SIGN_LANGUAGE,
        ("sign.framerate",),
        ("fps", "gpu_util", "degraded"),
        ("deployment",),
        ("signsrc-lsf",),
        weight=1.3,
        statement="The interpreter feed frame rate has collapsed below readability.",
    ),
    Signature(
        FailureClass.SIGN_CROP_FAILURE,
        FeatureType.SIGN_LANGUAGE,
        ("sign.visibility",),
        ("aspect", "crop"),
        ("config",),
        ("signsrc-lsf",),
        weight=1.2,
        statement="A framing change is cropping the interpreter's signing space.",
    ),
    Signature(
        FailureClass.SIGN_SYNC_DRIFT,
        FeatureType.SIGN_LANGUAGE,
        ("sign.sync",),
        ("clock", "resync"),
        ("config",),
        ("clock-ptp-primary",),
        weight=1.2,
        trend_hint="ramp",
        statement="The interpreter feed has lost synchronisation with programme audio.",
    ),
    Signature(
        FailureClass.INFRA_DEPLOY_REGRESSION,
        FeatureType.ACCESSIBLE_PLAYER,
        (
            "player.accessible_name",
            "player.caption_control",
            "player.screen_reader",
            "cap.render_success",
        ),
        ("canary", "assertion failed", "build"),
        ("deployment",),
        ("pv-ctv-9.4.0", "pv-web-4.12.0"),
        weight=1.5,
        scope_hint="player_version",
        statement="A player deployment regressed several accessibility behaviours.",
    ),
    Signature(
        FailureClass.PLAYER_KEYBOARD_TRAP,
        FeatureType.ACCESSIBLE_PLAYER,
        ("player.keyboard", "player.caption_control"),
        ("focus", "trap", "assertion"),
        ("deployment",),
        ("pv-web-4.12.0",),
        weight=1.3,
        statement="Keyboard focus cannot leave the caption dialog.",
    ),
    Signature(
        FailureClass.PLAYER_SCREEN_READER,
        FeatureType.ACCESSIBLE_PLAYER,
        ("player.screen_reader",),
        ("live region", "assertion"),
        ("deployment",),
        ("pv-web-4.12.0",),
        weight=1.1,
        statement="A screen-reader incompatibility blocks the playback journey.",
    ),
    Signature(
        FailureClass.PLAYER_FOCUS_LOSS,
        FeatureType.ACCESSIBLE_PLAYER,
        ("player.focus_visible",),
        ("focus",),
        ("deployment",),
        ("pv-web-4.12.0",),
        weight=1.0,
        statement="The visible focus indicator was removed.",
    ),
    Signature(
        FailureClass.PLAYER_REDUCED_MOTION,
        FeatureType.ACCESSIBLE_PLAYER,
        ("player.reduced_motion",),
        ("motion", "animation"),
        ("deployment",),
        ("pv-web-4.12.0",),
        weight=1.0,
        statement="prefers-reduced-motion is being ignored.",
    ),
    Signature(
        FailureClass.PLAYER_AUTH_FAILURE,
        FeatureType.ACCESSIBLE_AUTH,
        ("auth.completion",),
        ("challenge", "a11y_fallback"),
        ("deployment",),
        ("auth-svc",),
        weight=1.4,
        statement="A new authentication challenge has no accessible alternative.",
    ),
    Signature(
        FailureClass.PLAYER_PURCHASE_FAILURE,
        FeatureType.ACCESSIBLE_PURCHASE,
        ("purchase.completion",),
        ("label", "form"),
        ("deployment",),
        ("purchase-flow",),
        weight=1.3,
        statement="The purchase form lost its labels.",
    ),
)


class DiagnosisAgent:
    """Maps evidence onto the formal failure taxonomy and ranks hypotheses.

    Never invokes remediation. Abstains when the winning posterior is too low or
    the evidence is too thin, in which case the incident stops at DIAGNOSED and
    escalates to a human.
    """

    name = "diagnosis_agent"
    ABSTAIN_BELOW = 0.28
    ABSTAIN_MARGIN = 0.06

    @staticmethod
    def sli_trend(evidence: list[Evidence]) -> str:
        """Classify the breached SLI's trajectory over the incident window.

        Reads the sample series that came back through `query_prometheus`. A
        fixed clock offset and a progressive drift breach the same objective by
        the same amount; only the shape tells them apart, so this is computed
        from evidence rather than assumed.
        """
        samples: list[float] = []
        for e in evidence:
            if e.kind is not EvidenceKind.PROM_QUERY:
                continue
            for series in e.payload.get("result") or []:
                points = series.get("samples") or []
                if len(points) >= 6:
                    samples = [float(v) for _, v in points]
                    break
            if samples:
                break
        if len(samples) < 6:
            return "unknown"
        span = max(samples) - min(samples)
        if span < 1e-6:
            return "flat"
        third = max(2, len(samples) // 3)
        head = sum(samples[:third]) / third
        tail = sum(samples[-third:]) / third
        mid = sum(samples[third:-third]) / max(1, len(samples) - 2 * third)
        rising = (tail - head) > 0.35 * span
        # A step lands early and then holds: the middle already looks like the end.
        stepped = abs(tail - mid) < 0.15 * span and abs(mid - head) > 0.5 * span
        if stepped:
            return "step"
        if rising:
            return "ramp"
        return "flat"

    def run(
        self,
        incident: Incident,
        scope: Scope,
        breached_slos: set[str],
        evidence: list[Evidence],
        causal: list[CausalCandidate],
    ) -> list[Hypothesis]:
        log_text = " ".join(
            str(e.payload) for e in evidence if e.kind.value in ("loki_query", "grafana_alert")
        ).lower()
        change_kinds = {c.change.kind for c in causal[:3]}
        change_components = {c.change.component for c in causal[:3]}
        trend = self.sli_trend(evidence)

        scored: list[tuple[Signature, float, list[str], list[str]]] = []
        for sig in TAXONOMY:
            if sig.feature not in scope.features:
                continue
            supporting: list[str] = []
            contradicting: list[str] = []
            matched = breached_slos & set(sig.slos)
            if not matched:
                continue
            slo_cover = len(matched) / len(sig.slos)
            score = 0.9 * slo_cover
            supporting.append(f"breached SLOs match signature: {sorted(matched)}")
            missing = set(sig.slos) - breached_slos
            if missing:
                contradicting.append(f"signature also expects {sorted(missing)} to breach")
                score -= 0.12 * len(missing) / len(sig.slos)

            kw_hits = [k for k in sig.log_keywords if k in log_text]
            if sig.log_keywords:
                if kw_hits:
                    score += 0.5 * len(kw_hits) / len(sig.log_keywords)
                    supporting.append(f"log evidence contains {kw_hits}")
                else:
                    contradicting.append(f"no log line mentions {list(sig.log_keywords)[:3]}")
                    score -= 0.15

            if sig.change_components:
                overlap = change_components & set(sig.change_components)
                if overlap:
                    score += 0.55
                    supporting.append(f"a change on {sorted(overlap)} precedes onset")
                else:
                    score -= 0.1
            if sig.change_kinds and change_kinds & set(sig.change_kinds):
                score += 0.2
                supporting.append(f"change kind matches ({sorted(change_kinds)})")

            if (
                sig.scope_hint == "player_version"
                and len(scope.player_versions) in (1, 2)
                and len(scope.player_versions) < 5
            ):
                score += 0.25
                supporting.append(f"symptom is confined to build(s) {list(scope.player_versions)}")
            if sig.scope_hint == "cdn_region" and len(scope.cdn_regions) == 1:
                score += 0.25
                supporting.append(f"symptom is confined to region {scope.cdn_regions[0]}")

            if sig.trend_hint and trend != "unknown":
                if sig.trend_hint == trend:
                    score += 0.4
                    supporting.append(
                        f"the SLI trajectory is a {trend}, which matches this signature"
                    )
                else:
                    score -= 0.35
                    contradicting.append(
                        f"signature expects a {sig.trend_hint} but the SLI shows a {trend}"
                    )

            scored.append((sig, score * sig.weight, supporting, contradicting))

        if not scored:
            return [
                Hypothesis(
                    hypothesis_id=f"hyp-{uuid.uuid4().hex[:8]}",
                    failure_class=FailureClass.UNKNOWN,
                    statement="No taxonomy signature matches the observed evidence.",
                    rank=1,
                    posterior=0.0,
                    supporting_evidence_ids=tuple(e.evidence_id for e in evidence[:3]),
                    uncertainty_note="Escalating to a human: the failure mode is not in the taxonomy.",
                    abstained=True,
                )
            ]

        # softmax over the scores
        raw = [max(0.01, s) for _, s, _, _ in scored]
        exps = [math.exp(r * 2.2) for r in raw]
        total = sum(exps)
        ranked = sorted(
            [(sig, e / total, sup, con) for (sig, _, sup, con), e in zip(scored, exps)],
            key=lambda t: -t[1],
        )

        hypotheses: list[Hypothesis] = []
        ev_ids = [e.evidence_id for e in evidence]
        for i, (sig, posterior, supporting, contradicting) in enumerate(ranked[:4], start=1):
            causal_id = None
            if (
                causal
                and sig.change_components
                and causal[0].change.component in sig.change_components
            ):
                causal_id = causal[0].change.change_id
            # Abstain only when the leading hypothesis is both weak *and* not
            # meaningfully ahead of the runner-up. A confident single match is
            # not uncertainty; a three-way tie is.
            margin = posterior - (ranked[1][1] if len(ranked) > 1 else 0.0)
            abstain = i == 1 and posterior < self.ABSTAIN_BELOW and margin < self.ABSTAIN_MARGIN
            hypotheses.append(
                Hypothesis(
                    hypothesis_id=f"hyp-{uuid.uuid4().hex[:8]}",
                    failure_class=sig.failure_class,
                    statement=sig.statement,
                    rank=i,
                    posterior=round(posterior, 3),
                    supporting_evidence_ids=tuple(ev_ids[:6]),
                    contradicting_evidence_ids=tuple(ev_ids[6:8]),
                    causal_change_id=causal_id,
                    uncertainty_note="; ".join(
                        supporting + [f"counter: {c}" for c in contradicting]
                    ),
                    abstained=abstain,
                )
            )
        return hypotheses


# ---------------------------------------------------------------------------
# Communication agent
# ---------------------------------------------------------------------------


class CommunicationAgent:
    """Produces role-specific output from the approved incident record only."""

    name = "communication_agent"

    def run(self, incident: Incident, recovered: bool) -> list[Communication]:
        scope = incident.scope
        top = incident.hypotheses[0] if incident.hypotheses else None
        action = incident.proposed_action
        assert scope is not None
        features = ", ".join(f.value.replace("_", " ") for f in scope.features)
        territories = ", ".join(scope.territories) or "all territories"
        languages = ", ".join(scope.languages) or "all languages"
        out: list[Communication] = []

        def add(
            audience: str, subject: str, body: str, internal: bool = True, reading: str = ""
        ) -> None:
            out.append(
                Communication(
                    communication_id=f"com-{uuid.uuid4().hex[:8]}",
                    incident_id=incident.incident_id,
                    audience=audience,  # type: ignore[arg-type]
                    subject=subject,
                    body=body.strip(),
                    reading_level_note=reading,
                    contains_internal_detail=internal,
                )
            )

        add(
            "operator",
            f"[{incident.severity.value}] {incident.title}",
            f"""
Feature: {features} ({languages})
Scope: {territories}; builds {", ".join(scope.player_versions) or "all"}
Diagnosis: {top.statement if top else "pending"} (posterior {top.posterior if top else 0:.2f})
Action: {action.action_type.value if action else "none"} on {action.target if action else "-"}
State: {incident.state.value}
Sessions affected: {scope.affected_sessions:,} | protected: {scope.protected_sessions:,}
""",
        )

        add(
            "accessibility_specialist",
            f"Accessibility impact: {features}",
            f"""
Promises violated: {", ".join(scope.violated_promise_ids)}
Observed failure: {top.statement if top else "pending"}
Evidence: {len(incident.evidence)} items, {len(incident.findings)} model findings
Model confidence notes: {top.uncertainty_note[:280] if top else "n/a"}
Verification: {sum(1 for a in incident.assertions if a.status.value == "passing")}/{len(incident.assertions)} assertions passing
""",
        )

        add(
            "technical_director",
            f"Decision required: {incident.title}",
            f"""
Blast class: {scope.blast_class}. {scope.affected_sessions:,} accessibility-enabled sessions affected.
Proposed: {action.expected_effect if action else "none"}
Risk: reversible, {action.rollback_behaviour if action else "n/a"}
Approval authority: {", ".join(r.value for r in incident.policy_decision.required_roles) if incident.policy_decision else "n/a"}
""",
        )

        add(
            "viewer_support",
            "Viewer support guidance",
            f"""
What viewers may notice: {_plain_symptom(scope)}.
Where: {territories}. Languages: {languages}.
What to tell viewers: we have identified the cause and {"restored the feature" if recovered else "are restoring the feature now"}. No action is needed from them; reselecting the feature in the player menu will pick up the fix.
Do not ask viewers to describe their disability or assistive technology.
""",
        )

        add(
            "executive",
            "Accessibility incident summary",
            f"""
{features.capitalize()} were degraded in {territories} for {_duration(incident)}.
{scope.affected_sessions:,} accessibility-enabled sessions were affected; {scope.protected_sessions:,} were protected.
Cause: {top.statement if top else "under investigation"}
Status: {"restored and verified" if recovered else incident.state.value}
Error budget consumed: {incident.error_budget_consumed:.1%}
""",
        )

        add(
            "public_status",
            f"{_feature_label(scope)} on {territories}",
            _public_status_body(scope, recovered),
            internal=False,
            reading="Plain language, one idea per sentence, no jargon, no colour-only status; "
            "written to be read aloud by a screen reader.",
        )

        add(
            "post_incident",
            f"Post-incident review: {incident.title}",
            f"""
Root cause: {top.failure_class.value if top else "unknown"}
Audience impact (programme clock): degraded for {_duration(incident)}; detected {incident.timings.get("time_to_detect_s", 0):.1f}s after onset.
Agent working time (wall clock): scope {incident.timings.get("time_to_scope_s", 0):.1f}s | evidence {incident.timings.get("time_to_evidence_s", 0):.1f}s | approval {incident.timings.get("time_to_approval_s", 0):.1f}s | verified recovery {incident.timings.get("time_to_recovery_s", 0):.1f}s
MCP calls: {incident.timings.get("mcp_calls", 0):.0f}
""",
        )
        return out


def _plain_symptom(scope: Scope) -> str:
    f = scope.features[0]
    return {
        FeatureType.CAPTIONS: "captions appearing later than the speech, or not appearing",
        FeatureType.AUDIO_DESCRIPTION: "the described audio track missing or silent",
        FeatureType.ALTERNATE_AUDIO: "an audio language missing from the menu",
        FeatureType.SIGN_LANGUAGE: "the sign-language window freezing or missing",
        FeatureType.ACCESSIBLE_PLAYER: "playback controls that cannot be reached or operated",
        FeatureType.ACCESSIBLE_AUTH: "being unable to complete sign-in",
        FeatureType.ACCESSIBLE_PURCHASE: "being unable to complete a ticket purchase",
    }.get(f, "an accessibility feature not working as promised")


def _feature_label(scope: Scope) -> str:
    return {
        FeatureType.CAPTIONS: "Captions",
        FeatureType.SUBTITLES: "Subtitles",
        FeatureType.AUDIO_DESCRIPTION: "Audio description",
        FeatureType.ALTERNATE_AUDIO: "Alternate audio",
        FeatureType.SIGN_LANGUAGE: "The sign-language window",
        FeatureType.ACCESSIBLE_PLAYER: "The playback controls",
        FeatureType.ACCESSIBLE_AUTH: "Sign-in",
        FeatureType.ACCESSIBLE_PURCHASE: "Ticket purchase",
    }[scope.features[0]]


def _is_plural(scope: Scope) -> bool:
    return scope.features[0] in (
        FeatureType.CAPTIONS,
        FeatureType.SUBTITLES,
        FeatureType.ACCESSIBLE_PLAYER,
    )


def _public_status_body(scope: Scope, recovered: bool) -> str:
    what = _plain_symptom(scope)
    where = ", ".join(scope.territories) or "several regions"
    verb = "are" if _is_plural(scope) else "is"
    if recovered:
        return (
            f"{_feature_label(scope)} {verb} working again.\n\n"
            f"Earlier today some viewers in {where} experienced {what}.\n"
            "We found the cause and fixed it. We checked the fix on the affected devices "
            "and languages.\n"
            "If you still have a problem, close the player and open it again, then select "
            "the feature from the player menu.\n"
            f"Updated: {utcnow().strftime('%H:%M UTC on %d %B %Y')}."
        )
    return (
        f"{_feature_label(scope)} {verb} not working correctly for some viewers.\n\n"
        f"Viewers in {where} may notice {what}.\n"
        "We know about this and we are fixing it now.\n"
        "You do not need to do anything. We will update this page when it is fixed.\n"
        f"Updated: {utcnow().strftime('%H:%M UTC on %d %B %Y')}."
    )


def _duration(incident: Incident) -> str:
    """How long the audience was affected, on the programme clock.

    Deliberately *not* ``time_to_recovery_s``: that is a stopwatch on the agent's
    own work, in wall-clock seconds, so dividing it by 60 reported a real
    multi-minute degradation to an executive as "0.0 minutes".
    """
    outage = incident.timings.get("outage_seconds")
    if not outage:
        return "an ongoing period"
    if outage < 90:
        return f"{outage:.0f} seconds"
    return f"{outage / 60:.1f} minutes"


# ---------------------------------------------------------------------------
# Reliability learning agent
# ---------------------------------------------------------------------------


class ReliabilityLearningAgent:
    """Turns a resolved incident into measurements and proposals.

    Proposals are advisory. This agent cannot change production policy, alert
    rules or thresholds; it emits a review record that a human must accept.
    """

    name = "reliability_learning_agent"

    def run(
        self,
        incident: Incident,
        ground_truth_class: FailureClass | None = None,
        mcp_calls: int = 0,
        expected_mcp_calls: int = 13,
    ) -> PostIncidentReview:
        top = incident.hypotheses[0] if incident.hypotheses else None
        correct = bool(ground_truth_class and top and top.failure_class == ground_truth_class)
        missed: list[str] = []
        if incident.timings.get("time_to_detect_s", 0) > 30:
            missed.append("detection exceeded the 30 s operational objective")
        abstained = [f.metric for f in incident.findings if f.abstained]
        if abstained:
            missed.append(f"probes abstained on {sorted(set(abstained))}")
        if not any(e.kind.value == "pyroscope_profile" for e in incident.evidence):
            missed.append("no profile evidence collected; add it for saturation-class faults")

        proposals: list[str] = []
        if top and top.posterior < 0.5:
            proposals.append(
                f"add discriminating signals for {top.failure_class.value}: the winning "
                f"posterior was only {top.posterior:.2f}"
            )
        if incident.timings.get("time_to_approval_s", 0) > 120:
            proposals.append(
                "pre-authorise this action class for tier-0 caption drift so approval is a "
                "confirmation rather than a decision"
            )
        if mcp_calls > expected_mcp_calls:
            proposals.append(
                f"{mcp_calls} MCP calls for a {expected_mcp_calls}-call investigation: "
                "cache the alert-rule read across agents"
            )
        proposals.append("add this scenario to the benchmark corpus with its ground-truth class")

        return PostIncidentReview(
            incident_id=incident.incident_id,
            root_cause=(top.failure_class if top else FailureClass.UNKNOWN),
            contributing_factors=tuple(c.description for c in incident.changes[:3]),
            time_to_detect_s=incident.timings.get("time_to_detect_s", 0.0),
            outage_seconds=incident.timings.get("outage_seconds", 0.0),
            time_to_scope_s=incident.timings.get("time_to_scope_s", 0.0),
            time_to_evidence_s=incident.timings.get("time_to_evidence_s", 0.0),
            time_to_approval_s=incident.timings.get("time_to_approval_s", 0.0),
            time_to_recovery_s=incident.timings.get("time_to_recovery_s", 0.0),
            error_budget_consumed=incident.error_budget_consumed,
            affected_sessions=incident.scope.affected_sessions if incident.scope else 0,
            protected_sessions=incident.scope.protected_sessions if incident.scope else 0,
            missed_signals=tuple(missed),
            unnecessary_tool_calls=max(0, mcp_calls - expected_mcp_calls),
            diagnosis_correct=correct,
            verification_complete=bool(incident.assertions)
            and all(a.status.value == "passing" for a in incident.assertions if a.mandatory),
            proposed_improvements=tuple(proposals),
        )
