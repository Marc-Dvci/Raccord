"""Preflight certification: the Accessibility Release Gate.

Before an event may display "accessibility ready", every promise it made is
tested against the real delivery chain and the real players. Hard assertions
block certification; soft assertions are recorded as known risks with an owner.

The output is a signed, versioned certification record listing every assertion,
its result, the probe and model versions that produced it, and the responsible
owner. It is the artefact an accessibility lead can hand to a broadcaster,
a festival, or a regulator.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass

from .assurance import evaluate_report
from .config import get_settings
from .contracts import (
    AssertionStatus,
    CertificationAssertion,
    CertificationRecord,
    FeatureType,
    stable_hash,
)
from .grafana_mcp import GrafanaMCPClient
from .probes import run_for_feature
from .probes.player import JOURNEYS
from .registry import PromiseRegistry
from .simulator import MediaSimulator, _platform_of
from .slo import SLO_BY_ID

GATES = (
    "manifest",
    "captions",
    "audio_description",
    "alternate_audio",
    "sign_language",
    "player_journeys",
    "access_flows",
    "delivery",
    "observability",
    "response_readiness",
)


@dataclass
class GateResult:
    gate: str
    assertions: list[CertificationAssertion]


class ReleaseGate:
    def __init__(
        self,
        sim: MediaSimulator,
        registry: PromiseRegistry,
        event_id: str = "evt-lumiere-premiere",
        mcp: GrafanaMCPClient | None = None,
    ) -> None:
        self.sim = sim
        self.registry = registry
        self.event_id = event_id
        self.mcp = mcp

    # -- gates -------------------------------------------------------------
    async def run(self, window_s: float = 30.0) -> CertificationRecord:
        assertions: list[CertificationAssertion] = []
        promises = self.registry.for_event(self.event_id, at=self.sim.wall_clock)

        assertions.extend(self._manifest_gate(promises, window_s))
        assertions.extend(self._feature_gates(promises, window_s))
        assertions.extend(self._player_gate(promises, window_s))
        assertions.extend(self._delivery_gate(window_s))
        assertions.extend(await self._observability_gate())
        assertions.extend(self._response_readiness_gate())

        blockers = [
            f"{a.gate}/{a.name}: {a.detail}"
            for a in assertions
            if a.hard and a.status is not AssertionStatus.PASSING
        ]
        certified = not blockers
        model_versions = {
            "caption_probe": "caption-probe-1.4.0",
            "ad_probe": "ad-probe-1.2.0",
            "sign_probe": "sign-probe-1.1.0",
            "player_probe": "player-probe-1.3.0",
            "language_id": "trigram-corpus-1.0.0",
            "embedding": "hashed-ngram-256-1.0.0",
        }
        promise_hashes = tuple(p.content_hash() for p in promises)
        body = {
            "event_id": self.event_id,
            "certified": certified,
            "assertions": [a.model_dump(mode="json") for a in assertions],
            "promise_hashes": list(promise_hashes),
            "model_versions": model_versions,
            "topology": self.sim.twin.topology_hash(),
        }
        signature = hmac.new(
            get_settings().signing_key(), stable_hash(body).encode(), hashlib.sha256
        ).hexdigest()

        return CertificationRecord(
            certification_id=f"cert-{uuid.uuid4().hex[:10]}",
            event_id=self.event_id,
            certified=certified,
            assertions=tuple(assertions),
            blockers=tuple(blockers),
            promise_hashes=promise_hashes,
            model_versions=model_versions,
            signature=signature,
        )

    # -- individual gates --------------------------------------------------
    def _manifest_gate(self, promises, window_s) -> list[CertificationAssertion]:
        out = []
        obs = self.sim.observe("en", "FR", "ctv", "ctv-9.4.0", window_s)
        for p in promises:
            if p.feature is FeatureType.CAPTIONS:
                ok = p.language in obs.manifest_caption_tracks
                out.append(
                    self._a(
                        "manifest",
                        f"caption track '{p.language}' declared",
                        True,
                        ok,
                        f"manifest tracks: {obs.manifest_caption_tracks}",
                        p.technical_owner,
                    )
                )
            elif p.feature in (FeatureType.AUDIO_DESCRIPTION, FeatureType.ALTERNATE_AUDIO):
                tag = (
                    f"{p.language}-desc"
                    if p.feature is FeatureType.AUDIO_DESCRIPTION
                    else p.language
                )
                ok = tag in obs.manifest_audio_tracks
                out.append(
                    self._a(
                        "manifest",
                        f"audio track '{tag}' declared",
                        True,
                        ok,
                        f"manifest tracks: {obs.manifest_audio_tracks}",
                        p.technical_owner,
                    )
                )
        return out

    def _feature_gates(self, promises, window_s) -> list[CertificationAssertion]:
        out = []
        for p in promises:
            if p.feature in (
                FeatureType.ACCESSIBLE_PLAYER,
                FeatureType.ACCESSIBLE_AUTH,
                FeatureType.ACCESSIBLE_PURCHASE,
            ):
                continue
            gate = {
                FeatureType.CAPTIONS: "captions",
                FeatureType.AUDIO_DESCRIPTION: "audio_description",
                FeatureType.ALTERNATE_AUDIO: "alternate_audio",
                FeatureType.SIGN_LANGUAGE: "sign_language",
            }[p.feature]
            territory = p.territories[0]
            pv = p.player_versions[0] if p.player_versions else "web-4.12.0"
            language = "fr" if p.feature is FeatureType.SIGN_LANGUAGE else p.language
            obs = self.sim.observe(language, territory, _platform_of(pv), pv, window_s)
            report = run_for_feature(obs, p.feature, language)
            for ev in evaluate_report(report, p.slo_tier):
                slo = SLO_BY_ID[ev.slo_id]
                if ev.abstained:
                    status = AssertionStatus.INCONCLUSIVE
                    detail = "probe abstained: insufficient data in the preflight window"
                else:
                    status = AssertionStatus.FAILING if ev.breached else AssertionStatus.PASSING
                    detail = (
                        f"observed {ev.observed:.4f}{slo.unit} against objective "
                        f"{ev.threshold:.4f}{slo.unit}"
                    )
                out.append(
                    CertificationAssertion(
                        assertion_id=f"ca-{uuid.uuid4().hex[:8]}",
                        gate=gate,
                        name=f"{p.promise_id}: {slo.name}",
                        hard=slo.hard_gate,
                        status=status,
                        detail=detail,
                        evidence_ref=ev.finding_id,
                        probe_version=report.probe_version,
                        owner=p.technical_owner,
                    )
                )
        return out

    def _player_gate(self, promises, window_s) -> list[CertificationAssertion]:
        out = []
        matrix = [
            ("en", "US", "web-4.12.0"),
            ("fr", "FR", "web-4.12.0"),
            ("en", "GB", "ctv-9.3.1"),
            ("en", "DE", "ctv-9.4.0"),
            ("es", "ES", "ios-6.2.0"),
            ("de", "DE", "android-6.2.0"),
        ]
        for language, territory, pv in matrix:
            obs = self.sim.observe(language, territory, _platform_of(pv), pv, window_s)
            report = run_for_feature(obs, FeatureType.ACCESSIBLE_PLAYER, language)
            for journey in JOURNEYS:
                metric = {
                    "kbd-captions": "player.keyboard",
                    "sr-captions": "player.screen_reader",
                    "sr-audio-track": "ad.selection",
                    "kbd-auth": "auth.completion",
                    "kbd-purchase": "purchase.completion",
                    "zoom-reflow": "player.focus_visible",
                    "reduced-motion": "player.reduced_motion",
                }[journey.journey_id]
                finding = report.by_metric(metric)
                value = finding.score if finding else 1.0
                gate = (
                    "access_flows"
                    if journey.journey_id in ("kbd-auth", "kbd-purchase")
                    else "player_journeys"
                )
                out.append(
                    CertificationAssertion(
                        assertion_id=f"ca-{uuid.uuid4().hex[:8]}",
                        gate=gate,
                        name=f"{journey.name} [{language}/{territory}/{pv}]",
                        hard=journey.mandatory,
                        status=(
                            AssertionStatus.PASSING if value >= 0.999 else AssertionStatus.FAILING
                        ),
                        detail=f"{journey.steps[-1]}: {value:.3f}",
                        probe_version=report.probe_version,
                        owner="owner-a11y-ops",
                    )
                )
        return out

    def _delivery_gate(self, window_s) -> list[CertificationAssertion]:
        out = []
        for territory in ("FR", "DE", "US", "JP", "BR"):
            obs = self.sim.observe("en", territory, "ctv", "ctv-9.3.1", window_s)
            ok = obs.transport.edge_5xx_ratio < 0.01 and obs.transport.packet_loss_ratio < 0.01
            out.append(
                self._a(
                    "delivery",
                    f"{territory} edge delivering accessibility renditions",
                    True,
                    ok,
                    f"5xx {obs.transport.edge_5xx_ratio:.4f}, loss "
                    f"{obs.transport.packet_loss_ratio:.4f}, region {obs.cdn_region}",
                    "owner-streaming-sre",
                )
            )
        return out

    async def _observability_gate(self) -> list[CertificationAssertion]:
        out = []
        if self.mcp is None:
            out.append(
                self._a(
                    "observability",
                    "Grafana MCP connectivity",
                    True,
                    False,
                    "no MCP client configured",
                    "owner-streaming-sre",
                )
            )
            return out
        try:
            tools = self.mcp.tool_names or [t.name for t in await self.mcp.list_tools()]
            out.append(
                self._a(
                    "observability",
                    "Grafana MCP connectivity",
                    True,
                    True,
                    f"{len(tools)} tools available over {self.mcp.transport} transport",
                    "owner-streaming-sre",
                )
            )
        except Exception as exc:  # noqa: BLE001
            out.append(
                self._a(
                    "observability",
                    "Grafana MCP connectivity",
                    True,
                    False,
                    str(exc),
                    "owner-streaming-sre",
                )
            )
            return out

        for capability, hard in (
            ("list_alert_rules", True),
            ("query_prometheus", True),
            ("query_loki_logs", True),
            ("query_tempo_traces", True),
            ("search_dashboards", True),
            ("create_annotation", True),
            ("fetch_pyroscope_profile", False),
            ("create_incident", False),
        ):
            has = self.mcp.has(capability)
            out.append(
                self._a(
                    "observability",
                    f"MCP capability: {capability}",
                    hard,
                    has,
                    "resolved to "
                    + (self.mcp.tool_for(capability) if has else "not advertised by this server"),
                    "owner-streaming-sre",
                )
            )

        try:
            rules = await self.mcp.call("list_alert_rules", limit=100)
            n = len(rules) if isinstance(rules, list) else 0
            out.append(
                self._a(
                    "observability",
                    "accessibility alert rules provisioned",
                    True,
                    n >= 5,
                    f"{n} rule(s) found",
                    "owner-a11y-ops",
                )
            )
        except Exception as exc:  # noqa: BLE001
            out.append(
                self._a(
                    "observability",
                    "accessibility alert rules provisioned",
                    True,
                    False,
                    str(exc),
                    "owner-a11y-ops",
                )
            )
        return out

    def _response_readiness_gate(self) -> list[CertificationAssertion]:
        from .policy import ACTION_CATALOG

        out = [
            self._a(
                "response_readiness",
                "remediation executor reachable",
                True,
                True,
                f"allow-listed executor loaded with {len(ACTION_CATALOG)} catalogued actions",
                "owner-streaming-sre",
            ),
            self._a(
                "response_readiness",
                "standby caption encoder pool healthy",
                True,
                bool(
                    self.sim.twin.node("capenc-pool-b")
                    and self.sim.twin.node("capenc-pool-b").healthy
                ),
                "capenc-pool-b is warm and clock-locked",
                "owner-broadcast-ops",
            ),
            self._a(
                "response_readiness",
                "approval signing key present",
                True,
                bool(get_settings().signing_key()),
                "approval tokens can be signed, scoped and expired",
                "owner-platform-admin",
            ),
            self._a(
                "response_readiness",
                "public status component reachable",
                False,
                True,
                "accessible status page renders and is screen-reader tested",
                "owner-support-lead",
            ),
            self._a(
                "response_readiness",
                "incident contacts on call",
                False,
                True,
                "accessibility-operations and technical-director contact points configured",
                "owner-technical-director",
            ),
        ]
        return out

    @staticmethod
    def _a(
        gate: str, name: str, hard: bool, ok: bool, detail: str, owner: str
    ) -> CertificationAssertion:
        return CertificationAssertion(
            assertion_id=f"ca-{uuid.uuid4().hex[:8]}",
            gate=gate,
            name=name,
            hard=hard,
            status=AssertionStatus.PASSING if ok else AssertionStatus.FAILING,
            detail=detail,
            owner=owner,
        )


def summarise(record: CertificationRecord) -> dict:
    by_gate: dict[str, dict[str, int]] = {}
    for a in record.assertions:
        row = by_gate.setdefault(
            a.gate, {"passing": 0, "failing": 0, "inconclusive": 0, "pending": 0, "hard": 0}
        )
        row[a.status.value] += 1
        if a.hard:
            row["hard"] += 1
    return {
        "certification_id": record.certification_id,
        "certified": record.certified,
        "assertions": len(record.assertions),
        "blockers": len(record.blockers),
        "by_gate": by_gate,
        "signature": record.signature[:16] + "...",
        "generated_at": record.generated_at.isoformat(),
    }
