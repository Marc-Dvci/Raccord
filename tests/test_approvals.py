"""Approval tokens are the only thing standing between a proposal and production."""

from __future__ import annotations

from datetime import timedelta

import pytest

from accesspulse.approvals import ApprovalError, ApprovalService
from accesspulse.contracts import ActionType, ProposedAction, Role, utcnow


def _action(**overrides) -> ProposedAction:
    base = dict(
        action_id="act-1",
        incident_id="inc-1",
        action_type=ActionType.SELECT_SYNCHRONIZED_STANDBY,
        target="capenc-pool-b",
        parameters={},
    )
    base.update(overrides)
    return ProposedAction(**base)


def _issue(service: ApprovalService, action: ProposedAction, evidence="evhash"):
    return service.issue(action, evidence, "td@studio.example", Role.TECHNICAL_DIRECTOR,
                         (Role.TECHNICAL_DIRECTOR,))


def test_valid_token_redeems_once():
    s = ApprovalService()
    a = _action()
    approval = _issue(s, a)
    s.redeem(approval.token, a, "evhash")
    with pytest.raises(ApprovalError, match="already been redeemed"):
        s.redeem(approval.token, a, "evhash")


def test_wrong_role_cannot_approve():
    s = ApprovalService()
    a = _action()
    with pytest.raises(ApprovalError, match="requires one of"):
        s.issue(a, "evhash", "support@studio.example", Role.SUPPORT_LEAD,
                (Role.TECHNICAL_DIRECTOR,))


def test_changing_the_action_after_approval_invalidates_it():
    s = ApprovalService()
    a = _action()
    approval = _issue(s, a)
    widened = _action(target="capenc-pool-b", parameters={"scope": "all-territories"})
    with pytest.raises(ApprovalError, match="action hash mismatch"):
        s.redeem(approval.token, widened, "evhash")


def test_changing_the_evidence_after_approval_invalidates_it():
    s = ApprovalService()
    a = _action()
    approval = _issue(s, a)
    with pytest.raises(ApprovalError, match="evidence hash mismatch"):
        s.redeem(approval.token, a, "a-different-evidence-hash")


def test_expired_token_is_refused():
    s = ApprovalService(ttl_seconds=1)
    a = _action()
    approval = _issue(s, a)
    with pytest.raises(ApprovalError, match="expired"):
        s.redeem(approval.token, a, "evhash", now=utcnow() + timedelta(seconds=5))


def test_forged_signature_is_refused():
    s = ApprovalService()
    a = _action()
    approval = _issue(s, a)
    payload, _sig = approval.token.split(".")
    forged = f"{payload}.{'A' * 43}"
    with pytest.raises(ApprovalError, match="signature is invalid"):
        s.redeem(forged, a, "evhash")


def test_token_from_another_service_is_refused():
    a = _action()
    issuer = ApprovalService()
    approval = _issue(issuer, a)
    other = ApprovalService()
    other._issued.clear()
    with pytest.raises(ApprovalError):
        other.redeem(approval.token, a, "evhash")


def test_every_decision_is_audited():
    s = ApprovalService()
    a = _action()
    approval = _issue(s, a)
    s.redeem(approval.token, a, "evhash")
    events = [e["event"] for e in s.audit]
    assert events == ["issued", "redeemed"]
