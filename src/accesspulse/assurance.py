"""Live assurance: turn probe findings into SLO state, error-budget burn and alerts.

This is the detection layer. It sweeps the audience slice matrix, evaluates each
probe finding against the promise that was effective at that moment, burns error
budget, and raises structured alerts - the same alerts Grafana Alerting fires
from the recording rules generated in `observability/`.

Detection is deterministic and evidence-first: an alert always names the SLO,
the observed value, the objective, the slice matrix it covers and the model
findings that produced it. No language model is involved in deciding that
something is broken.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from .contracts import (
    Alert,
    FeatureType,
    SLOTier,
)
from .probes import ProbeReport
from .registry import PromiseRegistry
from .simulator import MediaSimulator, SliceObservation, _platform_of
from .slo import SLO_BY_ID, ErrorBudgetLedger, SLODefinition, burn_severity
from .twin import LANGUAGES, PLAYER_VERSIONS, TERRITORIES


@dataclass
class SLOEvaluation:
    slo: SLODefinition
    observed: float
    threshold: float
    breached: bool
    confidence: float
    abstained: bool
    slice_key: str
    labels: dict[str, str]
    finding_id: str | None
    magnitude: float  # how far past the objective, 1.0 == exactly at it

    @property
    def slo_id(self) -> str:
        return self.slo.slo_id


@dataclass
class BreachGroup:
    """Breaches of one SLO collapsed across the slices that share it."""

    slo_id: str
    feature: FeatureType
    evaluations: list[SLOEvaluation] = field(default_factory=list)

    @property
    def worst(self) -> SLOEvaluation:
        return max(self.evaluations, key=lambda e: e.magnitude)

    def dimension(self, key: str) -> list[str]:
        return sorted({e.labels.get(key, "") for e in self.evaluations if e.labels.get(key)})


def evaluate_report(
    report: ProbeReport,
    tier: SLOTier,
    min_confidence: float = 0.3,
) -> list[SLOEvaluation]:
    out: list[SLOEvaluation] = []
    for f in report.findings:
        slo = SLO_BY_ID.get(f.metric)
        if slo is None:
            continue
        thr = slo.threshold(tier)
        if f.abstained or f.confidence < min_confidence:
            out.append(SLOEvaluation(slo, f.score, thr, False, f.confidence, True,
                                     report.slice_key, report.labels, f.finding_id, 0.0))
            continue
        breached = slo.breached(f.score, tier)
        if slo.comparator.value == "lower_is_better":
            magnitude = (f.score / thr) if thr else float(f.score > 0)
        else:
            magnitude = ((1.0 - f.score) / max(1e-6, 1.0 - thr)) if thr < 1.0 else \
                (0.0 if f.score >= thr else 4.0)
        out.append(SLOEvaluation(slo, f.score, thr, breached, f.confidence, False,
                                 report.slice_key, report.labels, f.finding_id,
                                 round(magnitude, 3)))
    return out


class LiveAssurance:
    """Continuously evaluates the delivered experience across the slice matrix."""

    def __init__(
        self,
        sim: MediaSimulator,
        registry: PromiseRegistry,
        event_id: str = "evt-lumiere-premiere",
        tier: SLOTier = SLOTier.TIER_0_GLOBAL_LIVE,
    ) -> None:
        self.sim = sim
        self.registry = registry
        self.event_id = event_id
        self.tier = tier
        self.ledger = ErrorBudgetLedger(tier)
        self.last_evaluations: list[SLOEvaluation] = []
        self.last_reports: list[tuple[SliceObservation, list[ProbeReport]]] = []
        self.breach_since: dict[str, datetime] = {}
        self.fired_alerts: list[Alert] = []
        self.metrics: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}

    # -- sweep -------------------------------------------------------------
    def sweep(
        self,
        languages: list[str] | None = None,
        territories: list[str] | None = None,
        player_versions: list[str] | None = None,
        window_s: float = 30.0,
    ) -> list[SLOEvaluation]:
        from . import probes

        evaluations: list[SLOEvaluation] = []
        collected: list[tuple[SliceObservation, list[ProbeReport]]] = []
        self.metrics.clear()

        for language in languages or LANGUAGES:
            for territory in territories or TERRITORIES:
                for pv in player_versions or PLAYER_VERSIONS:
                    platform = _platform_of(pv)
                    obs = self.sim.observe(language, territory, platform, pv, window_s)
                    reports = probes.run_all(obs, language)
                    collected.append((obs, reports))
                    for r in reports:
                        promised = self.registry.matching(
                            self.event_id,
                            feature=_feature_of(r.probe),
                            language=language if r.probe != "sign_language" else "fr-lsf",
                            territory=territory,
                            at=self.sim.wall_clock,
                        )
                        if not promised and r.probe in ("sign_language", "audio_description"):
                            continue
                        tier = promised[0].slo_tier if promised else self.tier
                        evaluations.extend(evaluate_report(r, tier))
                        for name, value in r.metrics.items():
                            key = (name, tuple(sorted(r.labels.items())))
                            self.metrics[key] = value

        self.last_evaluations = evaluations
        self.last_reports = collected
        return evaluations

    # -- breach handling ---------------------------------------------------
    def breaches(self) -> dict[str, BreachGroup]:
        groups: dict[str, BreachGroup] = {}
        for e in self.last_evaluations:
            if not e.breached:
                continue
            g = groups.setdefault(e.slo_id, BreachGroup(e.slo_id, e.slo.feature))
            g.evaluations.append(e)
        return groups

    def burn(self, seconds: float) -> None:
        for slo_id in self.breaches():
            self.ledger.consume(slo_id, seconds)

    def raise_alerts(self, now: datetime | None = None) -> list[Alert]:
        """Structured alerts, shaped exactly like the Grafana Alerting payloads."""
        now = now or self.sim.wall_clock
        new_alerts: list[Alert] = []
        groups = self.breaches()
        for slo_id, group in groups.items():
            self.breach_since.setdefault(slo_id, now)
            worst = group.worst
            consumed = self.ledger.get(slo_id).consumed_fraction
            severity = burn_severity(consumed, worst.magnitude)
            labels = {
                "slo": slo_id,
                "feature": group.feature.value,
                "event": self.event_id,
                "language": ",".join(group.dimension("language")) or "all",
                "territory": ",".join(group.dimension("territory")) or "all",
                "platform": ",".join(group.dimension("platform")) or "all",
                "player_version": ",".join(group.dimension("player_version")) or "all",
                "cdn_region": ",".join(group.dimension("cdn_region")) or "all",
                "severity": severity.value,
                "slices_affected": str(len(group.evaluations)),
            }
            alert = Alert(
                alert_id=f"alrt-{uuid.uuid4().hex[:10]}",
                rule_uid=f"accesspulse-{slo_id.replace('.', '-')}",
                rule_title=f"{worst.slo.name} outside objective",
                state="firing",
                severity=severity,
                fired_at=self.breach_since[slo_id],
                labels=labels,
                annotations={
                    "summary": (
                        f"{worst.slo.name}: observed {worst.observed:.3f}{worst.slo.unit} "
                        f"against objective {worst.threshold:.3f}{worst.slo.unit} "
                        f"on {len(group.evaluations)} slice(s)"
                    ),
                    "description": worst.slo.description,
                    "error_budget_consumed": f"{consumed:.1%}",
                    "runbook": f"https://runbooks.accesspulse.local/{slo_id}",
                },
                value=worst.observed,
            )
            new_alerts.append(alert)
        # resolve alerts whose SLO is no longer breached
        for slo_id in list(self.breach_since):
            if slo_id not in groups:
                del self.breach_since[slo_id]
        self.fired_alerts = new_alerts
        return new_alerts

    # -- readouts ----------------------------------------------------------
    def health_summary(self) -> dict[str, dict]:
        by_feature: dict[str, dict] = defaultdict(
            lambda: {"evaluated": 0, "breached": 0, "abstained": 0, "worst_slo": None,
                     "worst_magnitude": 0.0}
        )
        for e in self.last_evaluations:
            row = by_feature[e.slo.feature.value]
            row["evaluated"] += 1
            if e.abstained:
                row["abstained"] += 1
            if e.breached:
                row["breached"] += 1
                if e.magnitude > row["worst_magnitude"]:
                    row["worst_magnitude"] = e.magnitude
                    row["worst_slo"] = e.slo_id
        return dict(by_feature)

    def affected_sessions(self, group: BreachGroup) -> tuple[int, int]:
        """(affected, protected) accessibility-enabled session aggregates."""
        affected = 0
        protected = 0
        breached_slices = {e.slice_key for e in group.evaluations}
        for obs, _ in self.last_reports:
            key = f"{obs.language}/{obs.territory}/{obs.platform}/{obs.player_version}"
            n = {
                FeatureType.CAPTIONS: obs.sessions_with_captions,
                FeatureType.AUDIO_DESCRIPTION: obs.sessions_with_description,
                FeatureType.ALTERNATE_AUDIO: obs.sessions_with_description,
                FeatureType.SIGN_LANGUAGE: obs.sessions_with_sign,
            }.get(group.feature, obs.sessions_with_captions)
            if key in breached_slices:
                affected += n
            else:
                protected += n
        return affected, protected


def _feature_of(probe_name: str) -> FeatureType:
    return {
        "caption": FeatureType.CAPTIONS,
        "audio_description": FeatureType.AUDIO_DESCRIPTION,
        "sign_language": FeatureType.SIGN_LANGUAGE,
        "player_synthetic": FeatureType.ACCESSIBLE_PLAYER,
    }[probe_name]


__all__ = [
    "SLOEvaluation",
    "BreachGroup",
    "LiveAssurance",
    "evaluate_report",
]
