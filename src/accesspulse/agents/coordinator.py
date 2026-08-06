"""Incident Coordinator.

Owns incident state and drives the other agents through the state machine. It
validates every transition's preconditions before invoking the next agent,
deduplicates concurrent incidents for the same SLO, prevents conflicting
remediation, enforces step timeouts, records every decision as an immutable
audit event, and closes an incident only after all mandatory verification
assertions pass.

The coordinator is the only component that may call `transition()`.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ..approvals import ApprovalService
from ..assurance import BreachGroup, LiveAssurance
from ..contracts import (
    ActionType,
    Alert,
    Approval,
    FailureClass,
    Incident,
    IncidentState,
    PolicyClass,
    PostIncidentReview,
    ProposedAction,
    Role,
    SLOTier,
    utcnow,
)
from ..executor import ExecutionRefused, RemediationExecutor
from ..grafana_mcp import GrafanaMCPClient
from ..incident import IncidentMachine, IncidentStore
from ..policy import ACTION_CATALOG, PolicyContext
from ..policy import evaluate as evaluate_policy
from ..registry import PromiseRegistry
from ..simulator import MediaSimulator
from ..telemetry import TelemetryPlane
from ..twin import DigitalTwin
from ..verification import all_mandatory_passed, failing, run_suite, suite_for
from .core import (
    ChangeCorrelationAgent,
    CommunicationAgent,
    DiagnosisAgent,
    MultimodalQualityAgent,
    ReliabilityLearningAgent,
    ScopeAgent,
)
from .evidence import GrafanaEvidenceAgent

# Which catalogued action genuinely addresses which failure class. The
# coordinator proposes; policy decides whether it may run.
REMEDIATION_MAP: dict[FailureClass, tuple[ActionType, str, dict[str, Any]]] = {
    FailureClass.INFRA_CLOCK_SOURCE_CHANGE:
        (ActionType.SELECT_SYNCHRONIZED_STANDBY, "capenc-pool-b", {}),
    FailureClass.CAPTION_PROGRESSIVE_DRIFT:
        (ActionType.SELECT_SYNCHRONIZED_STANDBY, "capenc-pool-b", {}),
    FailureClass.CAPTION_CLOCK_OFFSET:
        (ActionType.CHANGE_CLOCK_SOURCE, "clock-ptp-primary", {}),
    FailureClass.CAPTION_ENCODER_FAILURE:
        (ActionType.SWITCH_CAPTION_ENCODER_POOL, "capenc-pool-b", {}),
    FailureClass.CAPTION_SOURCE_LOSS:
        (ActionType.REROUTE_CAPTION_PATH, "secondary", {}),
    FailureClass.CAPTION_MANIFEST_OMISSION:
        (ActionType.REPUBLISH_MANIFEST, "manifest-main", {}),
    FailureClass.INFRA_MALFORMED_MANIFEST:
        (ActionType.REPUBLISH_MANIFEST, "manifest-main", {}),
    FailureClass.CAPTION_WRONG_LANGUAGE:
        (ActionType.REROUTE_CAPTION_PATH, "secondary", {}),
    FailureClass.CAPTION_RENDER_FAILURE:
        (ActionType.RESTORE_KNOWN_GOOD_PLAYER, "ctv-9.4.0",
         {"from_version": "ctv-9.4.0", "to_version": "ctv-9.3.1"}),
    FailureClass.CAPTION_WORD_DROP:
        (ActionType.SWITCH_CAPTION_ENCODER_POOL, "capenc-pool-b", {}),
    FailureClass.CAPTION_DUPLICATE:
        (ActionType.REPUBLISH_MANIFEST, "manifest-main", {}),
    FailureClass.CAPTION_READING_SPEED:
        (ActionType.SWITCH_CAPTION_ENCODER_POOL, "capenc-pool-b", {}),
    FailureClass.CAPTION_SPEAKER_CORRUPTION:
        (ActionType.SWITCH_CAPTION_ENCODER_POOL, "capenc-pool-b", {}),
    FailureClass.CAPTION_FLICKER:
        (ActionType.RESTORE_KNOWN_GOOD_PLAYER, "ctv-9.4.0",
         {"from_version": "ctv-9.4.0", "to_version": "ctv-9.3.1"}),
    FailureClass.INFRA_ENCODER_CPU:
        (ActionType.SWITCH_CAPTION_ENCODER_POOL, "capenc-pool-b", {}),
    FailureClass.INFRA_PROVIDER_DEGRADATION:
        (ActionType.SWITCH_ALTERNATE_LANGUAGE_SOURCE, "capsrc-en", {"language": "en"}),
    FailureClass.INFRA_STALE_CONFIG:
        (ActionType.SWITCH_CAPTION_ENCODER_POOL, "capenc-pool-b", {}),
    FailureClass.INFRA_CDN_REGIONAL:
        (ActionType.REROUTE_REGION, "region-eu-west", {"to_region": "eu-central"}),
    FailureClass.INFRA_PACKET_LOSS:
        (ActionType.REROUTE_REGION, "region-eu-central", {"to_region": "eu-west"}),
    FailureClass.SIGN_REGIONAL_DELIVERY:
        (ActionType.REROUTE_REGION, "region-eu-west", {"to_region": "eu-central"}),
    FailureClass.AD_TRACK_OMISSION:
        (ActionType.RESTORE_AUDIO_TRACK, "en-desc", {}),
    FailureClass.AD_SILENT_SEGMENT:
        (ActionType.RESTORE_AUDIO_TRACK, "en-desc", {}),
    FailureClass.AD_WRONG_LANGUAGE:
        (ActionType.SWITCH_ALTERNATE_LANGUAGE_SOURCE, "adsrc-en", {"language": "en"}),
    FailureClass.AD_TIMELINE_DRIFT:
        (ActionType.CHANGE_CLOCK_SOURCE, "clock-ptp-primary", {}),
    FailureClass.AD_LOUDNESS_DEFECT:
        (ActionType.RESTORE_AUDIO_TRACK, "fr-desc", {}),
    FailureClass.AD_CHANNEL_LAYOUT:
        (ActionType.REPUBLISH_MANIFEST, "manifest-main", {}),
    FailureClass.AD_SELECTION_FAILURE:
        (ActionType.RESTORE_KNOWN_GOOD_PLAYER, "ctv-9.4.0",
         {"from_version": "ctv-9.4.0", "to_version": "ctv-9.3.1"}),
    FailureClass.SIGN_FROZEN_FRAMES:
        (ActionType.RESTART_SIGN_PIPELINE, "signsrc-lsf", {}),
    FailureClass.SIGN_BLACK_FRAMES:
        (ActionType.RESTART_SIGN_PIPELINE, "signsrc-lsf", {}),
    FailureClass.SIGN_LOW_FRAMERATE:
        (ActionType.RESTART_SIGN_PIPELINE, "signsrc-lsf", {}),
    FailureClass.SIGN_CROP_FAILURE:
        (ActionType.RESTART_SIGN_PIPELINE, "signsrc-lsf", {}),
    FailureClass.SIGN_SYNC_DRIFT:
        (ActionType.CHANGE_CLOCK_SOURCE, "clock-ptp-primary", {}),
    FailureClass.SIGN_PIP_OBSTRUCTION:
        (ActionType.DISABLE_PLAYER_FEATURE_FLAG, "festival-branding-overlay", {}),
    FailureClass.INFRA_GPU_SATURATION:
        (ActionType.RESTART_SIGN_PIPELINE, "signsrc-lsf", {}),
    FailureClass.INFRA_DEPLOY_REGRESSION:
        (ActionType.RESTORE_KNOWN_GOOD_PLAYER, "ctv-9.4.0",
         {"from_version": "ctv-9.4.0", "to_version": "ctv-9.3.1"}),
    FailureClass.PLAYER_KEYBOARD_TRAP:
        (ActionType.RESTORE_KNOWN_GOOD_PLAYER, "web-4.12.0",
         {"from_version": "web-4.12.0", "to_version": "web-4.11.3"}),
    FailureClass.PLAYER_MISSING_NAME:
        (ActionType.RESTORE_KNOWN_GOOD_PLAYER, "web-4.12.0",
         {"from_version": "web-4.12.0", "to_version": "web-4.11.3"}),
    FailureClass.PLAYER_FOCUS_LOSS:
        (ActionType.RESTORE_KNOWN_GOOD_PLAYER, "web-4.12.0",
         {"from_version": "web-4.12.0", "to_version": "web-4.11.3"}),
    FailureClass.PLAYER_SCREEN_READER:
        (ActionType.RESTORE_KNOWN_GOOD_PLAYER, "web-4.12.0",
         {"from_version": "web-4.12.0", "to_version": "web-4.11.3"}),
    FailureClass.PLAYER_INACCESSIBLE_ERROR:
        (ActionType.RESTORE_KNOWN_GOOD_PLAYER, "ctv-9.4.0",
         {"from_version": "ctv-9.4.0", "to_version": "ctv-9.3.1"}),
    FailureClass.PLAYER_CAPTION_CONTROL:
        (ActionType.RESTORE_KNOWN_GOOD_PLAYER, "ctv-9.4.0",
         {"from_version": "ctv-9.4.0", "to_version": "ctv-9.3.1"}),
    FailureClass.PLAYER_REDUCED_MOTION:
        (ActionType.DISABLE_PLAYER_FEATURE_FLAG, "festival-branding-overlay", {}),
    FailureClass.PLAYER_AUTH_FAILURE:
        (ActionType.DISABLE_PLAYER_FEATURE_FLAG, "bot-challenge", {}),
    FailureClass.PLAYER_PURCHASE_FAILURE:
        (ActionType.RESTORE_KNOWN_GOOD_PLAYER, "web-4.12.0",
         {"from_version": "web-4.12.0", "to_version": "web-4.11.3"}),
}


@dataclass
class CoordinatorConfig:
    step_timeout_s: float = 45.0
    max_concurrent_incidents: int = 8
    auto_approve: bool = False
    auto_approver: str = "operator.demo@studio.example"
    auto_approver_role: Role = Role.TECHNICAL_DIRECTOR
    change_freeze: bool = False


class IncidentCoordinator:
    name = "incident_coordinator"

    def __init__(
        self,
        sim: MediaSimulator,
        twin: DigitalTwin,
        registry: PromiseRegistry,
        assurance: LiveAssurance,
        telemetry: TelemetryPlane,
        mcp: GrafanaMCPClient,
        approvals: ApprovalService,
        executor: RemediationExecutor,
        store: IncidentStore | None = None,
        config: CoordinatorConfig | None = None,
    ) -> None:
        self.sim = sim
        self.twin = twin
        self.registry = registry
        self.assurance = assurance
        self.telemetry = telemetry
        self.mcp = mcp
        self.approvals = approvals
        self.executor = executor
        self.store = store
        self.config = config or CoordinatorConfig()

        self.scope_agent = ScopeAgent(twin, registry, assurance)
        self.quality_agent = MultimodalQualityAgent()
        self.correlation_agent = ChangeCorrelationAgent(twin)
        self.diagnosis_agent = DiagnosisAgent()
        self.evidence_agent = GrafanaEvidenceAgent(mcp, telemetry)
        self.communication_agent = CommunicationAgent()
        self.learning_agent = ReliabilityLearningAgent()

        self.machines: dict[str, IncidentMachine] = {}
        self.open_by_slo: dict[str, str] = {}
        self.reviews: dict[str, PostIncidentReview] = {}
        self.trace_ids: dict[str, str] = {}
        self.quality_notes: dict[str, list[str]] = {}
        self.causal: dict[str, list] = {}
        self.deep_links: dict[str, str] = {}
        self._t0: dict[str, float] = {}

    # -- helpers -----------------------------------------------------------
    def _step(self, agent: str, step: str, incident_id: str):
        return _StepTimer(self, agent, step, incident_id)

    def machine(self, incident_id: str) -> IncidentMachine:
        return self.machines[incident_id]

    # -- 1. detection ------------------------------------------------------
    def detect(self, fault_onset: datetime | None = None) -> list[tuple[Alert, BreachGroup]]:
        alerts = self.assurance.raise_alerts()
        groups = self.assurance.breaches()
        pairs: list[tuple[Alert, BreachGroup]] = []
        for a in alerts:
            slo = a.labels["slo"]
            if slo in groups:
                pairs.append((a, groups[slo]))
        # publish to the MCP server's alert view
        if hasattr(self.mcp, "set_firing"):
            self.mcp.set_firing(alerts)  # type: ignore[attr-defined]
        return pairs

    # -- 2. open + qualify + scope ----------------------------------------
    def open_incident(
        self,
        alert: Alert,
        group: BreachGroup,
        fault_onset: datetime | None = None,
    ) -> Incident | None:
        slo = alert.labels["slo"]
        if slo in self.open_by_slo:
            existing = self.machines[self.open_by_slo[slo]]
            existing.note("duplicate_alert_suppressed", self.name, alert_id=alert.alert_id)
            return None
        if len(self.open_by_slo) >= self.config.max_concurrent_incidents:
            return None

        incident_id = f"inc-{uuid.uuid4().hex[:10]}"
        incident = Incident(
            incident_id=incident_id,
            event_id=self.assurance.event_id,
            title=f"{alert.rule_title} ({alert.labels.get('feature')})",
            severity=alert.severity,
            alert=alert,
        )
        machine = IncidentMachine(incident, self.store)
        self.machines[incident_id] = machine
        self.open_by_slo[slo] = incident_id
        self.trace_ids[incident_id] = self.telemetry.traces.new_trace()
        self._t0[incident_id] = time.perf_counter()

        onset = fault_onset or alert.fired_at
        incident.timings["time_to_detect_s"] = max(
            0.0, (alert.fired_at - onset).total_seconds()
        )

        with self._step(self.name, "qualify", incident_id):
            machine.transition(IncidentState.QUALIFIED, self.name,
                               slo=slo, severity=alert.severity.value)

        with self._step(self.scope_agent.name, "scope", incident_id):
            scope = self.scope_agent.run(incident, alert, group)
            incident.scope = scope
            machine.transition(IncidentState.SCOPED, self.scope_agent.name,
                               blast_class=scope.blast_class,
                               affected_sessions=scope.affected_sessions,
                               promises=list(scope.violated_promise_ids))
        incident.timings["time_to_scope_s"] = time.perf_counter() - self._t0[incident_id]
        return incident

    # -- 3. evidence -------------------------------------------------------
    async def investigate(self, incident: Incident) -> None:
        machine = self.machine(incident.incident_id)
        assert incident.alert is not None

        with self._step(self.evidence_agent.name, "collect_evidence", incident.incident_id):
            evidence, changes, deep_link = await self.evidence_agent.investigate(
                incident, incident.alert
            )
        incident.evidence.extend(evidence)
        if deep_link:
            self.deep_links[incident.incident_id] = deep_link

        # probe findings for the affected slices
        with self._step(self.quality_agent.name, "collect_findings", incident.incident_id):
            incident.findings.extend(self._findings_for(incident))
            self.quality_notes[incident.incident_id] = self.quality_agent.run(
                incident, incident.findings
            )

        # change events: from Grafana annotations and from the change feed
        known = {c.change_id for c in changes}
        window_start = incident.opened_at - timedelta(minutes=45)
        for c in self.sim.changes_between(window_start, utcnow()):
            if c.change_id not in known:
                changes.append(c)
        incident.changes = changes

        machine.transition(IncidentState.EVIDENCE_COMPLETE, self.evidence_agent.name,
                           evidence_items=len(incident.evidence),
                           findings=len(incident.findings),
                           changes=len(incident.changes),
                           mcp_calls=len(self.mcp.call_log))
        incident.timings["time_to_evidence_s"] = (
            time.perf_counter() - self._t0[incident.incident_id]
        )
        incident.timings["mcp_calls"] = float(len(self.mcp.call_log))

    def _findings_for(self, incident: Incident) -> list:
        from ..probes import run_for_feature
        from ..simulator import _platform_of

        scope = incident.scope
        assert scope is not None
        out = []
        langs = list(scope.languages) or ["en"]
        terrs = list(scope.territories) or ["FR"]
        pvs = list(scope.player_versions) or ["ctv-9.4.0"]
        for language in langs[:2]:
            for territory in terrs[:3]:
                for pv in pvs[:2]:
                    obs = self.sim.observe(language, territory, _platform_of(pv), pv, 30.0)
                    for feature in scope.features:
                        report = run_for_feature(obs, feature, language)
                        out.extend(report.findings)
        return out

    # -- 4. diagnosis ------------------------------------------------------
    def diagnose(self, incident: Incident) -> None:
        machine = self.machine(incident.incident_id)
        assert incident.scope is not None and incident.alert is not None
        onset = incident.alert.fired_at

        with self._step(self.correlation_agent.name, "correlate", incident.incident_id):
            causal = self.correlation_agent.run(
                incident, incident.changes, incident.scope, onset
            )
            self.causal[incident.incident_id] = causal

        breached = {
            e.slo_id for e in self.assurance.last_evaluations if e.breached
        }
        with self._step(self.diagnosis_agent.name, "diagnose", incident.incident_id):
            incident.hypotheses = self.diagnosis_agent.run(
                incident, incident.scope, breached, incident.evidence, causal
            )
        machine.transition(IncidentState.DIAGNOSED, self.diagnosis_agent.name,
                           top=incident.hypotheses[0].failure_class.value,
                           posterior=incident.hypotheses[0].posterior,
                           abstained=incident.hypotheses[0].abstained)

    # -- 5. propose + policy ----------------------------------------------
    def evaluate_policy(self, incident: Incident, live: bool = True) -> None:
        machine = self.machine(incident.incident_id)
        assert incident.scope is not None
        top = incident.hypotheses[0]
        if top.abstained:
            # The diagnosis agent declined to conclude. No action may be proposed
            # on insufficient evidence; the incident is escalated to a human with
            # everything collected so far intact.
            machine.note("escalated_insufficient_evidence", self.name,
                         top_posterior=top.posterior)
            machine.transition(IncidentState.REJECTED, self.diagnosis_agent.name,
                               reason="diagnosis abstained: evidence is insufficient to "
                                      "justify a production change; escalated to a human")
            return
        mapping = REMEDIATION_MAP.get(top.failure_class)
        if mapping is None:
            machine.note("no_remediation_mapped", self.name,
                         failure_class=top.failure_class.value)
            machine.transition(IncidentState.REJECTED, self.name,
                               reason=f"no catalogued action addresses "
                                      f"{top.failure_class.value}; escalated to a human")
            return
        action_type, target, params = mapping
        spec = ACTION_CATALOG[action_type]

        action = ProposedAction(
            action_id=f"act-{uuid.uuid4().hex[:8]}",
            incident_id=incident.incident_id,
            action_type=action_type,
            target=target,
            parameters=params,
            scope_digest=f"{'/'.join(incident.scope.territories)}|"
                         f"{'/'.join(incident.scope.player_versions)}",
            expected_effect=spec.title,
            expected_metric_change=spec.expected_metric_change,
            verification_suite=suite_for(incident.scope.features[0],
                                         spec.verification_suite),
            rollback_behaviour=spec.rollback_behaviour,
            idempotency_key=f"{incident.incident_id}:{action_type.value}:{target}",
        )
        incident.proposed_action = action

        tier = SLOTier.TIER_0_GLOBAL_LIVE
        promises = self.registry.for_event(self.assurance.event_id, at=self.sim.wall_clock)
        for p in promises:
            if p.promise_id in incident.scope.violated_promise_ids:
                tier = p.slo_tier
                break

        ctx = PolicyContext(
            tier=tier,
            live=live,
            scope=incident.scope,
            operator_roles=(Role.OPERATOR, Role.TECHNICAL_DIRECTOR, Role.STREAMING_SRE),
            change_freeze=self.config.change_freeze,
            concurrent_actions=tuple(
                m.incident.proposed_action.action_type.value
                for m in self.machines.values()
                if m.state is IncidentState.ACTION_EXECUTING and m.incident.proposed_action
                and m.incident.incident_id != incident.incident_id
            ),
        )
        with self._step("policy_agent", "evaluate", incident.incident_id):
            incident.policy_decision = evaluate_policy(action, ctx)

        machine.transition(IncidentState.POLICY_EVALUATED, "policy_agent",
                           classification=incident.policy_decision.classification.value,
                           required_roles=[r.value for r in
                                           incident.policy_decision.required_roles],
                           policy_version=incident.policy_decision.policy_version)

        if incident.policy_decision.classification is PolicyClass.PROHIBITED:
            machine.transition(IncidentState.REJECTED, "policy_agent",
                               reason=list(incident.policy_decision.rationale))
            return
        if incident.policy_decision.classification is PolicyClass.APPROVAL_REQUIRED:
            machine.transition(IncidentState.AWAITING_APPROVAL, "policy_agent")

    # -- 6. approval -------------------------------------------------------
    def approve(self, incident: Incident, approver: str, role: Role) -> Approval:
        assert incident.proposed_action is not None and incident.policy_decision is not None
        approval = self.approvals.issue(
            incident.proposed_action,
            incident.evidence_hash(),
            approver,
            role,
            incident.policy_decision.required_roles,
        )
        incident.approval = approval
        self.machine(incident.incident_id).note(
            "approval_issued", approver, action_id=approval.action_id,
            role=role.value, expires_at=approval.expires_at.isoformat(),
        )
        incident.timings["time_to_approval_s"] = (
            time.perf_counter() - self._t0[incident.incident_id]
        )
        return approval

    def reject(self, incident: Incident, actor: str, reason: str) -> None:
        self.machine(incident.incident_id).transition(
            IncidentState.REJECTED, actor, reason=reason
        )

    # -- 7. remediate ------------------------------------------------------
    async def remediate(self, incident: Incident) -> None:
        machine = self.machine(incident.incident_id)
        assert incident.proposed_action is not None
        machine.transition(IncidentState.ACTION_EXECUTING, "remediation_agent",
                           action_type=incident.proposed_action.action_type.value,
                           action_target=incident.proposed_action.target)
        with self._step("remediation_agent", "execute", incident.incident_id):
            try:
                incident.action_result = self.executor.execute(
                    incident, incident.proposed_action
                )
            except ExecutionRefused as exc:
                machine.note("execution_refused", "remediation_agent", error=str(exc))
                machine.transition(IncidentState.REJECTED, "remediation_agent",
                                   reason=str(exc))
                return

        text = (
            f"AccessPulse {incident.incident_id}: approved action "
            f"{incident.proposed_action.action_type.value} on "
            f"{incident.proposed_action.target} executed by "
            f"{incident.approval.approver if incident.approval else 'policy-automatic'}. "
            f"Scope: {incident.proposed_action.scope_digest}."
        )
        ev = await self.evidence_agent.record_action(incident, text)
        if ev:
            incident.evidence.append(ev)
        machine.transition(IncidentState.VERIFYING, "remediation_agent")

    # -- 8. verify ---------------------------------------------------------
    async def verify(self, incident: Incident, settle_seconds: float = 20.0) -> bool:
        machine = self.machine(incident.incident_id)
        assert incident.scope is not None and incident.proposed_action is not None
        self.sim.advance(settle_seconds)

        with self._step("verification_agent", "verify", incident.incident_id):
            incident.assertions = run_suite(
                incident.proposed_action.verification_suite, self.sim, incident.scope
            )
        ok = all_mandatory_passed(incident.assertions)

        slo = incident.alert.labels["slo"] if incident.alert else "cap.drift"
        # refresh the metric store from the post-action environment so the
        # recovery query through MCP reads live data, not the pre-action series
        self.assurance.sweep(
            languages=list(incident.scope.languages) or None,
            territories=list(incident.scope.territories) or None,
            player_versions=list(incident.scope.player_versions) or None,
        )
        publish_metrics(self.assurance, self.telemetry)
        incident.evidence.extend(
            await self.evidence_agent.verify_recovery(incident, slo)
        )

        if ok:
            machine.transition(IncidentState.RECOVERED, "verification_agent",
                               assertions_passing=len(incident.assertions))
            incident.timings["time_to_recovery_s"] = (
                time.perf_counter() - self._t0[incident.incident_id]
            )
        else:
            bad = [a.name for a in failing(incident.assertions) if a.mandatory]
            machine.note("verification_failed", "verification_agent", failing=bad)
            if incident.proposed_action.rollback_behaviour.startswith("auto_rollback"):
                self.executor.rollback(incident, incident.proposed_action)
                machine.note("rolled_back", "remediation_agent")
            machine.transition(IncidentState.ROLLED_BACK, "verification_agent", failing=bad)
        return ok

    # -- 9. communicate + review ------------------------------------------
    def communicate(self, incident: Incident, recovered: bool = True) -> None:
        machine = self.machine(incident.incident_id)
        with self._step(self.communication_agent.name, "compose", incident.incident_id):
            incident.communications = self.communication_agent.run(incident, recovered)
        machine.transition(IncidentState.COMMUNICATED, self.communication_agent.name,
                           audiences=[c.audience for c in incident.communications])
        incident.timings["time_to_communication_s"] = (
            time.perf_counter() - self._t0[incident.incident_id]
        )

    def review(
        self, incident: Incident, ground_truth: FailureClass | None = None
    ) -> PostIncidentReview:
        machine = self.machine(incident.incident_id)
        with self._step(self.learning_agent.name, "review", incident.incident_id):
            review = self.learning_agent.run(
                incident, ground_truth, mcp_calls=len(self.mcp.call_log)
            )
        self.reviews[incident.incident_id] = review
        machine.transition(IncidentState.REVIEWED, self.learning_agent.name,
                           root_cause=review.root_cause.value,
                           diagnosis_correct=review.diagnosis_correct)
        slo = incident.alert.labels["slo"] if incident.alert else None
        if slo and self.open_by_slo.get(slo) == incident.incident_id:
            del self.open_by_slo[slo]
        return review


class _StepTimer:
    """Times an agent step, records it as telemetry and as a span."""

    def __init__(self, coord: IncidentCoordinator, agent: str, step: str,
                 incident_id: str) -> None:
        self.coord = coord
        self.agent = agent
        self.step = step
        self.incident_id = incident_id

    def __enter__(self):
        self._t = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        duration_ms = (time.perf_counter() - self._t) * 1000.0
        self.coord.telemetry.record_agent_step(self.agent, self.step, duration_ms)
        self.coord.telemetry.traces.record(
            name=f"{self.agent}.{self.step}",
            service="accesspulse-agent",
            duration_ms=duration_ms,
            trace_id=self.coord.trace_ids.get(self.incident_id),
            attributes={"incident_id": self.incident_id, "agent": self.agent,
                        "step": self.step, "error": bool(exc)},
            status="ok" if exc is None else "error",
        )
        return False


def publish_metrics(assurance: LiveAssurance, telemetry: TelemetryPlane) -> int:
    """Push the latest sweep into the metric store the MCP server reads."""
    count = 0
    now = assurance.sim.wall_clock
    for (name, labels), value in assurance.metrics.items():
        telemetry.metrics.record(name, value, dict(labels), now)
        count += 1
    # SLO state + budget
    for e in assurance.last_evaluations:
        telemetry.metrics.record(
            "accesspulse_slo_breached", 1.0 if e.breached else 0.0,
            {"slo": e.slo_id, **{k: v for k, v in e.labels.items()
                                 if k in ("language", "territory", "platform",
                                          "player_version")}},
            now,
        )
        count += 1
    for b in assurance.ledger.all():
        telemetry.metrics.record("accesspulse_error_budget_consumed_ratio",
                                 b.consumed_fraction, {"slo": b.slo_id}, now)
        count += 1
    # session aggregates
    for obs, _ in assurance.last_reports:
        labels = {"language": obs.language, "territory": obs.territory,
                  "platform": obs.platform, "player_version": obs.player_version}
        telemetry.metrics.record("accesspulse_sessions_caption_enabled",
                                 obs.sessions_with_captions, labels, now)
        telemetry.metrics.record("accesspulse_sessions_description_enabled",
                                 obs.sessions_with_description, labels, now)
        telemetry.metrics.record("accesspulse_sessions_sign_enabled",
                                 obs.sessions_with_sign, labels, now)
        telemetry.metrics.record("accesspulse_transport_packet_loss_ratio",
                                 obs.transport.packet_loss_ratio, labels, now)
        telemetry.metrics.record("accesspulse_encoder_cpu_utilisation",
                                 obs.transport.encoder_cpu, labels, now)
        telemetry.metrics.record("accesspulse_gpu_utilisation",
                                 obs.transport.gpu_utilisation, labels, now)
        count += 6
    return count
