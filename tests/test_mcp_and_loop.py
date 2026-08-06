"""End-to-end: the closed loop, and the Grafana MCP dependency that carries it."""

from __future__ import annotations

import pytest

from accesspulse.contracts import IncidentState
from accesspulse.grafana_mcp.client import CAPABILITIES
from accesspulse.runtime import AccessPulseRuntime
from tests.conftest import BENCH_SWEEP


async def _drifting_runtime(fault="cap.progressive_drift") -> AccessPulseRuntime:
    rt = AccessPulseRuntime(db_prefix="test_loop")
    await rt.connect()
    rt.tick(20, **BENCH_SWEEP)
    rt.inject(fault)
    for _ in range(7):
        rt.tick(25, **BENCH_SWEEP)
    return rt


async def test_every_required_capability_resolves():
    rt = AccessPulseRuntime(db_prefix="test_mcp")
    await rt.connect()
    for cap in CAPABILITIES:
        if cap.required:
            assert rt.mcp.has(cap.key), cap.key
    await rt.aclose()


async def test_hero_incident_closes_with_every_assertion_passing():
    rt = await _drifting_runtime()
    result = await rt.run_incident()
    incident = result.incident

    assert result.detected
    assert result.diagnosis_correct
    assert incident.state is IncidentState.REVIEWED
    assert result.recovered and not result.rolled_back
    assert result.assertions_total > 0
    assert result.assertions_passing == result.assertions_total
    assert not result.unsafe_action
    assert rt.coordinator.machine(incident.incident_id).verify_audit_chain()
    await rt.aclose()


async def test_scope_matches_the_fault_it_was_never_shown():
    rt = await _drifting_runtime()
    result = await rt.run_incident()
    scope = result.incident.scope
    assert set(scope.territories) <= {"FR", "DE", "ES", "GB"}
    assert all(pv.startswith("ctv") for pv in scope.player_versions)
    assert set(scope.languages) == {"en"}
    assert result.scope_precision == 1.0
    await rt.aclose()


async def test_the_investigation_goes_through_mcp_and_only_through_mcp():
    rt = await _drifting_runtime()
    result = await rt.run_incident()
    tools = {e.source_tool for e in result.incident.evidence}
    assert tools, "no evidence collected"
    assert all(t.startswith("grafana.mcp:") for t in tools), tools

    ok, missing = rt.coordinator.evidence_agent.chain_complete()
    assert ok, f"incomplete MCP chain: {missing}"
    called = [c["tool"] for c in rt.mcp.call_log]
    for required in ("list_alert_rules", "query_prometheus", "query_loki_logs",
                     "query_tempo_traces", "search_dashboards", "create_annotation"):
        assert required in called, required
    await rt.aclose()


async def test_evidence_complete_is_unreachable_without_mcp_evidence():
    """Strip the MCP-sourced evidence and the state machine must refuse to advance."""
    rt = await _drifting_runtime()
    pairs = rt.coordinator.detect(rt.fault_onset)
    alert, group = rt._pick_primary(pairs, None)
    incident = rt.coordinator.open_incident(alert, group, rt.fault_onset)
    await rt.coordinator.investigate(incident)

    machine = rt.coordinator.machine(incident.incident_id)
    incident.state = IncidentState.SCOPED
    incident.evidence = [e for e in incident.evidence
                         if "query_prometheus" not in e.source_tool]
    ok, unmet = machine.can(IncidentState.EVIDENCE_COMPLETE)
    assert not ok
    assert any("query_prometheus" in u for u in unmet)
    await rt.aclose()


async def test_remediation_without_approval_is_refused():
    from accesspulse.executor import ExecutionRefused

    rt = await _drifting_runtime()
    pairs = rt.coordinator.detect(rt.fault_onset)
    alert, group = rt._pick_primary(pairs, None)
    incident = rt.coordinator.open_incident(alert, group, rt.fault_onset)
    await rt.coordinator.investigate(incident)
    rt.coordinator.diagnose(incident)
    rt.coordinator.evaluate_policy(incident, live=True)

    assert incident.state is IncidentState.AWAITING_APPROVAL
    assert incident.approval is None
    with pytest.raises(ExecutionRefused, match="approval required"):
        rt.executor.execute(incident, incident.proposed_action)
    assert rt.sim.caption_encoder_pool == "capenc-pool-a"  # nothing changed
    await rt.aclose()


async def test_a_wrong_action_is_caught_by_verification_and_rolled_back():
    """The clock fault is not fixed by republishing the manifest. Verification
    must notice, and the incident must not close."""
    from accesspulse.contracts import ActionType

    rt = await _drifting_runtime()
    pairs = rt.coordinator.detect(rt.fault_onset)
    alert, group = rt._pick_primary(pairs, None)
    incident = rt.coordinator.open_incident(alert, group, rt.fault_onset)
    await rt.coordinator.investigate(incident)
    rt.coordinator.diagnose(incident)
    rt.coordinator.evaluate_policy(incident, live=True)

    incident.proposed_action = incident.proposed_action.model_copy(update={
        "action_type": ActionType.REPUBLISH_MANIFEST,
        "target": "manifest-main",
    })
    incident.policy_decision = incident.policy_decision.model_copy(update={
        "action_id": incident.proposed_action.action_id,
    })
    decision_roles = incident.policy_decision.required_roles
    rt.coordinator.approve(incident, "td@studio.example", decision_roles[0])
    await rt.coordinator.remediate(incident)
    recovered = await rt.coordinator.verify(incident)

    assert not recovered
    assert incident.state is IncidentState.ROLLED_BACK
    await rt.aclose()


async def test_reset_is_deterministic():
    rt = AccessPulseRuntime(db_prefix="test_reset")
    await rt.connect()
    before = rt.twin.topology_hash()
    rt.tick(20, **BENCH_SWEEP)
    rt.inject("cap.progressive_drift")
    rt.tick(40, **BENCH_SWEEP)
    rt.reset()
    await rt.connect()
    assert rt.twin.topology_hash() == before
    assert rt.sim.program_s == 0.0
    assert rt.sim.active_faults == []
    assert rt.sim.caption_encoder_pool == "capenc-pool-a"
    await rt.aclose()


async def test_public_status_contains_no_internal_detail():
    rt = await _drifting_runtime()
    result = await rt.run_incident()
    public = [c for c in result.incident.communications if c.audience == "public_status"]
    assert public
    body = public[0].body.lower()
    for leak in ("capenc-pool", "clock-ptp", "signsrc", "packager", "posterior",
                 "hypothesis", "encoder"):
        assert leak not in body, leak
    assert not public[0].contains_internal_detail
    await rt.aclose()


async def test_audience_impact_is_measured_on_the_programme_clock():
    """The two clocks must not be confused.

    `time_to_detect_s` and `outage_seconds` describe what an audience lived
    through, on the twin's programme clock. `time_to_recovery_s` is a stopwatch
    on the agent's own work. Reporting the second as an outage duration once put
    "degraded for 0.0 minutes" in front of an executive.
    """
    rt = await _drifting_runtime()
    result = await rt.run_incident()
    timings = result.incident.timings

    outage = timings["outage_seconds"]
    assert outage >= timings["time_to_detect_s"] > 0
    # The agent's own working time is orders of magnitude smaller, which is
    # exactly why it cannot stand in for the audience-visible duration.
    assert timings["time_to_recovery_s"] < outage

    executive = [c for c in result.incident.communications if c.audience == "executive"]
    assert executive
    assert "for 0.0 minutes" not in executive[0].body
    assert "0 seconds" not in executive[0].body
    await rt.aclose()
