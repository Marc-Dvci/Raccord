"""The executor is the only thing that can change the environment.

These tests probe the boundary directly rather than through the loop: they hand
the executor actions a confused or hostile agent might construct and assert that
nothing moves. See docs/THREAT_MODEL.md sections 4.1, 4.4, 4.5 and 4.7.
"""

from __future__ import annotations

import pytest

from raccord.approvals import ApprovalService
from raccord.contracts import (
    ActionType,
    FeatureType,
    Incident,
    Platform,
    PolicyClass,
    PolicyDecision,
    ProposedAction,
    Role,
    Scope,
    SLOTier,
)
from raccord.executor import ExecutionRefused, RemediationExecutor
from raccord.policy import ACTION_CATALOG, POLICY_VERSION, PolicyContext, evaluate
from raccord.simulator import MediaSimulator


def _scope(**overrides) -> Scope:
    base = dict(
        incident_id="inc-1",
        features=(FeatureType.CAPTIONS,),
        languages=("en",),
        territories=("FR",),
        platforms=(Platform.CTV,),
        device_classes=(),
        player_versions=("ctv-9.4.0",),
        cdn_regions=("eu-west",),
        providers=("verbaflow",),
        components=("capenc-pool-a",),
        violated_promise_ids=("pr-captions-en",),
        affected_sessions=1000,
        protected_sessions=9000,
        blast_class="local",
    )
    base.update(overrides)
    return Scope(**base)


def _action(
    action_type=ActionType.SELECT_SYNCHRONIZED_STANDBY, target="capenc-pool-b", **kw
) -> ProposedAction:
    return ProposedAction(
        action_id="act-1", incident_id="inc-1", action_type=action_type, target=target, **kw
    )


def _incident(action: ProposedAction, decision: PolicyDecision, **kw) -> Incident:
    return Incident(
        incident_id="inc-1",
        event_id="evt-1",
        title="test",
        scope=_scope(),
        proposed_action=action,
        policy_decision=decision,
        **kw,
    )


def _decide(
    action: ProposedAction, live: bool = False, tier: SLOTier = SLOTier.TIER_2_VOD_PREMIUM
) -> PolicyDecision:
    return evaluate(
        action,
        PolicyContext(tier=tier, live=live, scope=_scope(), operator_roles=(Role.STREAMING_SRE,)),
    )


@pytest.fixture
def executor(sim: MediaSimulator) -> RemediationExecutor:
    return RemediationExecutor(sim=sim, approvals=ApprovalService())


# --- the catalog is the whole action surface --------------------------------


def test_every_action_type_has_a_catalog_entry():
    """P-001 is the backstop; this is the invariant that keeps it unreachable."""
    missing = [a.value for a in ActionType if a not in ACTION_CATALOG]
    assert not missing, f"action types with no catalog entry: {missing}"


def test_an_uncatalogued_action_is_prohibited_and_refused(executor, monkeypatch):
    """If an action type ever ships without a catalog entry, both the policy
    engine and the executor must fail closed - independently."""
    action = _action()
    decision = _decide(action)
    monkeypatch.delitem(ACTION_CATALOG, ActionType.SELECT_SYNCHRONIZED_STANDBY)

    assert (
        evaluate(
            action,
            PolicyContext(
                tier=SLOTier.TIER_2_VOD_PREMIUM,
                live=False,
                scope=_scope(),
                operator_roles=(Role.STREAMING_SRE,),
            ),
        ).classification
        is PolicyClass.PROHIBITED
    )

    # The executor re-checks rather than trusting the decision it was handed.
    with pytest.raises(ExecutionRefused, match="not in the catalog"):
        executor.execute(_incident(action, decision), action)


def test_a_target_outside_the_allow_list_is_refused_by_the_executor(executor):
    """Even with a hand-crafted 'automatic' decision, the target is re-checked."""
    action = _action(target="capenc-pool-z")
    forged = PolicyDecision(
        decision_id="dec-forged",
        incident_id="inc-1",
        action_id="act-1",
        classification=PolicyClass.AUTOMATIC,
        required_roles=(),
        rationale=("forged",),
        policy_version=POLICY_VERSION,
    )
    with pytest.raises(ExecutionRefused, match="not allow-listed"):
        executor.execute(_incident(action, forged), action)


# --- policy and approval ----------------------------------------------------


def test_a_prohibited_action_is_refused(executor):
    action = _action(ActionType.RESTART_SIGN_PIPELINE, "signsrc-lsf")
    decision = _decide(action)  # scope is captions-only, so P-003 prohibits
    assert decision.classification is PolicyClass.PROHIBITED
    with pytest.raises(ExecutionRefused, match="policy prohibits"):
        executor.execute(_incident(action, decision), action)


def test_approval_required_without_a_token_is_refused(executor):
    action = _action()
    decision = _decide(action, live=True, tier=SLOTier.TIER_0_GLOBAL_LIVE)
    assert decision.classification is PolicyClass.APPROVAL_REQUIRED
    with pytest.raises(ExecutionRefused, match="no token presented"):
        executor.execute(_incident(action, decision), action)


def test_a_decision_for_a_different_action_is_refused(executor):
    """A decision cannot be reused to authorise a different action."""
    approved = _action()
    other = _action(ActionType.REROUTE_CAPTION_PATH, "capdist-secondary")
    other = other.model_copy(update={"action_id": "act-2"})
    decision = _decide(approved)
    with pytest.raises(ExecutionRefused, match="does not match"):
        executor.execute(_incident(other, decision), other)


# --- idempotency ------------------------------------------------------------


def test_a_duplicate_idempotency_key_executes_nothing(executor):
    action = _action(idempotency_key="inc-1:standby")
    decision = _decide(action)
    assert decision.classification is PolicyClass.AUTOMATIC
    incident = _incident(action, decision)

    first = executor.execute(incident, action)
    assert first.executed is True

    second = executor.execute(incident, action)
    assert second.executed is False
    assert second.error == "duplicate_idempotency_key"
    assert [e["event"] for e in executor.log] == ["executed", "duplicate_suppressed"]


def test_the_environment_changes_exactly_once(executor, sim):
    action = _action()
    decision = _decide(action)
    incident = _incident(action, decision)

    before = sim.state_snapshot()
    result = executor.execute(incident, action)
    after = sim.state_snapshot()

    assert result.executed is True
    assert before != after, "an executed action must change the environment"
    assert result.before_state == before
    assert result.after_state == after

    # a replay of the same action hash leaves the environment untouched
    executor.execute(incident, action)
    assert sim.state_snapshot() == after


def test_rollback_restores_the_recorded_pre_action_state(executor, sim):
    action = _action()
    decision = _decide(action)
    incident = _incident(action, decision)

    before = sim.state_snapshot()
    result = executor.execute(incident, action)
    assert sim.state_snapshot() != before

    incident = incident.model_copy(update={"action_result": result})
    executor.rollback(incident, action)

    restored = sim.state_snapshot()
    for key in (
        "caption_encoder_pool",
        "clock_source",
        "caption_path",
        "pinned_player_versions",
        "rerouted_regions",
        "disabled_feature_flags",
    ):
        assert restored[key] == before[key], key


def test_rollback_without_an_executed_action_is_refused(executor):
    action = _action()
    with pytest.raises(ExecutionRefused, match="nothing to roll back"):
        executor.rollback(_incident(action, _decide(action)), action)
