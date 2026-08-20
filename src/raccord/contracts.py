"""Typed contracts for every record that crosses an agent, tool or storage boundary.

Nothing in Raccord passes free-form text between agents. Every alert, scope,
evidence item, hypothesis, policy decision, approval, action, verification
assertion and communication is a validated model. Malformed records are rejected
at the boundary rather than interpreted by a language model.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Mutable(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class FeatureType(str, Enum):
    CAPTIONS = "captions"
    SUBTITLES = "subtitles"
    AUDIO_DESCRIPTION = "audio_description"
    ALTERNATE_AUDIO = "alternate_audio"
    SIGN_LANGUAGE = "sign_language"
    ACCESSIBLE_PLAYER = "accessible_player"
    ACCESSIBLE_AUTH = "accessible_auth"
    ACCESSIBLE_PURCHASE = "accessible_purchase"


class Platform(str, Enum):
    WEB = "web"
    CTV = "ctv"
    MOBILE_IOS = "ios"
    MOBILE_ANDROID = "android"
    SET_TOP_BOX = "stb"


class DeviceClass(str, Enum):
    DESKTOP = "desktop"
    LAPTOP = "laptop"
    PHONE = "phone"
    TABLET = "tablet"
    SMART_TV = "smart_tv"
    STREAMING_STICK = "streaming_stick"
    CONSOLE = "console"


class SLOTier(str, Enum):
    """Event criticality tier. Drives error budget and approval requirements."""

    TIER_0_GLOBAL_LIVE = "tier0_global_live"
    TIER_1_REGIONAL_LIVE = "tier1_regional_live"
    TIER_2_VOD_PREMIUM = "tier2_vod_premium"
    TIER_3_CATALOG = "tier3_catalog"


class Severity(str, Enum):
    SEV1 = "sev1"
    SEV2 = "sev2"
    SEV3 = "sev3"
    SEV4 = "sev4"


class Role(str, Enum):
    OPERATOR = "broadcast_operator"
    A11Y_SPECIALIST = "accessibility_specialist"
    TECHNICAL_DIRECTOR = "event_technical_director"
    STREAMING_SRE = "streaming_sre"
    SUPPORT_LEAD = "viewer_support_lead"
    EXECUTIVE = "executive_producer"
    PLATFORM_ADMIN = "platform_admin"
    AUDITOR = "auditor"


class IncidentState(str, Enum):
    """Deterministic incident lifecycle. No transition may be skipped."""

    DETECTED = "DETECTED"
    QUALIFIED = "QUALIFIED"
    SCOPED = "SCOPED"
    EVIDENCE_COMPLETE = "EVIDENCE_COMPLETE"
    DIAGNOSED = "DIAGNOSED"
    POLICY_EVALUATED = "POLICY_EVALUATED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    ACTION_EXECUTING = "ACTION_EXECUTING"
    VERIFYING = "VERIFYING"
    RECOVERED = "RECOVERED"
    COMMUNICATED = "COMMUNICATED"
    REVIEWED = "REVIEWED"
    # Terminal off-ramps
    REJECTED = "REJECTED"
    ROLLED_BACK = "ROLLED_BACK"


INCIDENT_TRANSITIONS: dict[IncidentState, tuple[IncidentState, ...]] = {
    IncidentState.DETECTED: (IncidentState.QUALIFIED, IncidentState.REJECTED),
    IncidentState.QUALIFIED: (IncidentState.SCOPED,),
    IncidentState.SCOPED: (IncidentState.EVIDENCE_COMPLETE,),
    IncidentState.EVIDENCE_COMPLETE: (IncidentState.DIAGNOSED,),
    # An abstained diagnosis, or a failure class with no catalogued remedy,
    # leaves DIAGNOSED for REJECTED: the incident is escalated to a human with
    # all evidence intact rather than being forced towards an action.
    IncidentState.DIAGNOSED: (IncidentState.POLICY_EVALUATED, IncidentState.REJECTED),
    IncidentState.POLICY_EVALUATED: (
        IncidentState.AWAITING_APPROVAL,
        IncidentState.ACTION_EXECUTING,
        IncidentState.REJECTED,
    ),
    IncidentState.AWAITING_APPROVAL: (IncidentState.ACTION_EXECUTING, IncidentState.REJECTED),
    IncidentState.ACTION_EXECUTING: (IncidentState.VERIFYING,),
    IncidentState.VERIFYING: (IncidentState.RECOVERED, IncidentState.ROLLED_BACK),
    IncidentState.RECOVERED: (IncidentState.COMMUNICATED,),
    IncidentState.COMMUNICATED: (IncidentState.REVIEWED,),
    IncidentState.ROLLED_BACK: (IncidentState.SCOPED, IncidentState.REVIEWED),
    IncidentState.REVIEWED: (),
    IncidentState.REJECTED: (),
}


class PolicyClass(str, Enum):
    AUTOMATIC = "automatic"
    APPROVAL_REQUIRED = "approval_required"
    PROHIBITED = "prohibited"


class AssertionStatus(str, Enum):
    PENDING = "pending"
    PASSING = "passing"
    FAILING = "failing"
    INCONCLUSIVE = "inconclusive"


class EvidenceKind(str, Enum):
    GRAFANA_ALERT = "grafana_alert"
    PROM_QUERY = "prometheus_query"
    LOKI_QUERY = "loki_query"
    TEMPO_TRACE = "tempo_trace"
    PYROSCOPE_PROFILE = "pyroscope_profile"
    DASHBOARD_LINK = "dashboard_link"
    ANNOTATION = "annotation"
    PROBE_FINDING = "probe_finding"
    MODEL_FINDING = "model_finding"
    CHANGE_EVENT = "change_event"
    SESSION_AGGREGATE = "session_aggregate"
    MEDIA_CLIP = "media_clip"


class FailureClass(str, Enum):
    """Formal failure taxonomy. Diagnosis must map to exactly one class."""

    CAPTION_SOURCE_LOSS = "caption.source_loss"
    CAPTION_ENCODER_FAILURE = "caption.encoder_failure"
    CAPTION_CLOCK_OFFSET = "caption.clock_offset"
    CAPTION_PROGRESSIVE_DRIFT = "caption.progressive_drift"
    CAPTION_WORD_DROP = "caption.word_drop"
    CAPTION_DUPLICATE = "caption.duplicate"
    CAPTION_WRONG_LANGUAGE = "caption.wrong_language"
    CAPTION_SPEAKER_CORRUPTION = "caption.speaker_corruption"
    CAPTION_READING_SPEED = "caption.reading_speed"
    CAPTION_FLICKER = "caption.flicker"
    CAPTION_MANIFEST_OMISSION = "caption.manifest_omission"
    CAPTION_RENDER_FAILURE = "caption.device_render_failure"

    AD_TRACK_OMISSION = "audio_description.track_omission"
    AD_SILENT_SEGMENT = "audio_description.silent_segment"
    AD_WRONG_LANGUAGE = "audio_description.wrong_language"
    AD_TIMELINE_DRIFT = "audio_description.timeline_drift"
    AD_LOUDNESS_DEFECT = "audio_description.loudness_defect"
    AD_CHANNEL_LAYOUT = "audio_description.channel_layout"
    AD_SELECTION_FAILURE = "audio_description.device_selection_failure"

    SIGN_FROZEN_FRAMES = "sign_language.frozen_frames"
    SIGN_BLACK_FRAMES = "sign_language.black_frames"
    SIGN_CROP_FAILURE = "sign_language.crop_failure"
    SIGN_LOW_FRAMERATE = "sign_language.low_framerate"
    SIGN_SYNC_DRIFT = "sign_language.sync_drift"
    SIGN_PIP_OBSTRUCTION = "sign_language.pip_obstruction"
    SIGN_REGIONAL_DELIVERY = "sign_language.regional_delivery_failure"

    PLAYER_KEYBOARD_TRAP = "player.keyboard_trap"
    PLAYER_MISSING_NAME = "player.missing_accessible_name"
    PLAYER_FOCUS_LOSS = "player.focus_loss"
    PLAYER_INACCESSIBLE_ERROR = "player.inaccessible_error"
    PLAYER_CAPTION_CONTROL = "player.caption_control_regression"
    PLAYER_SCREEN_READER = "player.screen_reader_incompatibility"
    PLAYER_PURCHASE_FAILURE = "player.purchase_flow_failure"
    PLAYER_AUTH_FAILURE = "player.authentication_failure"
    PLAYER_REDUCED_MOTION = "player.reduced_motion_violation"

    INFRA_CDN_REGIONAL = "infra.cdn_regional_failure"
    INFRA_PACKET_LOSS = "infra.packet_loss"
    INFRA_ENCODER_CPU = "infra.encoder_cpu_saturation"
    INFRA_GPU_SATURATION = "infra.gpu_saturation"
    INFRA_CLOCK_SOURCE_CHANGE = "infra.clock_source_change"
    INFRA_DEPLOY_REGRESSION = "infra.deployment_regression"
    INFRA_MALFORMED_MANIFEST = "infra.malformed_manifest"
    INFRA_STALE_CONFIG = "infra.stale_configuration"
    INFRA_PROVIDER_DEGRADATION = "infra.provider_degradation"

    UNKNOWN = "unknown"


class ActionType(str, Enum):
    """The complete allow-list. Nothing outside this enum can be executed."""

    SWITCH_CAPTION_ENCODER_POOL = "switch_caption_encoder_pool"
    CHANGE_CLOCK_SOURCE = "change_clock_source"
    REROUTE_CAPTION_PATH = "reroute_caption_path"
    SELECT_SYNCHRONIZED_STANDBY = "select_synchronized_standby"
    RESTORE_KNOWN_GOOD_PLAYER = "restore_known_good_player_version"
    REPUBLISH_MANIFEST = "republish_corrected_manifest"
    REROUTE_REGION = "reroute_region"
    RESTORE_AUDIO_TRACK = "restore_omitted_audio_track"
    SWITCH_ALTERNATE_LANGUAGE_SOURCE = "switch_to_approved_alternate_language_source"
    RESTART_SIGN_PIPELINE = "restart_sign_language_pipeline_component"
    DISABLE_PLAYER_FEATURE_FLAG = "disable_faulty_player_feature_flag"
    ISSUE_STATUS_UPDATE = "issue_accessible_status_update"


# ---------------------------------------------------------------------------
# Promise registry
# ---------------------------------------------------------------------------


class AccessibilityPromise(Frozen):
    """A versioned operational contract. Cannot be silently changed mid-event."""

    promise_id: str
    version: int = 1
    event_id: str
    feature: FeatureType
    language: str
    territories: tuple[str, ...]
    platforms: tuple[Platform, ...]
    device_classes: tuple[DeviceClass, ...]
    player_versions: tuple[str, ...]
    delivery_path: str
    provider: str
    planned_start: datetime
    planned_end: datetime

    # Acceptance thresholds
    max_latency_ms: int = 2000
    max_sync_drift_ms: int = 1500
    min_availability: float = 0.995
    required_behaviour: str = ""

    approved_fallback: str | None = None
    slo_tier: SLOTier = SLOTier.TIER_2_VOD_PREMIUM
    business_owner: str = "unassigned"
    technical_owner: str = "unassigned"
    escalation_owner: str = "unassigned"
    public_communication_policy: Literal["always", "regional_only", "on_request"] = "always"
    remediation_policy: str = "standard"
    evidence_retention_days: int = 90

    effective_from: datetime = Field(default_factory=utcnow)
    effective_to: datetime | None = None

    @field_validator("language")
    @classmethod
    def _lang(cls, v: str) -> str:
        return v.lower()

    def content_hash(self) -> str:
        return stable_hash(self.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


class Alert(Frozen):
    alert_id: str
    rule_uid: str
    rule_title: str
    state: Literal["firing", "resolved", "pending"]
    severity: Severity
    fired_at: datetime
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    value: float | None = None
    source: Literal["grafana"] = "grafana"


class Scope(Frozen):
    """What is broken, where, for whom."""

    incident_id: str
    features: tuple[FeatureType, ...]
    languages: tuple[str, ...]
    territories: tuple[str, ...]
    platforms: tuple[Platform, ...]
    device_classes: tuple[DeviceClass, ...]
    player_versions: tuple[str, ...]
    cdn_regions: tuple[str, ...]
    providers: tuple[str, ...]
    components: tuple[str, ...]
    violated_promise_ids: tuple[str, ...]
    affected_sessions: int
    protected_sessions: int
    blast_class: Literal["local", "regional", "provider_wide", "systemic"]
    owners: dict[str, str] = Field(default_factory=dict)
    computed_at: datetime = Field(default_factory=utcnow)


class Evidence(Frozen):
    evidence_id: str
    incident_id: str
    kind: EvidenceKind
    source_tool: str  # e.g. "grafana.mcp:query_prometheus"
    query: str | None = None
    summary: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    interval_start: datetime | None = None
    interval_end: datetime | None = None
    deep_link: str | None = None
    collected_at: datetime = Field(default_factory=utcnow)

    def content_hash(self) -> str:
        return stable_hash(self.model_dump(mode="json"))


class ModelFinding(Frozen):
    """Every model output carries score, uncertainty, provenance and limits."""

    finding_id: str
    probe: str
    model_version: str
    metric: str
    score: float
    unit: str = ""
    confidence: float = 0.0
    confidence_interval: tuple[float, float] | None = None
    evidence_interval: tuple[float, float] | None = None  # seconds into programme
    abstained: bool = False
    data_quality: Literal["ok", "degraded", "insufficient"] = "ok"
    known_limitations: tuple[str, ...] = ()
    detail: dict[str, Any] = Field(default_factory=dict)


class ChangeEvent(Frozen):
    change_id: str
    kind: Literal["deployment", "config", "provider", "traffic", "manifest", "routing"]
    component: str
    description: str
    at: datetime
    actor: str = "unknown"
    payload: dict[str, Any] = Field(default_factory=dict)


class Hypothesis(Frozen):
    hypothesis_id: str
    failure_class: FailureClass
    statement: str
    rank: int
    posterior: float  # 0..1
    supporting_evidence_ids: tuple[str, ...]
    contradicting_evidence_ids: tuple[str, ...] = ()
    causal_change_id: str | None = None
    uncertainty_note: str = ""
    abstained: bool = False


# ---------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------


class ProposedAction(Frozen):
    action_id: str
    incident_id: str
    action_type: ActionType
    target: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    scope_digest: str = ""
    expected_effect: str = ""
    expected_metric_change: dict[str, str] = Field(default_factory=dict)
    verification_suite: str = "default"
    rollback_behaviour: str = "auto_rollback_on_verification_failure"
    idempotency_key: str = ""

    def action_hash(self) -> str:
        return stable_hash(
            {
                "action_type": self.action_type.value,
                "target": self.target,
                "parameters": self.parameters,
                "incident_id": self.incident_id,
            }
        )


class PolicyDecision(Frozen):
    decision_id: str
    incident_id: str
    action_id: str
    classification: PolicyClass
    required_roles: tuple[Role, ...] = ()
    rationale: tuple[str, ...] = ()
    violated_rules: tuple[str, ...] = ()
    policy_version: str = "unset"
    freeze_window_active: bool = False
    decided_at: datetime = Field(default_factory=utcnow)


class Approval(Frozen):
    approval_id: str
    incident_id: str
    action_id: str
    approver: str
    approver_role: Role
    token: str  # signed, single-use, expiring
    action_hash: str
    evidence_hash: str
    issued_at: datetime
    expires_at: datetime


class ActionResult(Frozen):
    action_id: str
    incident_id: str
    executed: bool
    executed_at: datetime = Field(default_factory=utcnow)
    executor: str = "raccord.remediation"
    outcome: str = ""
    before_state: dict[str, Any] = Field(default_factory=dict)
    after_state: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class VerificationAssertion(Frozen):
    assertion_id: str
    incident_id: str
    name: str
    description: str
    mandatory: bool
    status: AssertionStatus
    observed: float | None = None
    threshold: float | None = None
    comparator: Literal["lt", "lte", "gt", "gte", "eq"] = "lte"
    scope_note: str = ""
    checked_at: datetime = Field(default_factory=utcnow)


class Communication(Frozen):
    communication_id: str
    incident_id: str
    audience: Literal[
        "operator",
        "accessibility_specialist",
        "technical_director",
        "viewer_support",
        "executive",
        "public_status",
        "post_incident",
    ]
    subject: str
    body: str
    reading_level_note: str = ""
    contains_internal_detail: bool = False
    generated_at: datetime = Field(default_factory=utcnow)


class ReasoningSynthesis(Frozen):
    """Non-authoritative Gemini interpretation of the deterministic record."""

    narrative: str
    supporting: tuple[str, ...] = ()
    contradicting: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    recommended_next_evidence: str = ""
    confidence_statement: str = ""
    model: str
    runtime: str = "vertex-direct"
    tokens_in: int = 0
    tokens_out: int = 0


class AuditEvent(Frozen):
    seq: int
    incident_id: str
    at: datetime
    actor: str
    event: str
    from_state: IncidentState | None = None
    to_state: IncidentState | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str = ""
    hash: str = ""


class SessionAggregate(Frozen):
    """Privacy-preserving. Never an individual, never an inferred trait."""

    slice_key: str
    feature: FeatureType
    language: str
    territory: str
    platform: Platform
    player_version: str
    sessions_with_feature_enabled: int
    selection_failures: int = 0
    playback_errors_after_selection: int = 0
    repeated_attempts: int = 0
    support_contacts: int = 0
    k_anonymity_threshold: int = 50
    suppressed: bool = False


class Incident(Mutable):
    incident_id: str
    event_id: str
    title: str
    state: IncidentState = IncidentState.DETECTED
    severity: Severity = Severity.SEV3
    opened_at: datetime = Field(default_factory=utcnow)
    closed_at: datetime | None = None

    alert: Alert | None = None
    scope: Scope | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    findings: list[ModelFinding] = Field(default_factory=list)
    changes: list[ChangeEvent] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    reasoning: ReasoningSynthesis | None = None
    proposed_action: ProposedAction | None = None
    policy_decision: PolicyDecision | None = None
    approval: Approval | None = None
    action_result: ActionResult | None = None
    assertions: list[VerificationAssertion] = Field(default_factory=list)
    communications: list[Communication] = Field(default_factory=list)
    audit: list[AuditEvent] = Field(default_factory=list)

    # Measured lifecycle timings (seconds from fault injection)
    timings: dict[str, float] = Field(default_factory=dict)
    error_budget_consumed: float = 0.0

    def evidence_hash(self) -> str:
        return stable_hash([e.content_hash() for e in self.evidence])


class PostIncidentReview(Frozen):
    incident_id: str
    root_cause: FailureClass
    contributing_factors: tuple[str, ...]
    # `time_to_detect_s` and `outage_seconds` are on the programme clock: they
    # describe what the audience experienced. Everything from `time_to_scope_s`
    # to `time_to_recovery_s` is a wall-clock stopwatch on the agent's own work.
    # The two are not comparable and must not be presented as if they were.
    time_to_detect_s: float
    outage_seconds: float = 0.0
    time_to_scope_s: float
    time_to_evidence_s: float
    time_to_approval_s: float
    time_to_recovery_s: float
    error_budget_consumed: float
    affected_sessions: int
    protected_sessions: int
    missed_signals: tuple[str, ...]
    unnecessary_tool_calls: int
    diagnosis_correct: bool
    verification_complete: bool
    proposed_improvements: tuple[str, ...]
    learning_narrative: str = ""
    proposed_experiments: tuple[str, ...] = ()
    reasoning_model: str | None = None
    reasoning_runtime: str | None = None
    generated_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Preflight certification
# ---------------------------------------------------------------------------


class CertificationAssertion(Frozen):
    assertion_id: str
    gate: str
    name: str
    hard: bool  # hard assertions block certification
    status: AssertionStatus
    detail: str = ""
    evidence_ref: str | None = None
    probe_version: str = "1.0.0"
    owner: str = "unassigned"


class CertificationRecord(Frozen):
    certification_id: str
    event_id: str
    certified: bool
    assertions: tuple[CertificationAssertion, ...]
    blockers: tuple[str, ...]
    promise_hashes: tuple[str, ...]
    model_versions: dict[str, str]
    signature: str
    generated_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def stable_hash(obj: Any) -> str:
    """Deterministic content hash used for evidence and action integrity."""

    def default(o: Any) -> Any:
        if isinstance(o, datetime):
            return _iso(o)
        if isinstance(o, Enum):
            return o.value
        if isinstance(o, (set, frozenset)):
            return sorted(o)
        return str(o)

    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=default)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def can_transition(current: IncidentState, target: IncidentState) -> bool:
    return target in INCIDENT_TRANSITIONS.get(current, ())
