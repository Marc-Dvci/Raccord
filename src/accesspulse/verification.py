"""Verification suites.

An incident does not close because an action ran. It closes because the
experience was re-measured, on the slices that were broken *and* on the slices
that were healthy, and every mandatory assertion passed.

Each suite re-runs the probe fleet against the live environment after the action:

* `original`  - the exact slices that breached, must now be inside objective
* `adjacent`  - neighbouring languages, territories, platforms and features that
                were healthy, must still be healthy (no regression)
* `dependent` - features that share the repaired component

If a mandatory assertion fails and the approved action declared automatic
rollback, the verification agent triggers it and the incident moves to
ROLLED_BACK rather than RECOVERED.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from .assurance import evaluate_report
from .contracts import (
    AssertionStatus,
    FeatureType,
    Scope,
    SLOTier,
    VerificationAssertion,
)
from .probes import run_for_feature
from .simulator import MediaSimulator, _platform_of
from .slo import SLO_BY_ID
from .twin import LANGUAGES, PLAYER_VERSIONS, TERRITORIES

ScopeKind = Literal["original", "adjacent", "dependent"]


@dataclass(frozen=True)
class AssertionSpec:
    name: str
    description: str
    slo_id: str
    scope_kind: ScopeKind
    mandatory: bool = True
    feature: FeatureType | None = None


SUITES: dict[str, tuple[AssertionSpec, ...]] = {
    "caption_recovery": (
        AssertionSpec("caption_drift_recovered",
                      "Caption drift is back inside the objective on every affected slice",
                      "cap.drift", "original"),
        AssertionSpec("caption_availability_held",
                      "Caption track availability did not drop during the switch",
                      "cap.availability", "original"),
        AssertionSpec("caption_omission_recovered",
                      "Word omission is back inside the objective",
                      "cap.omission_rate", "original"),
        AssertionSpec("caption_semantics_recovered",
                      "Semantic preservation is back above the objective",
                      "cap.semantic", "original", mandatory=False),
        AssertionSpec("adjacent_languages_unregressed",
                      "Captions in the other promised languages are unaffected",
                      "cap.drift", "adjacent"),
        AssertionSpec("adjacent_territories_unregressed",
                      "Captions in unaffected territories are unaffected",
                      "cap.availability", "adjacent"),
        AssertionSpec("description_unregressed",
                      "Audio description was not disturbed by the caption change",
                      "ad.audio_present", "dependent", feature=FeatureType.AUDIO_DESCRIPTION),
        AssertionSpec("sign_unregressed",
                      "The interpreter feed was not disturbed by the caption change",
                      "sign.sync", "dependent", feature=FeatureType.SIGN_LANGUAGE),
        AssertionSpec("player_journeys_unregressed",
                      "Keyboard and screen-reader journeys still complete",
                      "player.keyboard", "dependent", feature=FeatureType.ACCESSIBLE_PLAYER),
    ),
    "player_recovery": (
        AssertionSpec("keyboard_journey_recovered", "Keyboard journey completes again",
                      "player.keyboard", "original"),
        AssertionSpec("screenreader_journey_recovered",
                      "Screen-reader journey completes again",
                      "player.screen_reader", "original"),
        AssertionSpec("accessible_names_recovered", "Controls expose accessible names",
                      "player.accessible_name", "original"),
        AssertionSpec("caption_render_recovered", "Captions render on the affected devices",
                      "cap.render_success", "original", feature=FeatureType.CAPTIONS),
        AssertionSpec("other_platforms_unregressed",
                      "Other platforms were not affected by the rollback",
                      "player.keyboard", "adjacent"),
    ),
    "manifest_recovery": (
        AssertionSpec("track_declared_recovered",
                      "The promised track is declared in the manifest again",
                      "ad.track_present", "original", feature=FeatureType.AUDIO_DESCRIPTION),
        AssertionSpec("track_selectable", "The track can be selected in a real player",
                      "ad.selection", "original", feature=FeatureType.AUDIO_DESCRIPTION),
        AssertionSpec("caption_tracks_unregressed",
                      "Caption tracks were not removed by the republish",
                      "cap.availability", "dependent", feature=FeatureType.CAPTIONS),
    ),
    "sign_recovery": (
        AssertionSpec("sign_continuity_recovered", "Interpreter feed frames are continuous",
                      "sign.frozen", "original", feature=FeatureType.SIGN_LANGUAGE),
        AssertionSpec("sign_framerate_recovered", "Interpreter feed frame rate recovered",
                      "sign.framerate", "original", feature=FeatureType.SIGN_LANGUAGE),
        AssertionSpec("sign_visibility_recovered", "The interpreter is fully visible",
                      "sign.visibility", "original", feature=FeatureType.SIGN_LANGUAGE),
        AssertionSpec("captions_unregressed", "Captions were not disturbed",
                      "cap.drift", "dependent", feature=FeatureType.CAPTIONS),
    ),
    "delivery_recovery": (
        AssertionSpec("availability_recovered", "The rerouted region serves the promise again",
                      "cap.availability", "original"),
        AssertionSpec("target_region_unregressed", "The region absorbing traffic stayed healthy",
                      "cap.availability", "adjacent"),
        AssertionSpec("sign_availability_recovered", "Interpreter feed is delivered again",
                      "sign.availability", "dependent", feature=FeatureType.SIGN_LANGUAGE),
    ),
    "communication": (
        AssertionSpec("status_published", "A status update was published",
                      "cap.availability", "original", mandatory=False),
    ),
}
SUITES["description_recovery"] = (
    AssertionSpec("described_audio_audible", "Described audio is audible again",
                  "ad.audio_present", "original", feature=FeatureType.AUDIO_DESCRIPTION),
    AssertionSpec("described_track_declared", "The described track is declared",
                  "ad.track_present", "original", feature=FeatureType.AUDIO_DESCRIPTION),
    AssertionSpec("described_track_selectable", "The described track can be selected",
                  "ad.selection", "original", feature=FeatureType.AUDIO_DESCRIPTION),
    AssertionSpec("description_timing_recovered", "Descriptions land in the dialogue gaps",
                  "ad.drift", "original", feature=FeatureType.AUDIO_DESCRIPTION,
                  mandatory=False),
    AssertionSpec("captions_unregressed", "Captions were not disturbed",
                  "cap.drift", "dependent", feature=FeatureType.CAPTIONS),
    AssertionSpec("other_territories_unregressed", "Unaffected territories are unaffected",
                  "ad.track_present", "adjacent", feature=FeatureType.AUDIO_DESCRIPTION),
)

SUITES["access_flow_recovery"] = (
    AssertionSpec("auth_completes", "Accessible sign-in completes again",
                  "auth.completion", "original", feature=FeatureType.ACCESSIBLE_AUTH),
    AssertionSpec("purchase_completes", "Accessible purchase completes again",
                  "purchase.completion", "original", feature=FeatureType.ACCESSIBLE_PURCHASE),
    AssertionSpec("player_journeys_unregressed", "Playback journeys still complete",
                  "player.keyboard", "dependent", feature=FeatureType.ACCESSIBLE_PLAYER),
)

SUITES["default"] = SUITES["caption_recovery"]

# An action can serve several features (a clock change moves captions, described
# audio and the interpreter feed alike). The suite that must pass is the one for
# the feature that actually broke, not the action's default.
_SUITE_BY_FEATURE: dict[FeatureType, str] = {
    FeatureType.CAPTIONS: "caption_recovery",
    FeatureType.SUBTITLES: "caption_recovery",
    FeatureType.AUDIO_DESCRIPTION: "description_recovery",
    FeatureType.ALTERNATE_AUDIO: "description_recovery",
    FeatureType.SIGN_LANGUAGE: "sign_recovery",
    FeatureType.ACCESSIBLE_PLAYER: "player_recovery",
    FeatureType.ACCESSIBLE_AUTH: "access_flow_recovery",
    FeatureType.ACCESSIBLE_PURCHASE: "access_flow_recovery",
}


def suite_for(feature: FeatureType, action_default: str) -> str:
    """Pick the verification suite for the feature that broke.

    Falls back to the action's declared suite for features with no dedicated
    suite, and never invents one that is not in SUITES.
    """
    candidate = _SUITE_BY_FEATURE.get(feature, action_default)
    return candidate if candidate in SUITES else action_default


def _slices_for(scope: Scope, kind: ScopeKind) -> list[tuple[str, str, str]]:
    """Return (language, territory, player_version) triples to re-measure."""
    langs = list(scope.languages) or ["en"]
    terrs = list(scope.territories) or ["FR"]
    pvs = list(scope.player_versions) or list(PLAYER_VERSIONS)

    if kind == "original":
        return [(lang, t, p) for lang in langs for t in terrs for p in pvs]

    if kind == "adjacent":
        other_langs = [lang for lang in LANGUAGES if lang not in langs] or langs
        other_terrs = [t for t in TERRITORIES if t not in terrs] or terrs
        other_pvs = [p for p in PLAYER_VERSIONS if p not in pvs] or pvs
        out = [(lang, terrs[0], pvs[0]) for lang in other_langs[:3]]
        out += [(langs[0], t, pvs[0]) for t in other_terrs[:3]]
        out += [(langs[0], terrs[0], p) for p in other_pvs[:2]]
        return out

    # dependent: same audience slices, different feature
    return [(langs[0], t, pvs[0]) for t in terrs[:3]]


def run_suite(
    suite_name: str,
    sim: MediaSimulator,
    scope: Scope,
    tier: SLOTier = SLOTier.TIER_0_GLOBAL_LIVE,
    window_s: float = 30.0,
) -> list[VerificationAssertion]:
    suite = SUITES.get(suite_name, SUITES["default"])
    assertions: list[VerificationAssertion] = []

    for spec in suite:
        slo = SLO_BY_ID.get(spec.slo_id)
        if slo is None:
            continue
        feature = spec.feature or slo.feature
        observed_values: list[float] = []
        abstentions = 0

        for language, territory, pv in _slices_for(scope, spec.scope_kind):
            if feature is FeatureType.SIGN_LANGUAGE and territory not in ("FR", "CA"):
                continue
            obs = sim.observe(language, territory, _platform_of(pv), pv, window_s)
            report = run_for_feature(obs, feature, language)
            evals = [e for e in evaluate_report(report, tier) if e.slo_id == spec.slo_id]
            for e in evals:
                if e.abstained:
                    abstentions += 1
                else:
                    observed_values.append(e.observed)

        if not observed_values:
            status = AssertionStatus.INCONCLUSIVE
            observed = None
        else:
            worst = (max(observed_values) if slo.comparator.value == "lower_is_better"
                     else min(observed_values))
            observed = worst
            status = (AssertionStatus.FAILING if slo.breached(worst, tier)
                      else AssertionStatus.PASSING)

        # A regression check on a feature this audience slice does not receive
        # produces no data. That is not evidence of recovery and not evidence of
        # regression: it is recorded as inconclusive and stops being blocking,
        # rather than being quietly counted as a pass.
        mandatory = spec.mandatory
        if status is AssertionStatus.INCONCLUSIVE and spec.scope_kind != "original":
            mandatory = False

        assertions.append(VerificationAssertion(
            assertion_id=f"va-{uuid.uuid4().hex[:8]}",
            incident_id=scope.incident_id,
            name=spec.name,
            description=spec.description,
            mandatory=mandatory,
            status=status,
            observed=observed,
            threshold=slo.threshold(tier),
            comparator="lte" if slo.comparator.value == "lower_is_better" else "gte",
            scope_note=(
                f"{spec.scope_kind} scope, {len(observed_values)} slice(s) measured"
                + (f", {abstentions} abstained" if abstentions else "")
            ),
        ))
    return assertions


def all_mandatory_passed(assertions: list[VerificationAssertion]) -> bool:
    return all(
        a.status is AssertionStatus.PASSING
        for a in assertions
        if a.mandatory
    )


def failing(assertions: list[VerificationAssertion]) -> list[VerificationAssertion]:
    return [a for a in assertions if a.status is AssertionStatus.FAILING]


def summarise(assertions: list[VerificationAssertion]) -> dict[str, int]:
    out = {"passing": 0, "failing": 0, "pending": 0, "inconclusive": 0}
    for a in assertions:
        out[a.status.value] += 1
    return out
