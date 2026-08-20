"""Fault library for the digital-twin demonstration environment.

Each fault is a declarative specification: which component fails, which slices of
the audience it reaches, how the symptom develops over time, which SLOs it should
breach, and which change event (if any) truly caused it. The benchmark harness
samples from this library; the hero incident uses one entry from it.

Ground truth lives here and *only* here. The probes, the diagnosis agent and the
verification agent never read this module - that separation is what makes the
benchmark meaningful.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .contracts import FailureClass, FeatureType, Platform

Onset = Literal["step", "ramp", "intermittent", "flap"]


@dataclass(frozen=True)
class FaultSpec:
    fault_id: str
    failure_class: FailureClass
    feature: FeatureType
    name: str
    description: str
    component: str
    onset: Onset = "step"
    ramp_seconds: float = 0.0
    params: dict[str, Any] = field(default_factory=dict)
    default_scope: dict[str, Any] = field(default_factory=dict)
    expected_slos: tuple[str, ...] = ()
    causal_change: dict[str, Any] | None = None
    remediation: tuple[str, ...] = ()  # ActionType values that genuinely fix it
    difficulty: float = 0.5  # 0 easy .. 1 hard, used to stratify the benchmark


def _scope(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "languages": None,  # None = all
        "territories": None,
        "platforms": None,
        "player_versions": None,
        "cdn_regions": None,
    }
    base.update(kw)
    return base


WEST_EU = ["FR", "DE", "ES", "GB"]
CTV = [Platform.CTV.value]

FAULT_LIBRARY: dict[str, FaultSpec] = {}


def _add(spec: FaultSpec) -> FaultSpec:
    FAULT_LIBRARY[spec.fault_id] = spec
    return spec


# ---------------------------------------------------------------------------
# Captions
# ---------------------------------------------------------------------------

_add(
    FaultSpec(
        "cap.source_loss",
        FailureClass.CAPTION_SOURCE_LOSS,
        FeatureType.CAPTIONS,
        "Caption source loss",
        "The upstream caption source stops emitting cues for one language.",
        "capsrc-{language}",
        onset="step",
        params={"cue_rate_multiplier": 0.0},
        default_scope=_scope(languages=["en"]),
        expected_slos=("cap.availability", "cap.omission_rate"),
        remediation=("reroute_caption_path", "switch_to_approved_alternate_language_source"),
        difficulty=0.2,
    )
)

_add(
    FaultSpec(
        "cap.encoder_failure",
        FailureClass.CAPTION_ENCODER_FAILURE,
        FeatureType.CAPTIONS,
        "Caption encoder pool failure",
        "Half the encoder pool crash-loops; cues are emitted intermittently.",
        "capenc-pool-a",
        onset="intermittent",
        params={"cue_rate_multiplier": 0.45, "duty_cycle": 0.5, "period_s": 8.0},
        default_scope=_scope(),
        expected_slos=("cap.availability", "cap.omission_rate", "cap.drift"),
        causal_change={
            "kind": "deployment",
            "component": "capenc-pool-a",
            "description": "caption-encoder 3.11.2 rollout",
        },
        remediation=("switch_caption_encoder_pool", "select_synchronized_standby"),
        difficulty=0.45,
    )
)

_add(
    FaultSpec(
        "cap.clock_offset",
        FailureClass.CAPTION_CLOCK_OFFSET,
        FeatureType.CAPTIONS,
        "Fixed caption clock offset",
        "A constant timing offset is introduced between programme audio and cues.",
        "capenc-pool-a",
        onset="step",
        params={"drift_seconds": 3.0},
        default_scope=_scope(),
        expected_slos=("cap.drift",),
        causal_change={
            "kind": "config",
            "component": "clock-ptp-primary",
            "description": "clock source changed to ntp-fallback",
        },
        remediation=("change_clock_source", "select_synchronized_standby"),
        difficulty=0.4,
    )
)

_add(
    FaultSpec(
        "cap.progressive_drift",
        FailureClass.CAPTION_PROGRESSIVE_DRIFT,
        FeatureType.CAPTIONS,
        "Progressive caption drift",
        "Caption timing degrades steadily after a clock-source resynchronisation, "
        "reaching eight seconds behind the dialogue.",
        "capenc-pool-a",
        onset="ramp",
        ramp_seconds=180.0,
        params={"drift_seconds": 8.0},
        default_scope=_scope(
            languages=["en"],
            territories=WEST_EU,
            platforms=CTV,
            player_versions=["ctv-9.3.1", "ctv-9.4.0"],
        ),
        expected_slos=("cap.drift", "cap.semantic"),
        causal_change={
            "kind": "config",
            "component": "clock-ptp-primary",
            "description": "PTP grandmaster failover to NTP fallback pool",
        },
        remediation=(
            "select_synchronized_standby",
            "change_clock_source",
            "switch_caption_encoder_pool",
        ),
        difficulty=0.75,
    )
)

_add(
    FaultSpec(
        "cap.word_drop",
        FailureClass.CAPTION_WORD_DROP,
        FeatureType.CAPTIONS,
        "Caption word drops",
        "Random tokens are dropped from cues, degrading semantic preservation.",
        "capenc-pool-a",
        onset="step",
        params={"drop_probability": 0.18},
        default_scope=_scope(languages=["fr"]),
        expected_slos=("cap.omission_rate", "cap.semantic"),
        remediation=("switch_caption_encoder_pool",),
        difficulty=0.6,
    )
)

_add(
    FaultSpec(
        "cap.duplicate",
        FailureClass.CAPTION_DUPLICATE,
        FeatureType.CAPTIONS,
        "Duplicate caption cues",
        "The packager repeats the previous cue, producing stuttering captions.",
        "packager-main",
        onset="step",
        params={"duplicate_probability": 0.35},
        default_scope=_scope(),
        expected_slos=("cap.duplicate", "cap.reading_speed"),
        remediation=("republish_corrected_manifest", "switch_caption_encoder_pool"),
        difficulty=0.35,
    )
)

_add(
    FaultSpec(
        "cap.wrong_language",
        FailureClass.CAPTION_WRONG_LANGUAGE,
        FeatureType.CAPTIONS,
        "Wrong caption language routed",
        "Spanish cues are delivered on the German caption track after a routing change.",
        "packager-main",
        onset="step",
        params={"substitute_language": "es"},
        default_scope=_scope(languages=["de"]),
        expected_slos=("cap.wrong_language", "cap.semantic"),
        causal_change={
            "kind": "routing",
            "component": "packager-main",
            "description": "caption track map updated",
        },
        remediation=("reroute_caption_path", "republish_corrected_manifest"),
        difficulty=0.3,
    )
)

_add(
    FaultSpec(
        "cap.speaker_corruption",
        FailureClass.CAPTION_SPEAKER_CORRUPTION,
        FeatureType.CAPTIONS,
        "Speaker label corruption",
        "Speaker attribution labels are swapped between characters.",
        "capenc-pool-a",
        onset="step",
        params={"swap_probability": 0.5},
        default_scope=_scope(languages=["en"]),
        expected_slos=("cap.speaker_accuracy",),
        remediation=("switch_caption_encoder_pool",),
        difficulty=0.55,
    )
)

_add(
    FaultSpec(
        "cap.reading_speed",
        FailureClass.CAPTION_READING_SPEED,
        FeatureType.CAPTIONS,
        "Excessive reading speed",
        "Cue durations are compressed, pushing characters-per-second past the limit.",
        "capenc-pool-a",
        onset="step",
        params={"duration_multiplier": 0.45},
        default_scope=_scope(),
        expected_slos=("cap.reading_speed", "cap.flicker"),
        remediation=("switch_caption_encoder_pool",),
        difficulty=0.4,
    )
)

_add(
    FaultSpec(
        "cap.flicker",
        FailureClass.CAPTION_FLICKER,
        FeatureType.CAPTIONS,
        "Caption flicker",
        "Cues are torn down and re-rendered within a second.",
        "pv-ctv-9.4.0",
        onset="intermittent",
        params={"flicker_probability": 0.3, "duty_cycle": 0.6, "period_s": 5.0},
        default_scope=_scope(platforms=CTV, player_versions=["ctv-9.4.0"]),
        expected_slos=("cap.flicker",),
        causal_change={
            "kind": "deployment",
            "component": "pv-ctv-9.4.0",
            "description": "player 9.4.0 canary",
        },
        remediation=("restore_known_good_player_version", "disable_faulty_player_feature_flag"),
        difficulty=0.5,
    )
)

_add(
    FaultSpec(
        "cap.manifest_omission",
        FailureClass.CAPTION_MANIFEST_OMISSION,
        FeatureType.CAPTIONS,
        "Caption track missing from manifest",
        "The manifest no longer declares the promised caption track, so players cannot select it.",
        "manifest-main",
        onset="step",
        params={"drop_track": True},
        default_scope=_scope(languages=["es"]),
        expected_slos=("cap.availability", "cap.render_success"),
        causal_change={
            "kind": "manifest",
            "component": "manifest-main",
            "description": "manifest regenerated after packager restart",
        },
        remediation=("republish_corrected_manifest",),
        difficulty=0.3,
    )
)

_add(
    FaultSpec(
        "cap.render_failure",
        FailureClass.CAPTION_RENDER_FAILURE,
        FeatureType.CAPTIONS,
        "Device-specific caption render failure",
        "Cues arrive but the player draws them off-screen on one connected-TV build.",
        "pv-ctv-9.4.0",
        onset="step",
        params={"render_success": 0.1},
        default_scope=_scope(platforms=CTV, player_versions=["ctv-9.4.0"]),
        expected_slos=("cap.render_success",),
        causal_change={
            "kind": "deployment",
            "component": "pv-ctv-9.4.0",
            "description": "player 9.4.0 canary",
        },
        remediation=("restore_known_good_player_version",),
        difficulty=0.65,
    )
)

# ---------------------------------------------------------------------------
# Audio description and alternate audio
# ---------------------------------------------------------------------------

_add(
    FaultSpec(
        "ad.track_omission",
        FailureClass.AD_TRACK_OMISSION,
        FeatureType.AUDIO_DESCRIPTION,
        "Described audio track omitted",
        "The described track disappears from the manifest after repackaging.",
        "manifest-main",
        onset="step",
        params={"drop_track": True},
        default_scope=_scope(languages=["en"]),
        expected_slos=("ad.track_present", "ad.selection"),
        causal_change={
            "kind": "manifest",
            "component": "manifest-main",
            "description": "audio adaptation set rebuilt",
        },
        remediation=("restore_omitted_audio_track", "republish_corrected_manifest"),
        difficulty=0.25,
    )
)

_add(
    FaultSpec(
        "ad.silent_segments",
        FailureClass.AD_SILENT_SEGMENT,
        FeatureType.AUDIO_DESCRIPTION,
        "Silent description segments",
        "The described track is declared and delivered but carries no audible description.",
        "adsrc-en",
        onset="step",
        params={"silence_probability": 0.7},
        default_scope=_scope(languages=["en"]),
        expected_slos=("ad.audio_present",),
        remediation=("restart_sign_language_pipeline_component", "restore_omitted_audio_track"),
        difficulty=0.7,
    )
)

_add(
    FaultSpec(
        "ad.wrong_language",
        FailureClass.AD_WRONG_LANGUAGE,
        FeatureType.AUDIO_DESCRIPTION,
        "Description language mismatch",
        "French description is routed onto the English described track.",
        "packager-main",
        onset="step",
        params={"substitute_language": "fr"},
        default_scope=_scope(languages=["en"]),
        expected_slos=("ad.language",),
        causal_change={
            "kind": "routing",
            "component": "packager-main",
            "description": "audio track map updated",
        },
        remediation=(
            "switch_to_approved_alternate_language_source",
            "republish_corrected_manifest",
        ),
        difficulty=0.4,
    )
)

_add(
    FaultSpec(
        "ad.timeline_drift",
        FailureClass.AD_TIMELINE_DRIFT,
        FeatureType.AUDIO_DESCRIPTION,
        "Description timeline drift",
        "Descriptions land over dialogue instead of in the gaps.",
        "adsrc-en",
        onset="ramp",
        ramp_seconds=120.0,
        params={"drift_seconds": 2.5},
        default_scope=_scope(languages=["en"]),
        expected_slos=("ad.drift",),
        causal_change={
            "kind": "config",
            "component": "clock-ptp-primary",
            "description": "clock source changed",
        },
        remediation=("change_clock_source", "select_synchronized_standby"),
        difficulty=0.7,
    )
)

_add(
    FaultSpec(
        "ad.loudness",
        FailureClass.AD_LOUDNESS_DEFECT,
        FeatureType.AUDIO_DESCRIPTION,
        "Description loudness defect",
        "Description is mixed far below the programme, masked by dialogue.",
        "adsrc-fr",
        onset="step",
        params={"loudness_lufs": -34.0},
        default_scope=_scope(languages=["fr"]),
        expected_slos=("ad.loudness",),
        remediation=("restore_omitted_audio_track",),
        difficulty=0.5,
    )
)

_add(
    FaultSpec(
        "ad.channel_layout",
        FailureClass.AD_CHANNEL_LAYOUT,
        FeatureType.AUDIO_DESCRIPTION,
        "Invalid channel layout",
        "The described track is published as 5.1 where players expect stereo, so the "
        "track appears in the menu but cannot be selected on connected-TV builds.",
        "packager-main",
        onset="step",
        params={"channels": 6, "expected_channels": 2, "selection_success": 0.1},
        default_scope=_scope(languages=["en"], platforms=CTV),
        expected_slos=("ad.selection", "ad.audio_present"),
        remediation=("republish_corrected_manifest",),
        difficulty=0.45,
    )
)

_add(
    FaultSpec(
        "ad.selection_failure",
        FailureClass.AD_SELECTION_FAILURE,
        FeatureType.AUDIO_DESCRIPTION,
        "Track selection fails on device",
        "The audio-track menu lists the described track but selecting it silently fails.",
        "pv-ctv-9.4.0",
        onset="step",
        params={"selection_success": 0.05},
        default_scope=_scope(platforms=CTV, player_versions=["ctv-9.4.0"]),
        expected_slos=("ad.selection",),
        causal_change={
            "kind": "deployment",
            "component": "pv-ctv-9.4.0",
            "description": "player 9.4.0 canary",
        },
        remediation=("restore_known_good_player_version",),
        difficulty=0.6,
    )
)

_add(
    FaultSpec(
        "alt.track_omission",
        FailureClass.AD_TRACK_OMISSION,
        FeatureType.ALTERNATE_AUDIO,
        "Alternate-language audio omitted",
        "The French audio track is missing for francophone territories.",
        "manifest-main",
        onset="step",
        params={"drop_track": True},
        default_scope=_scope(languages=["fr"], territories=["FR", "CA"]),
        expected_slos=("ad.track_present",),
        remediation=("restore_omitted_audio_track", "republish_corrected_manifest"),
        difficulty=0.3,
    )
)

# ---------------------------------------------------------------------------
# Sign-language feed
# ---------------------------------------------------------------------------

_add(
    FaultSpec(
        "sign.frozen",
        FailureClass.SIGN_FROZEN_FRAMES,
        FeatureType.SIGN_LANGUAGE,
        "Frozen interpreter frames",
        "The interpreter feed freezes on a repeated frame.",
        "signsrc-lsf",
        onset="step",
        params={"frozen_ratio": 0.42},
        default_scope=_scope(territories=["FR", "CA"]),
        expected_slos=("sign.frozen", "sign.availability"),
        remediation=("restart_sign_language_pipeline_component",),
        difficulty=0.3,
    )
)

_add(
    FaultSpec(
        "sign.black",
        FailureClass.SIGN_BLACK_FRAMES,
        FeatureType.SIGN_LANGUAGE,
        "Black frames on interpreter feed",
        "The interpreter feed drops to black between segments.",
        "signsrc-lsf",
        onset="intermittent",
        params={"black_ratio": 0.15, "duty_cycle": 0.4, "period_s": 6.0},
        default_scope=_scope(territories=["FR", "CA"]),
        expected_slos=("sign.black", "sign.availability"),
        remediation=("restart_sign_language_pipeline_component",),
        difficulty=0.35,
    )
)

_add(
    FaultSpec(
        "sign.crop",
        FailureClass.SIGN_CROP_FAILURE,
        FeatureType.SIGN_LANGUAGE,
        "Interpreter cropped out of frame",
        "An aspect-ratio change crops the interpreter's signing space.",
        "signsrc-lsf",
        onset="step",
        params={"visible_ratio": 0.55},
        default_scope=_scope(territories=["FR", "CA"]),
        expected_slos=("sign.visibility",),
        causal_change={
            "kind": "config",
            "component": "signsrc-lsf",
            "description": "output aspect ratio changed to 16:9 safe",
        },
        remediation=("restart_sign_language_pipeline_component",),
        difficulty=0.55,
    )
)

_add(
    FaultSpec(
        "sign.low_framerate",
        FailureClass.SIGN_LOW_FRAMERATE,
        FeatureType.SIGN_LANGUAGE,
        "Interpreter feed frame-rate collapse",
        "Encoder saturation drops the interpreter feed to 12 fps, making signs unreadable.",
        "signsrc-lsf",
        onset="ramp",
        ramp_seconds=60.0,
        params={"fps": 12.0},
        default_scope=_scope(territories=["FR", "CA"]),
        expected_slos=("sign.framerate",),
        causal_change={
            "kind": "deployment",
            "component": "signsrc-lsf",
            "description": "encoder preset changed",
        },
        remediation=("restart_sign_language_pipeline_component",),
        difficulty=0.5,
    )
)

_add(
    FaultSpec(
        "sign.sync_drift",
        FailureClass.SIGN_SYNC_DRIFT,
        FeatureType.SIGN_LANGUAGE,
        "Interpreter feed out of sync",
        "The interpreter feed runs ahead of programme audio.",
        "signsrc-lsf",
        onset="ramp",
        ramp_seconds=90.0,
        params={"drift_seconds": 2.2},
        default_scope=_scope(territories=["FR", "CA"]),
        expected_slos=("sign.sync",),
        causal_change={
            "kind": "config",
            "component": "clock-ptp-primary",
            "description": "clock source changed",
        },
        remediation=("change_clock_source", "restart_sign_language_pipeline_component"),
        difficulty=0.65,
    )
)

_add(
    FaultSpec(
        "sign.pip_obstruction",
        FailureClass.SIGN_PIP_OBSTRUCTION,
        FeatureType.SIGN_LANGUAGE,
        "Picture-in-picture obstruction",
        "The interpreter panel is overlapped by burned-in festival branding.",
        "pv-web-4.12.0",
        onset="step",
        params={"visible_ratio": 0.7},
        default_scope=_scope(territories=["FR"], platforms=[Platform.WEB.value]),
        expected_slos=("sign.visibility",),
        remediation=("disable_faulty_player_feature_flag",),
        difficulty=0.6,
    )
)

_add(
    FaultSpec(
        "sign.regional_delivery",
        FailureClass.SIGN_REGIONAL_DELIVERY,
        FeatureType.SIGN_LANGUAGE,
        "Interpreter feed undelivered in one region",
        "The interpreter rendition is not cached in one CDN region.",
        "region-eu-west",
        onset="step",
        params={"availability": 0.05},
        default_scope=_scope(territories=["FR"], cdn_regions=["eu-west"]),
        expected_slos=("sign.availability",),
        remediation=("reroute_region",),
        difficulty=0.5,
    )
)

# ---------------------------------------------------------------------------
# Player and access flows
# ---------------------------------------------------------------------------

_add(
    FaultSpec(
        "player.keyboard_trap",
        FailureClass.PLAYER_KEYBOARD_TRAP,
        FeatureType.ACCESSIBLE_PLAYER,
        "Keyboard trap in caption menu",
        "Focus cannot leave the caption settings dialog with the keyboard.",
        "pv-web-4.12.0",
        onset="step",
        params={"keyboard_completion": 0.0},
        default_scope=_scope(platforms=[Platform.WEB.value], player_versions=["web-4.12.0"]),
        expected_slos=("player.keyboard", "player.caption_control"),
        causal_change={
            "kind": "deployment",
            "component": "pv-web-4.12.0",
            "description": "player 4.12.0 rollout",
        },
        remediation=("restore_known_good_player_version", "disable_faulty_player_feature_flag"),
        difficulty=0.4,
    )
)

_add(
    FaultSpec(
        "player.missing_name",
        FailureClass.PLAYER_MISSING_NAME,
        FeatureType.ACCESSIBLE_PLAYER,
        "Controls without accessible names",
        "Icon-only playback controls lose their accessible names.",
        "pv-web-4.12.0",
        onset="step",
        params={"accessible_name_ratio": 0.55},
        default_scope=_scope(platforms=[Platform.WEB.value]),
        expected_slos=("player.accessible_name", "player.screen_reader"),
        remediation=("restore_known_good_player_version",),
        difficulty=0.35,
    )
)

_add(
    FaultSpec(
        "player.focus_loss",
        FailureClass.PLAYER_FOCUS_LOSS,
        FeatureType.ACCESSIBLE_PLAYER,
        "Focus indicator removed",
        "A CSS regression removes the visible focus ring.",
        "pv-web-4.12.0",
        onset="step",
        params={"focus_visible_ratio": 0.2},
        default_scope=_scope(platforms=[Platform.WEB.value]),
        expected_slos=("player.focus_visible",),
        remediation=("restore_known_good_player_version",),
        difficulty=0.3,
    )
)

_add(
    FaultSpec(
        "player.inaccessible_error",
        FailureClass.PLAYER_INACCESSIBLE_ERROR,
        FeatureType.ACCESSIBLE_PLAYER,
        "Errors not announced",
        "Playback errors render visually but are never announced to assistive technology.",
        "pv-ctv-9.4.0",
        onset="step",
        params={"screenreader_completion": 0.4},
        default_scope=_scope(platforms=CTV),
        expected_slos=("player.screen_reader",),
        remediation=("restore_known_good_player_version",),
        difficulty=0.6,
    )
)

_add(
    FaultSpec(
        "player.caption_control",
        FailureClass.PLAYER_CAPTION_CONTROL,
        FeatureType.ACCESSIBLE_PLAYER,
        "Caption control regression",
        "The caption toggle no longer persists the viewer's choice between segments.",
        "pv-ctv-9.4.0",
        onset="step",
        params={"caption_control_ok": 0.25},
        default_scope=_scope(platforms=CTV, player_versions=["ctv-9.4.0"]),
        expected_slos=("player.caption_control",),
        causal_change={
            "kind": "deployment",
            "component": "pv-ctv-9.4.0",
            "description": "player 9.4.0 canary",
        },
        remediation=("restore_known_good_player_version", "disable_faulty_player_feature_flag"),
        difficulty=0.45,
    )
)

_add(
    FaultSpec(
        "player.screen_reader",
        FailureClass.PLAYER_SCREEN_READER,
        FeatureType.ACCESSIBLE_PLAYER,
        "Screen-reader incompatibility",
        "A live region floods assistive technology and blocks the playback journey.",
        "pv-web-4.12.0",
        onset="step",
        params={"screenreader_completion": 0.15},
        default_scope=_scope(platforms=[Platform.WEB.value]),
        expected_slos=("player.screen_reader",),
        remediation=("restore_known_good_player_version", "disable_faulty_player_feature_flag"),
        difficulty=0.6,
    )
)

_add(
    FaultSpec(
        "player.reduced_motion",
        FailureClass.PLAYER_REDUCED_MOTION,
        FeatureType.ACCESSIBLE_PLAYER,
        "Reduced-motion preference ignored",
        "A parallax festival background animates despite prefers-reduced-motion.",
        "pv-web-4.12.0",
        onset="step",
        params={"reduced_motion_ok": 0.0},
        default_scope=_scope(platforms=[Platform.WEB.value]),
        expected_slos=("player.reduced_motion",),
        remediation=("disable_faulty_player_feature_flag",),
        difficulty=0.25,
    )
)

_add(
    FaultSpec(
        "auth.failure",
        FailureClass.PLAYER_AUTH_FAILURE,
        FeatureType.ACCESSIBLE_AUTH,
        "Accessible sign-in broken",
        "A new CAPTCHA step has no accessible alternative, blocking screen-reader sign-in.",
        "auth-svc",
        onset="step",
        params={"auth_completion": 0.1},
        default_scope=_scope(),
        expected_slos=("auth.completion",),
        causal_change={
            "kind": "deployment",
            "component": "auth-svc",
            "description": "bot-protection challenge enabled",
        },
        remediation=("disable_faulty_player_feature_flag",),
        difficulty=0.4,
    )
)

_add(
    FaultSpec(
        "purchase.failure",
        FailureClass.PLAYER_PURCHASE_FAILURE,
        FeatureType.ACCESSIBLE_PURCHASE,
        "Accessible purchase broken",
        "The ticket purchase form loses its labels, blocking keyboard and screen-reader buyers.",
        "purchase-flow",
        onset="step",
        params={"purchase_completion": 0.2},
        default_scope=_scope(),
        expected_slos=("purchase.completion",),
        remediation=("restore_known_good_player_version",),
        difficulty=0.4,
    )
)

# ---------------------------------------------------------------------------
# Infrastructure
# ---------------------------------------------------------------------------

_add(
    FaultSpec(
        "infra.cdn_regional",
        FailureClass.INFRA_CDN_REGIONAL,
        FeatureType.CAPTIONS,
        "CDN regional failure",
        "One CDN region stops serving accessibility renditions.",
        "region-eu-west",
        onset="step",
        params={"availability": 0.1, "applies_all_features": True},
        default_scope=_scope(cdn_regions=["eu-west"], territories=["FR", "GB", "ES"]),
        expected_slos=("cap.availability", "sign.availability", "ad.track_present"),
        causal_change={
            "kind": "provider",
            "component": "cdn-primary",
            "description": "provider maintenance in eu-west",
        },
        remediation=("reroute_region",),
        difficulty=0.35,
    )
)

_add(
    FaultSpec(
        "infra.packet_loss",
        FailureClass.INFRA_PACKET_LOSS,
        FeatureType.CAPTIONS,
        "Transport packet loss",
        "Packet loss on the contribution link causes cue gaps and drift.",
        "cdn-primary",
        onset="intermittent",
        params={
            "cue_rate_multiplier": 0.7,
            "drift_seconds": 1.2,
            "packet_loss": 0.08,
            "duty_cycle": 0.5,
            "period_s": 10.0,
        },
        default_scope=_scope(cdn_regions=["eu-central"], territories=["DE"]),
        expected_slos=("cap.availability", "cap.drift"),
        remediation=("reroute_region",),
        difficulty=0.75,
    )
)

_add(
    FaultSpec(
        "infra.encoder_cpu",
        FailureClass.INFRA_ENCODER_CPU,
        FeatureType.CAPTIONS,
        "Encoder CPU saturation",
        "Encoder CPU saturation delays cue emission and lengthens queues.",
        "capenc-pool-a",
        onset="ramp",
        ramp_seconds=120.0,
        params={"drift_seconds": 4.0, "cpu_saturation": 0.97},
        default_scope=_scope(),
        expected_slos=("cap.drift", "cap.first_caption_latency"),
        causal_change={
            "kind": "traffic",
            "component": "capenc-pool-a",
            "description": "concurrent stream count doubled",
        },
        remediation=("switch_caption_encoder_pool", "select_synchronized_standby"),
        difficulty=0.7,
    )
)

_add(
    FaultSpec(
        "infra.gpu_saturation",
        FailureClass.INFRA_GPU_SATURATION,
        FeatureType.SIGN_LANGUAGE,
        "GPU saturation on the sign pipeline",
        "Interpreter feed transcoding falls behind because the GPU pool is saturated.",
        "signsrc-lsf",
        onset="ramp",
        ramp_seconds=90.0,
        params={"fps": 18.0, "gpu_saturation": 0.99},
        default_scope=_scope(territories=["FR", "CA"]),
        expected_slos=("sign.framerate", "sign.frozen"),
        remediation=("restart_sign_language_pipeline_component",),
        difficulty=0.7,
    )
)

_add(
    FaultSpec(
        "infra.clock_source_change",
        FailureClass.INFRA_CLOCK_SOURCE_CHANGE,
        FeatureType.CAPTIONS,
        "Clock source change",
        "A grandmaster failover changes the timing reference for the whole caption chain.",
        "clock-ptp-primary",
        onset="ramp",
        ramp_seconds=150.0,
        params={"drift_seconds": 6.0, "also_affects_sign": True},
        default_scope=_scope(),
        expected_slos=("cap.drift", "sign.sync"),
        causal_change={
            "kind": "config",
            "component": "clock-ptp-primary",
            "description": "PTP grandmaster failover to NTP fallback pool",
        },
        remediation=("change_clock_source", "select_synchronized_standby"),
        difficulty=0.8,
    )
)

_add(
    FaultSpec(
        "infra.deploy_regression",
        FailureClass.INFRA_DEPLOY_REGRESSION,
        FeatureType.ACCESSIBLE_PLAYER,
        "Deployment regression",
        "A player deployment regresses several accessibility behaviours at once.",
        "pv-ctv-9.4.0",
        onset="step",
        params={
            "accessible_name_ratio": 0.6,
            "caption_control_ok": 0.3,
            "screenreader_completion": 0.35,
            "render_success": 0.5,
        },
        default_scope=_scope(platforms=CTV, player_versions=["ctv-9.4.0"]),
        expected_slos=(
            "player.accessible_name",
            "player.caption_control",
            "player.screen_reader",
            "cap.render_success",
        ),
        causal_change={
            "kind": "deployment",
            "component": "pv-ctv-9.4.0",
            "description": "player 9.4.0 canary to 25% of CTV traffic",
        },
        remediation=("restore_known_good_player_version",),
        difficulty=0.55,
    )
)

_add(
    FaultSpec(
        "infra.malformed_manifest",
        FailureClass.INFRA_MALFORMED_MANIFEST,
        FeatureType.CAPTIONS,
        "Malformed manifest",
        "The manifest declares tracks with invalid language tags; players ignore them.",
        "manifest-main",
        onset="step",
        params={"drop_track": True, "malformed": True},
        default_scope=_scope(languages=["de", "es"]),
        expected_slos=("cap.availability", "cap.render_success"),
        causal_change={
            "kind": "manifest",
            "component": "manifest-main",
            "description": "manifest template updated",
        },
        remediation=("republish_corrected_manifest",),
        difficulty=0.4,
    )
)

_add(
    FaultSpec(
        "infra.stale_config",
        FailureClass.INFRA_STALE_CONFIG,
        FeatureType.CAPTIONS,
        "Stale configuration",
        "One encoder node keeps an old track map after a partial config rollout.",
        "capenc-pool-a",
        onset="intermittent",
        params={"substitute_language": "en", "duty_cycle": 0.25, "period_s": 12.0},
        default_scope=_scope(languages=["fr"]),
        expected_slos=("cap.wrong_language",),
        causal_change={
            "kind": "config",
            "component": "capenc-pool-a",
            "description": "partial track-map rollout",
        },
        remediation=("switch_caption_encoder_pool", "reroute_caption_path"),
        difficulty=0.85,
    )
)

_add(
    FaultSpec(
        "infra.provider_degradation",
        FailureClass.INFRA_PROVIDER_DEGRADATION,
        FeatureType.CAPTIONS,
        "Caption provider degradation",
        "The caption provider degrades globally: latency up, quality down.",
        "capsrc-en",
        onset="ramp",
        ramp_seconds=180.0,
        params={"drift_seconds": 3.5, "drop_probability": 0.1},
        default_scope=_scope(),
        expected_slos=("cap.drift", "cap.omission_rate", "cap.semantic"),
        causal_change={
            "kind": "provider",
            "component": "capsrc-en",
            "description": "verbaflow region migration",
        },
        remediation=("switch_to_approved_alternate_language_source", "reroute_caption_path"),
        difficulty=0.8,
    )
)


def all_faults() -> list[FaultSpec]:
    return list(FAULT_LIBRARY.values())


def faults_for(feature: FeatureType) -> list[FaultSpec]:
    return [f for f in FAULT_LIBRARY.values() if f.feature is feature]


HERO_FAULT_ID = "cap.progressive_drift"
