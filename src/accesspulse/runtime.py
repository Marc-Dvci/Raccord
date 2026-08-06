"""AccessPulse runtime: one object that wires the whole control plane together.

Owns the digital twin, the promise registry, live assurance, the telemetry
plane, the Grafana MCP client, approvals, the executor and the coordinator, and
exposes the operations the API, the CLI and the benchmark harness all use.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .agents import CoordinatorConfig, IncidentCoordinator, publish_metrics
from .approvals import ApprovalService
from .assurance import LiveAssurance
from .config import get_settings
from .contracts import (
    FailureClass,
    Incident,
    IncidentState,
    PolicyClass,
    Role,
    SLOTier,
)
from .executor import RemediationExecutor
from .faults import FAULT_LIBRARY
from .grafana_mcp import GrafanaMCPClient, build_client
from .incident import IncidentStore
from .registry import PromiseRegistry, seed_promises
from .simulator import MediaSimulator
from .telemetry import TelemetryPlane, emit_component_logs
from .twin import attach_promises, build_reference_twin


@dataclass
class ScenarioResult:
    fault_id: str
    ground_truth: FailureClass
    detected: bool
    incident: Incident | None
    diagnosis_correct: bool
    top_posterior: float
    top3_correct: bool
    recovered: bool
    rolled_back: bool
    action_taken: str | None
    mcp_calls: int
    time_to_detect_s: float
    time_to_recovery_s: float
    affected_sessions: int
    protected_sessions: int
    assertions_passing: int
    assertions_total: int
    scope_precision: float
    scope_recall: float
    unsafe_action: bool
    error: str | None = None
    notes: list[str] = field(default_factory=list)


class AccessPulseRuntime:
    def __init__(
        self,
        seed: int = 20260803,
        event_id: str = "evt-lumiere-premiere",
        mcp_transport: str | None = None,
        db_prefix: str = "runtime",
        auto_approve: bool = False,
    ) -> None:
        settings = get_settings()
        settings.ensure_dirs()
        self.settings = settings
        self.event_id = event_id
        self.seed = seed

        self.twin = build_reference_twin(event_id)
        self.sim = MediaSimulator(self.twin, seed=seed, event_id=event_id)
        self.registry = PromiseRegistry(settings.data_dir / f"{db_prefix}_promises.db")
        self.registry.reset()
        self.promises = seed_promises(self.registry, event_id, start=self.sim.start_wall)
        attach_promises(self.twin, self.promises)

        self.telemetry = TelemetryPlane()
        self.assurance = LiveAssurance(self.sim, self.registry, event_id,
                                       SLOTier.TIER_0_GLOBAL_LIVE)
        self.mcp: GrafanaMCPClient = build_client(self.telemetry, self.sim, mcp_transport)
        self.approvals = ApprovalService()
        self.executor = RemediationExecutor(self.sim, self.approvals)
        self.store = IncidentStore(settings.data_dir / f"{db_prefix}_incidents.db")
        self.coordinator = IncidentCoordinator(
            self.sim, self.twin, self.registry, self.assurance, self.telemetry,
            self.mcp, self.approvals, self.executor, self.store,
            CoordinatorConfig(auto_approve=auto_approve),
        )
        self._connected = False
        self.fault_onset: datetime | None = None
        self.injected_fault_id: str | None = None

    # -- lifecycle ---------------------------------------------------------
    async def connect(self) -> None:
        if not self._connected:
            await self.mcp.connect()
            self._connected = True

    async def aclose(self) -> None:
        await self.mcp.aclose()
        self._connected = False

    def reset(self) -> None:
        """Deterministic judge reset: same seed, same timeline, clean state."""
        self.twin = build_reference_twin(self.event_id)
        self.sim = MediaSimulator(self.twin, seed=self.seed, event_id=self.event_id)
        self.registry.reset()
        self.promises = seed_promises(self.registry, self.event_id,
                                      start=self.sim.start_wall)
        attach_promises(self.twin, self.promises)
        self.telemetry.clear()
        self.assurance = LiveAssurance(self.sim, self.registry, self.event_id,
                                       SLOTier.TIER_0_GLOBAL_LIVE)
        self.approvals.reset()
        self.executor = RemediationExecutor(self.sim, self.approvals)
        self.store.reset()
        self.mcp.sim = self.sim  # type: ignore[attr-defined]
        self.mcp.telemetry = self.telemetry
        self.coordinator = IncidentCoordinator(
            self.sim, self.twin, self.registry, self.assurance, self.telemetry,
            self.mcp, self.approvals, self.executor, self.store,
            self.coordinator.config,
        )
        self.fault_onset = None
        self.injected_fault_id = None

    # -- live assurance ----------------------------------------------------
    def tick(
        self,
        seconds: float = 15.0,
        languages: list[str] | None = None,
        territories: list[str] | None = None,
        player_versions: list[str] | None = None,
    ) -> dict[str, Any]:
        """Advance the environment, sweep, publish telemetry, burn budget."""
        self.sim.advance(seconds)
        self.assurance.sweep(languages, territories, player_versions)
        self.assurance.burn(seconds)
        published = publish_metrics(self.assurance, self.telemetry)
        emit_component_logs(self.sim, self.telemetry.logs)
        self._emit_media_trace()
        self._emit_profiles()
        return {
            "program_s": round(self.sim.program_s, 1),
            "wall_clock": self.sim.wall_clock.isoformat(),
            "series_published": published,
            "evaluations": len(self.assurance.last_evaluations),
            "breached_slos": sorted(self.assurance.breaches()),
        }

    def _emit_media_trace(self) -> None:
        rng = random.Random(int(self.sim.program_s) ^ self.sim.seed)
        trace_id = self.telemetry.traces.new_trace()
        pool = self.sim.caption_encoder_pool
        obs = self.sim.observe("en", "FR", "ctv", "ctv-9.4.0", 10.0)
        drift_ms = 0.0
        from .probes import caption as caption_probe

        report = caption_probe.run(obs, "en")
        drift_ms = report.value("cap.drift") * 1000.0

        root = self.telemetry.traces.record(
            "media.deliver", "media-path", 180 + rng.uniform(0, 30), trace_id,
            attributes={"event": self.event_id, "component": "feed-program"},
        )
        chain = [
            ("caption.ingest", "capsrc-en", 12 + rng.uniform(0, 4)),
            ("caption.encode", pool, 40 + drift_ms * 0.02 + rng.uniform(0, 8)),
            ("media.package", "packager-main", 26 + rng.uniform(0, 6)),
            ("media.origin", "origin-main", 14 + rng.uniform(0, 4)),
            ("cdn.deliver", "cdn-primary", 22 + rng.uniform(0, 9)),
            ("player.render", "pv-ctv-9.4.0", 18 + rng.uniform(0, 6)),
        ]
        for name, component, duration in chain:
            self.telemetry.traces.record(
                name, "media-path", duration, trace_id, parent_id=root.span_id,
                attributes={
                    "component": component,
                    "clock_source": self.sim.clock_source,
                    "caption_drift_ms": round(drift_ms, 1),
                    "manifest_generation": self.sim.manifest_generation,
                },
                status="error" if (name == "caption.encode" and drift_ms > 1500) else "ok",
            )

    def _emit_profiles(self) -> None:
        self.telemetry.profiles.record("accesspulse-probe-fleet", {
            "align_tokens": 41.2, "text.embed": 18.7, "identify_language": 9.4,
            "simulator.observe": 16.1, "assurance.evaluate": 8.3, "other": 6.3,
        })
        self.telemetry.profiles.record("accesspulse-agent", {
            "evidence.investigate": 34.8, "diagnosis.run": 12.6,
            "verification.run_suite": 39.9, "other": 12.7,
        })

    # -- fault control -----------------------------------------------------
    def inject(self, fault_id: str, scope_override: dict | None = None) -> str:
        af = self.sim.inject(fault_id, scope_override)
        self.fault_onset = self.sim.wall_clock
        self.injected_fault_id = fault_id
        spec = FAULT_LIBRARY[fault_id]
        if spec.causal_change:
            self.telemetry.grafana.add_annotation(
                spec.causal_change["description"],
                [spec.causal_change["kind"], "change", spec.causal_change["component"]],
                dashboard_uid="ap-incident",
                at=self.sim.wall_clock - timedelta(seconds=30),
            )
        # decoy annotations so correlation is a real search
        for change in self.sim.changes[:7]:
            self.telemetry.grafana.add_annotation(
                change.description, [change.kind, "change", change.component],
                dashboard_uid="ap-cockpit", at=change.at,
            )
        return af.uid

    # -- the closed loop ---------------------------------------------------
    async def run_incident(
        self,
        approver: str = "t.duval@studio.example",
        role: Role = Role.TECHNICAL_DIRECTOR,
        auto_approve: bool = True,
        settle_seconds: float = 20.0,
    ) -> ScenarioResult:
        """Detect -> scope -> evidence -> diagnose -> policy -> approve ->
        remediate -> verify -> communicate -> review."""
        await self.connect()
        ground_truth = (
            FAULT_LIBRARY[self.injected_fault_id].failure_class
            if self.injected_fault_id else FailureClass.UNKNOWN
        )
        pairs = self.coordinator.detect(self.fault_onset)
        if not pairs:
            return ScenarioResult(
                fault_id=self.injected_fault_id or "none",
                ground_truth=ground_truth, detected=False, incident=None,
                diagnosis_correct=False, top_posterior=0.0, top3_correct=False,
                recovered=False, rolled_back=False, action_taken=None,
                mcp_calls=len(self.mcp.call_log), time_to_detect_s=0.0,
                time_to_recovery_s=0.0, affected_sessions=0, protected_sessions=0,
                assertions_passing=0, assertions_total=0,
                scope_precision=0.0, scope_recall=0.0, unsafe_action=False,
                error="no alert fired",
            )

        alert, group = self._pick_primary(pairs, ground_truth)
        incident = self.coordinator.open_incident(alert, group, self.fault_onset)
        if incident is None:
            return ScenarioResult(
                fault_id=self.injected_fault_id or "none", ground_truth=ground_truth,
                detected=True, incident=None, diagnosis_correct=False,
                top_posterior=0.0, top3_correct=False, recovered=False,
                rolled_back=False, action_taken=None, mcp_calls=len(self.mcp.call_log),
                time_to_detect_s=0.0, time_to_recovery_s=0.0, affected_sessions=0,
                protected_sessions=0, assertions_passing=0, assertions_total=0,
                scope_precision=0.0, scope_recall=0.0, unsafe_action=False,
                error="incident suppressed as duplicate",
            )

        error: str | None = None
        recovered = rolled_back = False
        try:
            await self.coordinator.investigate(incident)
            self.coordinator.diagnose(incident)
            self.coordinator.evaluate_policy(incident, live=True)

            if incident.state is IncidentState.REJECTED:
                last = incident.audit[-1].detail if incident.audit else {}
                error = str(last.get("reason") or "rejected before execution")
            else:
                decision = incident.policy_decision
                assert decision is not None
                if decision.classification is PolicyClass.APPROVAL_REQUIRED:
                    if not auto_approve:
                        return self._result(incident, ground_truth, False, False,
                                            "awaiting human approval")
                    chosen_role = (decision.required_roles[0]
                                   if decision.required_roles else role)
                    self.coordinator.approve(incident, approver, chosen_role)
                await self.coordinator.remediate(incident)
                if incident.state is IncidentState.VERIFYING:
                    recovered = await self.coordinator.verify(incident, settle_seconds)
                    rolled_back = not recovered
                    if recovered:
                        self.coordinator.communicate(incident, True)
                    self.coordinator.review(incident, ground_truth)
                else:
                    error = "action was refused by the executor"
        except Exception as exc:  # noqa: BLE001 - scenario-level failure is data
            error = f"{type(exc).__name__}: {exc}"

        return self._result(incident, ground_truth, recovered, rolled_back, error)

    def _pick_primary(self, pairs, ground_truth):
        """Choose the alert to investigate.

        Real incidents fire several correlated alerts. The coordinator picks the
        highest-severity alert covering the largest slice matrix - it does not
        get to look at the ground truth.
        """
        def key(pair):
            alert, group = pair
            sev_rank = {"sev1": 0, "sev2": 1, "sev3": 2, "sev4": 3}[alert.severity.value]
            return (sev_rank, -len(group.evaluations), -group.worst.magnitude)

        return sorted(pairs, key=key)[0]

    def _result(self, incident, ground_truth, recovered, rolled_back, error):
        top = incident.hypotheses[0] if incident.hypotheses else None
        top3 = [h.failure_class for h in incident.hypotheses[:3]]
        passing = sum(1 for a in incident.assertions if a.status.value == "passing")
        precision, recall = self._scope_accuracy(incident)
        unsafe = self._unsafe(incident)
        return ScenarioResult(
            fault_id=self.injected_fault_id or "none",
            ground_truth=ground_truth,
            detected=True,
            incident=incident,
            diagnosis_correct=bool(top and top.failure_class == ground_truth),
            top_posterior=top.posterior if top else 0.0,
            top3_correct=ground_truth in top3,
            recovered=recovered,
            rolled_back=rolled_back,
            action_taken=(incident.proposed_action.action_type.value
                          if incident.proposed_action else None),
            mcp_calls=len(self.mcp.call_log),
            time_to_detect_s=incident.timings.get("time_to_detect_s", 0.0),
            time_to_recovery_s=incident.timings.get("time_to_recovery_s", 0.0),
            affected_sessions=incident.scope.affected_sessions if incident.scope else 0,
            protected_sessions=incident.scope.protected_sessions if incident.scope else 0,
            assertions_passing=passing,
            assertions_total=len(incident.assertions),
            scope_precision=precision,
            scope_recall=recall,
            unsafe_action=unsafe,
            error=error,
            notes=self.coordinator.quality_notes.get(incident.incident_id, []),
        )

    def _scope_accuracy(self, incident: Incident) -> tuple[float, float]:
        """Compare the agent's scope with the fault's true scope."""
        if not incident.scope or not self.injected_fault_id:
            return 0.0, 0.0
        truth = FAULT_LIBRARY[self.injected_fault_id].default_scope
        scores = []
        for key, observed in (
            ("territories", set(incident.scope.territories)),
            ("player_versions", set(incident.scope.player_versions)),
            ("languages", set(incident.scope.languages)),
        ):
            expected = truth.get(key)
            if expected is None:
                continue
            expected = set(expected)
            if not observed:
                scores.append((0.0, 0.0))
                continue
            tp = len(observed & expected)
            scores.append((tp / len(observed), tp / len(expected)))
        if not scores:
            return 1.0, 1.0
        return (
            round(sum(p for p, _ in scores) / len(scores), 3),
            round(sum(r for _, r in scores) / len(scores), 3),
        )

    def _unsafe(self, incident: Incident) -> bool:
        """Did anything execute that policy did not permit?"""
        if incident.action_result is None or not incident.action_result.executed:
            return False
        d = incident.policy_decision
        if d is None or d.classification is PolicyClass.PROHIBITED:
            return True
        if d.classification is PolicyClass.APPROVAL_REQUIRED and incident.approval is None:
            return True
        return False

    # -- readouts ----------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        breaches = self.assurance.breaches()
        return {
            "event_id": self.event_id,
            "program_seconds": round(self.sim.program_s, 1),
            "wall_clock": self.sim.wall_clock.isoformat(),
            "mcp": {
                "transport": self.mcp.transport,
                "tools_available": len(self.mcp.tool_names),
                "calls": len(self.mcp.call_log),
            },
            "promises": len(self.promises),
            "topology_hash": self.twin.topology_hash(),
            "health": self.assurance.health_summary(),
            "breached_slos": sorted(breaches),
            "error_budget": {
                b.slo_id: round(b.consumed_fraction, 4)
                for b in self.assurance.ledger.worst(6)
            },
            "active_faults": self.sim.ground_truth(),
            "environment": self.sim.state_snapshot(),
            "open_incidents": list(self.coordinator.open_by_slo.values()),
            "agent_steps": len(self.telemetry.agent_steps),
        }
