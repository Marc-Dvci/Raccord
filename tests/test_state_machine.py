"""The incident state machine must be impossible to talk out of its own rules."""

from __future__ import annotations

import pytest

from raccord.contracts import (
    Alert,
    AssertionStatus,
    Incident,
    IncidentState,
    Severity,
    VerificationAssertion,
    utcnow,
)
from raccord.incident import IncidentMachine, IncidentStateError


def _incident() -> Incident:
    return Incident(incident_id="inc-test", event_id="evt", title="test")


def _alert() -> Alert:
    return Alert(
        alert_id="a1",
        rule_uid="r1",
        rule_title="t",
        state="firing",
        severity=Severity.SEV2,
        fired_at=utcnow(),
        labels={"slo": "cap.drift", "feature": "captions"},
    )


def test_illegal_transition_is_refused():
    m = IncidentMachine(_incident())
    with pytest.raises(IncidentStateError) as exc:
        m.transition(IncidentState.RECOVERED)
    assert "not a legal transition" in str(exc.value)
    assert m.state is IncidentState.DETECTED


def test_states_cannot_be_skipped():
    m = IncidentMachine(_incident())
    m.incident.alert = _alert()
    m.transition(IncidentState.QUALIFIED)
    with pytest.raises(IncidentStateError):
        m.transition(IncidentState.EVIDENCE_COMPLETE)


def test_qualify_requires_a_firing_alert():
    m = IncidentMachine(_incident())
    ok, unmet = m.can(IncidentState.QUALIFIED)
    assert not ok and "no alert attached" in unmet[0]


def test_recovered_requires_every_mandatory_assertion():
    inc = _incident()
    inc.state = IncidentState.VERIFYING
    inc.assertions = [
        VerificationAssertion(
            assertion_id="v1",
            incident_id=inc.incident_id,
            name="a",
            description="",
            mandatory=True,
            status=AssertionStatus.PASSING,
        ),
        VerificationAssertion(
            assertion_id="v2",
            incident_id=inc.incident_id,
            name="b",
            description="",
            mandatory=True,
            status=AssertionStatus.FAILING,
        ),
    ]
    m = IncidentMachine(inc)
    ok, unmet = m.can(IncidentState.RECOVERED)
    assert not ok
    assert any("mandatory assertion 'b'" in u for u in unmet)


def test_rollback_requires_a_real_failure():
    inc = _incident()
    inc.state = IncidentState.VERIFYING
    inc.assertions = [
        VerificationAssertion(
            assertion_id="v1",
            incident_id=inc.incident_id,
            name="a",
            description="",
            mandatory=True,
            status=AssertionStatus.PASSING,
        ),
    ]
    m = IncidentMachine(inc)
    ok, unmet = m.can(IncidentState.ROLLED_BACK)
    assert not ok and "not justified" in unmet[0]


def test_audit_chain_detects_tampering():
    m = IncidentMachine(_incident())
    m.incident.alert = _alert()
    m.transition(IncidentState.QUALIFIED)
    assert m.verify_audit_chain()
    tampered = m.incident.audit[-1].model_copy(update={"actor": "someone-else"})
    m.incident.audit[-1] = tampered
    assert not m.verify_audit_chain()


def test_rejected_transition_is_recorded_even_when_refused():
    m = IncidentMachine(_incident())
    with pytest.raises(IncidentStateError):
        m.transition(IncidentState.RECOVERED)
    assert m.incident.audit[-1].event == "transition.rejected"
    assert m.verify_audit_chain()
