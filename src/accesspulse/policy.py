"""Policy as code, and the action catalog it governs.

Nothing consequential happens in AccessPulse unless a rule in this module allows
it. The agents can *propose*; only the policy engine can classify a proposal as
automatic, approval-required or prohibited, and only an executor holding a valid
approval token for that exact action hash can run it.

Rules are ordinary Python predicates over typed records - readable by an auditor,
testable in CI, and versioned. `POLICY_VERSION` is stamped onto every decision so
an incident can always be re-evaluated under the policy that was in force.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .contracts import (
    ActionType,
    FeatureType,
    PolicyClass,
    PolicyDecision,
    ProposedAction,
    Role,
    Scope,
    SLOTier,
    utcnow,
)

POLICY_VERSION = "accesspulse-policy-2026.08.1"


# ---------------------------------------------------------------------------
# Action catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionSpec:
    action_type: ActionType
    title: str
    description: str
    features: tuple[FeatureType, ...]
    preconditions: tuple[str, ...]
    allowed_targets: tuple[str, ...]
    default_required_role: Role
    expected_metric_change: dict[str, str]
    verification_suite: str
    rollback_behaviour: str
    audience_visible: bool = False
    max_blast_class: str = "systemic"  # widest scope this action may be used on


ACTION_CATALOG: dict[ActionType, ActionSpec] = {
    ActionType.SELECT_SYNCHRONIZED_STANDBY: ActionSpec(
        ActionType.SELECT_SYNCHRONIZED_STANDBY,
        "Cut captions to the synchronised standby encoder pool",
        "Moves caption encoding to the standby pool, which holds its own lock to the "
        "primary timing reference. Designed for live use: the standby is pre-warmed and "
        "already ingesting, so the switch is frame-accurate.",
        (FeatureType.CAPTIONS,),
        ("standby pool healthy", "standby pool clock locked", "no conflicting action in flight"),
        ("capenc-pool-b",),
        Role.TECHNICAL_DIRECTOR,
        {"accesspulse_caption_drift_seconds": "falls below 1.5 s within 60 s",
         "accesspulse_caption_track_available_ratio": "remains >= 0.999"},
        "caption_recovery",
        "auto_rollback_on_verification_failure",
    ),
    ActionType.SWITCH_CAPTION_ENCODER_POOL: ActionSpec(
        ActionType.SWITCH_CAPTION_ENCODER_POOL,
        "Switch caption encoder pool",
        "Moves caption encoding to another pool without requiring a synchronised standby.",
        (FeatureType.CAPTIONS,),
        ("target pool healthy", "target pool has capacity"),
        ("capenc-pool-b",),
        Role.STREAMING_SRE,
        {"accesspulse_caption_track_available_ratio": "returns to >= 0.999"},
        "caption_recovery",
        "auto_rollback_on_verification_failure",
    ),
    ActionType.CHANGE_CLOCK_SOURCE: ActionSpec(
        ActionType.CHANGE_CLOCK_SOURCE,
        "Change the timing reference",
        "Repoints the caption, described-audio and sign chain at a different clock source.",
        (FeatureType.CAPTIONS, FeatureType.SIGN_LANGUAGE, FeatureType.AUDIO_DESCRIPTION,
         FeatureType.ALTERNATE_AUDIO),
        ("target clock reachable", "target clock offset within 5 ms"),
        ("clock-ptp-primary", "clock-ntp-fallback"),
        Role.STREAMING_SRE,
        {"accesspulse_caption_drift_seconds": "falls below 1.5 s",
         "accesspulse_sign_sync_drift_seconds": "falls below 0.5 s"},
        "caption_recovery",
        "auto_rollback_on_verification_failure",
    ),
    ActionType.REROUTE_CAPTION_PATH: ActionSpec(
        ActionType.REROUTE_CAPTION_PATH,
        "Reroute the caption path",
        "Sends caption data over the secondary contribution path.",
        (FeatureType.CAPTIONS,),
        ("secondary path healthy",),
        ("secondary",),
        Role.STREAMING_SRE,
        {"accesspulse_caption_track_available_ratio": "returns to >= 0.999"},
        "caption_recovery",
        "auto_rollback_on_verification_failure",
    ),
    ActionType.RESTORE_KNOWN_GOOD_PLAYER: ActionSpec(
        ActionType.RESTORE_KNOWN_GOOD_PLAYER,
        "Roll traffic back to the last known-good player build",
        "Pins affected devices to the previous player version.",
        (FeatureType.ACCESSIBLE_PLAYER, FeatureType.CAPTIONS,
         FeatureType.AUDIO_DESCRIPTION, FeatureType.ALTERNATE_AUDIO,
         FeatureType.ACCESSIBLE_AUTH, FeatureType.ACCESSIBLE_PURCHASE),
        ("known-good version identified", "version is still published"),
        ("ctv-9.4.0", "web-4.12.0", "ios-6.2.0", "android-6.2.0"),
        Role.STREAMING_SRE,
        {"accesspulse_player_keyboard_completion_ratio": "returns to 1.0",
         "accesspulse_caption_render_success_ratio": "returns to >= 0.999"},
        "player_recovery",
        "auto_rollback_on_verification_failure",
    ),
    ActionType.REPUBLISH_MANIFEST: ActionSpec(
        ActionType.REPUBLISH_MANIFEST,
        "Republish a corrected manifest",
        "Regenerates the manifest with the promised accessibility renditions declared.",
        (FeatureType.CAPTIONS, FeatureType.AUDIO_DESCRIPTION, FeatureType.ALTERNATE_AUDIO),
        ("all promised tracks present at origin",),
        ("manifest-main",),
        Role.STREAMING_SRE,
        {"accesspulse_ad_track_declared_ratio": "returns to 1.0"},
        "manifest_recovery",
        "auto_rollback_on_verification_failure",
    ),
    ActionType.REROUTE_REGION: ActionSpec(
        ActionType.REROUTE_REGION,
        "Reroute a CDN region",
        "Moves a region's traffic to a healthy neighbouring region.",
        (FeatureType.CAPTIONS, FeatureType.SIGN_LANGUAGE, FeatureType.AUDIO_DESCRIPTION),
        ("target region has headroom", "latency impact acceptable"),
        ("region-eu-west", "region-eu-central", "region-us-east", "region-us-west",
         "region-sa-east", "region-ap-northeast"),
        Role.STREAMING_SRE,
        {"accesspulse_caption_track_available_ratio": "returns to >= 0.999"},
        "delivery_recovery",
        "auto_rollback_on_verification_failure",
    ),
    ActionType.RESTORE_AUDIO_TRACK: ActionSpec(
        ActionType.RESTORE_AUDIO_TRACK,
        "Restore an omitted audio track",
        "Re-adds a described or alternate-language audio rendition and republishes.",
        (FeatureType.AUDIO_DESCRIPTION, FeatureType.ALTERNATE_AUDIO),
        ("track present at origin",),
        ("en-desc", "fr-desc", "fr"),
        Role.A11Y_SPECIALIST,
        {"accesspulse_ad_track_declared_ratio": "returns to 1.0"},
        "manifest_recovery",
        "auto_rollback_on_verification_failure",
    ),
    ActionType.SWITCH_ALTERNATE_LANGUAGE_SOURCE: ActionSpec(
        ActionType.SWITCH_ALTERNATE_LANGUAGE_SOURCE,
        "Switch to an approved alternate language source",
        "Falls back to the alternate source named in the promise's approved fallback.",
        (FeatureType.CAPTIONS, FeatureType.AUDIO_DESCRIPTION, FeatureType.ALTERNATE_AUDIO),
        ("fallback is named in the effective promise", "fallback source healthy"),
        ("capsrc-en", "capsrc-fr", "capsrc-de", "capsrc-es", "adsrc-en", "adsrc-fr"),
        Role.A11Y_SPECIALIST,
        {"accesspulse_caption_semantic_score": "returns above objective"},
        "caption_recovery",
        "auto_rollback_on_verification_failure",
    ),
    ActionType.RESTART_SIGN_PIPELINE: ActionSpec(
        ActionType.RESTART_SIGN_PIPELINE,
        "Restart a sign-language pipeline component",
        "Restarts the interpreter feed ingest or transcode component.",
        (FeatureType.SIGN_LANGUAGE,),
        ("interpreter feed source still live", "restart window agreed with the interpreter"),
        ("signsrc-lsf",),
        Role.A11Y_SPECIALIST,
        {"accesspulse_sign_frozen_frame_ratio": "falls below 0.01",
         "accesspulse_sign_fps": "returns to >= 45"},
        "sign_recovery",
        "auto_rollback_on_verification_failure",
    ),
    ActionType.DISABLE_PLAYER_FEATURE_FLAG: ActionSpec(
        ActionType.DISABLE_PLAYER_FEATURE_FLAG,
        "Disable a faulty player feature flag",
        "Turns off the flag guarding the regressed behaviour, without a full rollback.",
        (FeatureType.ACCESSIBLE_PLAYER, FeatureType.SIGN_LANGUAGE,
         FeatureType.ACCESSIBLE_AUTH),
        ("flag exists and is runtime-toggleable",),
        ("ctv-9.4.0", "web-4.12.0", "festival-branding-overlay", "bot-challenge"),
        Role.OPERATOR,
        {"accesspulse_player_reduced_motion_ok_ratio": "returns to 1.0"},
        "player_recovery",
        "auto_rollback_on_verification_failure",
    ),
    ActionType.ISSUE_STATUS_UPDATE: ActionSpec(
        ActionType.ISSUE_STATUS_UPDATE,
        "Publish an accessible status update",
        "Publishes plain-language, screen-reader-optimised status to the public page.",
        tuple(FeatureType),
        ("incident scope confirmed", "wording generated from the approved incident record"),
        ("public-status",),
        Role.SUPPORT_LEAD,
        {},
        "communication",
        "supersede_with_correction",
        audience_visible=True,
    ),
}


# ---------------------------------------------------------------------------
# Policy rules
# ---------------------------------------------------------------------------


@dataclass
class PolicyContext:
    tier: SLOTier
    live: bool
    scope: Scope
    operator_roles: tuple[Role, ...]
    change_freeze: bool = False
    concurrent_actions: tuple[str, ...] = ()
    now: datetime = field(default_factory=utcnow)
    provider_constraints: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Rule:
    rule_id: str
    description: str
    check: Callable[[ProposedAction, ActionSpec, PolicyContext], "RuleOutcome | None"]


@dataclass(frozen=True)
class RuleOutcome:
    classification: PolicyClass
    reason: str
    required_roles: tuple[Role, ...] = ()


def _r(rule_id: str, description: str):
    def deco(fn: Callable[[ProposedAction, ActionSpec, PolicyContext], "RuleOutcome | None"]):
        RULES.append(Rule(rule_id, description, fn))
        return fn
    return deco


RULES: list[Rule] = []


@_r("P-001", "Only catalogued actions may ever be proposed.")
def _catalogued(action, spec, ctx):
    if spec is None:
        return RuleOutcome(PolicyClass.PROHIBITED, "action type is not in the catalog")
    return None


@_r("P-002", "The action's target must be on the catalog allow-list.")
def _target_allowed(action, spec, ctx):
    if spec.allowed_targets and action.target not in spec.allowed_targets:
        return RuleOutcome(
            PolicyClass.PROHIBITED,
            f"target '{action.target}' is not allow-listed for {action.action_type.value}",
        )
    return None


@_r("P-003", "An action must be capable of affecting the feature that is broken.")
def _feature_relevant(action, spec, ctx):
    if not set(ctx.scope.features) & set(spec.features):
        return RuleOutcome(
            PolicyClass.PROHIBITED,
            "action cannot affect any feature in the incident scope",
        )
    return None


@_r("P-004", "A change freeze blocks everything except audience communication.")
def _freeze(action, spec, ctx):
    if ctx.change_freeze and not spec.audience_visible:
        return RuleOutcome(
            PolicyClass.PROHIBITED,
            "an active change freeze blocks operational changes",
        )
    return None


@_r("P-005", "No two conflicting actions may be in flight for one incident.")
def _no_conflict(action, spec, ctx):
    if action.action_type.value in ctx.concurrent_actions:
        return RuleOutcome(PolicyClass.PROHIBITED, "an identical action is already executing")
    return None


@_r("P-006", "An action may not be used on a blast radius wider than it is rated for.")
def _blast_limit(action, spec, ctx):
    order = ["local", "regional", "provider_wide", "systemic"]
    if order.index(ctx.scope.blast_class) > order.index(spec.max_blast_class):
        return RuleOutcome(
            PolicyClass.PROHIBITED,
            f"{action.action_type.value} is not rated for a {ctx.scope.blast_class} incident",
        )
    return None


@_r("P-010", "Anything touching a live tier-0 broadcast chain needs the technical director.")
def _tier0_live(action, spec, ctx):
    if ctx.live and ctx.tier is SLOTier.TIER_0_GLOBAL_LIVE and not spec.audience_visible:
        return RuleOutcome(
            PolicyClass.APPROVAL_REQUIRED,
            "tier-0 event is live and globally distributed",
            (Role.TECHNICAL_DIRECTOR, Role.STREAMING_SRE),
        )
    return None


@_r("P-011", "Audience-visible communication needs the viewer-support lead.")
def _audience_comms(action, spec, ctx):
    if spec.audience_visible:
        return RuleOutcome(
            PolicyClass.APPROVAL_REQUIRED,
            "public communication is audience-visible",
            (Role.SUPPORT_LEAD, Role.TECHNICAL_DIRECTOR),
        )
    return None


@_r("P-012", "Multi-territory actions need approval even outside tier 0.")
def _multi_territory(action, spec, ctx):
    if len(ctx.scope.territories) > 2:
        return RuleOutcome(
            PolicyClass.APPROVAL_REQUIRED,
            f"action affects {len(ctx.scope.territories)} territories",
            (Role.TECHNICAL_DIRECTOR,),
        )
    return None


@_r("P-013", "Sign-language pipeline restarts always involve the accessibility specialist.")
def _sign_specialist(action, spec, ctx):
    if action.action_type is ActionType.RESTART_SIGN_PIPELINE:
        return RuleOutcome(
            PolicyClass.APPROVAL_REQUIRED,
            "restarting the interpreter feed interrupts a live human interpreter",
            (Role.A11Y_SPECIALIST,),
        )
    return None


@_r("P-014", "Provider constraints can force approval.")
def _provider(action, spec, ctx):
    for provider in ctx.scope.providers:
        if ctx.provider_constraints.get(provider) == "approval_required":
            return RuleOutcome(
                PolicyClass.APPROVAL_REQUIRED,
                f"provider {provider} is under a contractual change constraint",
                (Role.TECHNICAL_DIRECTOR,),
            )
    return None


@_r("P-020", "Narrow, reversible, single-region recoveries may run automatically.")
def _auto_narrow(action, spec, ctx):
    if (
        not ctx.live
        and ctx.scope.blast_class == "local"
        and len(ctx.scope.territories) <= 1
        and spec.rollback_behaviour.startswith("auto_rollback")
    ):
        return RuleOutcome(PolicyClass.AUTOMATIC, "narrow, reversible, non-live recovery")
    return None


def evaluate(
    action: ProposedAction,
    ctx: PolicyContext,
    decision_id: str | None = None,
) -> PolicyDecision:
    """Run every rule. Prohibited wins, then approval-required, then automatic."""
    spec = ACTION_CATALOG.get(action.action_type)
    outcomes: list[tuple[str, RuleOutcome]] = []
    for rule in RULES:
        result = rule.check(action, spec, ctx)  # type: ignore[arg-type]
        if result is not None:
            outcomes.append((rule.rule_id, result))
        # Every rule after P-001 reasons about the catalog entry. If there is no
        # entry the proposal is already prohibited, and continuing would ask the
        # remaining rules to interpret an action nothing knows how to perform.
        if spec is None:
            break

    prohibited = [(rid, o) for rid, o in outcomes if o.classification is PolicyClass.PROHIBITED]
    approvals = [(rid, o) for rid, o in outcomes
                 if o.classification is PolicyClass.APPROVAL_REQUIRED]

    if prohibited:
        classification = PolicyClass.PROHIBITED
        roles: tuple[Role, ...] = ()
        rationale = tuple(f"{rid}: {o.reason}" for rid, o in prohibited)
        violated = tuple(rid for rid, _ in prohibited)
    elif approvals:
        classification = PolicyClass.APPROVAL_REQUIRED
        roles = tuple(dict.fromkeys(r for _, o in approvals for r in o.required_roles))
        rationale = tuple(f"{rid}: {o.reason}" for rid, o in approvals)
        violated = ()
    else:
        auto = [(rid, o) for rid, o in outcomes if o.classification is PolicyClass.AUTOMATIC]
        classification = PolicyClass.AUTOMATIC
        roles = ()
        rationale = tuple(f"{rid}: {o.reason}" for rid, o in auto) or \
            ("no rule required approval",)
        violated = ()

    return PolicyDecision(
        decision_id=decision_id or f"pol-{action.action_id}",
        incident_id=action.incident_id,
        action_id=action.action_id,
        classification=classification,
        required_roles=roles,
        rationale=rationale,
        violated_rules=violated,
        policy_version=POLICY_VERSION,
        freeze_window_active=ctx.change_freeze,
    )


def spec_for(action_type: ActionType) -> ActionSpec | None:
    return ACTION_CATALOG.get(action_type)


def actions_for_feature(feature: FeatureType) -> list[ActionSpec]:
    return [s for s in ACTION_CATALOG.values() if feature in s.features]
