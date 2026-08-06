"""Policy as code: the classification must not depend on who is asking."""

from __future__ import annotations

from accesspulse.contracts import (
    ActionType,
    FeatureType,
    Platform,
    PolicyClass,
    ProposedAction,
    Role,
    Scope,
    SLOTier,
)
from accesspulse.policy import ACTION_CATALOG, PolicyContext, evaluate


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


def _action(action_type=ActionType.SELECT_SYNCHRONIZED_STANDBY, target="capenc-pool-b"):
    return ProposedAction(action_id="act-1", incident_id="inc-1",
                          action_type=action_type, target=target)


def test_live_tier0_requires_approval():
    decision = evaluate(_action(), PolicyContext(
        tier=SLOTier.TIER_0_GLOBAL_LIVE, live=True, scope=_scope(),
        operator_roles=(Role.OPERATOR,)))
    assert decision.classification is PolicyClass.APPROVAL_REQUIRED
    assert Role.TECHNICAL_DIRECTOR in decision.required_roles


def test_narrow_offline_recovery_may_run_automatically():
    decision = evaluate(_action(), PolicyContext(
        tier=SLOTier.TIER_2_VOD_PREMIUM, live=False, scope=_scope(),
        operator_roles=(Role.STREAMING_SRE,)))
    assert decision.classification is PolicyClass.AUTOMATIC


def test_target_outside_the_allow_list_is_prohibited():
    decision = evaluate(_action(target="capenc-pool-z"), PolicyContext(
        tier=SLOTier.TIER_2_VOD_PREMIUM, live=False, scope=_scope(),
        operator_roles=(Role.STREAMING_SRE,)))
    assert decision.classification is PolicyClass.PROHIBITED
    assert "P-002" in decision.violated_rules


def test_action_irrelevant_to_the_broken_feature_is_prohibited():
    decision = evaluate(
        _action(ActionType.RESTART_SIGN_PIPELINE, "signsrc-lsf"),
        PolicyContext(tier=SLOTier.TIER_2_VOD_PREMIUM, live=False,
                      scope=_scope(features=(FeatureType.CAPTIONS,)),
                      operator_roles=(Role.STREAMING_SRE,)))
    assert decision.classification is PolicyClass.PROHIBITED
    assert "P-003" in decision.violated_rules


def test_change_freeze_blocks_operational_change_but_not_communication():
    ctx = PolicyContext(tier=SLOTier.TIER_2_VOD_PREMIUM, live=False, scope=_scope(),
                        operator_roles=(Role.STREAMING_SRE,), change_freeze=True)
    assert evaluate(_action(), ctx).classification is PolicyClass.PROHIBITED
    comms = evaluate(_action(ActionType.ISSUE_STATUS_UPDATE, "public-status"), ctx)
    assert comms.classification is PolicyClass.APPROVAL_REQUIRED


def test_multi_territory_requires_approval_outside_tier0():
    decision = evaluate(_action(), PolicyContext(
        tier=SLOTier.TIER_2_VOD_PREMIUM, live=False,
        scope=_scope(territories=("FR", "DE", "ES", "GB"), blast_class="regional"),
        operator_roles=(Role.STREAMING_SRE,)))
    assert decision.classification is PolicyClass.APPROVAL_REQUIRED


def test_public_communication_always_needs_the_support_lead():
    decision = evaluate(
        _action(ActionType.ISSUE_STATUS_UPDATE, "public-status"),
        PolicyContext(tier=SLOTier.TIER_3_CATALOG, live=False, scope=_scope(),
                      operator_roles=(Role.OPERATOR,)))
    assert decision.classification is PolicyClass.APPROVAL_REQUIRED
    assert Role.SUPPORT_LEAD in decision.required_roles


def test_every_catalogued_action_declares_a_verification_suite():
    from accesspulse.verification import SUITES

    for spec in ACTION_CATALOG.values():
        assert spec.verification_suite in SUITES, spec.action_type
