"""The instrumented digital-twin media environment.

This is a real, running delivery chain - not a bag of canned JSON. It advances a
clock, produces timed caption cues, described-audio windows, interpreter-feed
frame statistics, synthetic player journeys, manifests, transport counters and
privacy-preserving session aggregates. Faults change what the audience actually
receives; approved remediation actions change the environment back.

Everything is seeded, so a scenario replays identically for the benchmark, the
demonstration and the judge reset.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable

from . import media
from .contracts import (
    ActionType,
    ChangeEvent,
    FeatureType,
    Platform,
    SessionAggregate,
    utcnow,
)
from .faults import FAULT_LIBRARY, FaultSpec
from .twin import (
    LANGUAGES,
    PLAYER_VERSIONS,
    TERRITORIES,
    DigitalTwin,
    build_reference_twin,
    territory_regions,
)

SILENCE_FLOOR_DBFS = -50.0
TARGET_LUFS = -23.0


# ---------------------------------------------------------------------------
# Observation records - exactly what a probe sees
# ---------------------------------------------------------------------------


@dataclass
class CaptionCue:
    start_s: float
    end_s: float
    text: str
    speaker: str
    language: str
    rendered: bool = True


@dataclass
class DescribedWindow:
    start_s: float
    end_s: float
    target_scene_start: float
    peak_dbfs: float
    loudness_lufs: float
    language: str
    channels: int
    covers_scene_index: int


@dataclass
class SignFeedStats:
    frames_expected: int
    frames_delivered: int
    frozen_frames: int
    black_frames: int
    fps: float
    interpreter_visible_ratio: float
    sync_drift_s: float
    pip_overlap_ratio: float


@dataclass
class PlayerJourney:
    player_version: str
    platform: str
    device_class: str
    keyboard_completed: bool
    screenreader_completed: bool
    accessible_name_ratio: float
    focus_visible_ratio: float
    caption_control_ok: bool
    audio_track_selection_ok: bool
    reduced_motion_ok: bool
    captions_rendered: bool
    auth_completed: bool
    purchase_completed: bool
    console_errors: list[str] = field(default_factory=list)
    a11y_tree_nodes: int = 0


@dataclass
class TransportStats:
    packet_loss_ratio: float
    retransmit_ratio: float
    rtt_ms: float
    edge_5xx_ratio: float
    origin_cpu: float
    encoder_cpu: float
    gpu_utilisation: float


@dataclass
class SliceObservation:
    language: str
    territory: str
    platform: str
    player_version: str
    cdn_region: str
    window_start_s: float
    window_end_s: float
    wall_clock: datetime

    reference_tokens: list[tuple[float, str]]
    cues: list[CaptionCue]
    manifest_caption_tracks: list[str]
    manifest_audio_tracks: list[str]
    described: list[DescribedWindow]
    described_language: str
    sign: SignFeedStats | None
    sign_declared: bool
    player: PlayerJourney
    transport: TransportStats
    sessions_with_captions: int
    sessions_with_description: int
    sessions_with_sign: int
    selection_failures: int
    playback_errors: int


@dataclass
class ActiveFault:
    uid: str
    spec: FaultSpec
    injected_at_wall: datetime
    injected_at_program_s: float
    scope: dict[str, Any]
    neutralised: bool = False
    neutralised_at_wall: datetime | None = None

    def intensity(self, program_s: float) -> float:
        """0..1 symptom strength at programme time."""
        if self.neutralised:
            return 0.0
        elapsed = program_s - self.injected_at_program_s
        if elapsed < 0:
            return 0.0
        onset = self.spec.onset
        if onset == "step":
            return 1.0
        if onset == "ramp":
            r = self.spec.ramp_seconds or 60.0
            return min(1.0, elapsed / r)
        # Intermittent and flapping faults are observed through windowed probes,
        # not instantaneous samples: a 30 s window over an 8 s cycle always
        # contains both phases. Reporting the duty-cycle average is what the
        # window actually contains; reporting 0 or 1 from a single instant would
        # make detection depend on when the probe happened to fire.
        period = max(0.1, float(self.spec.params.get("period_s", 8.0)))
        duty = float(self.spec.params.get("duty_cycle", 0.5))
        if onset == "intermittent":
            return duty
        # flap: symmetric on/off, plus the current phase so logs stay bursty
        phase_on = int(elapsed // period) % 2 == 0
        return 0.5 + (0.25 if phase_on else -0.25)

    def matches(
        self,
        language: str,
        territory: str,
        platform: str,
        player_version: str,
        cdn_region: str,
    ) -> bool:
        s = self.scope
        if s.get("languages") and language not in s["languages"]:
            return False
        if s.get("territories") and territory not in s["territories"]:
            return False
        if s.get("platforms") and platform not in s["platforms"]:
            return False
        if s.get("player_versions") and player_version not in s["player_versions"]:
            return False
        if s.get("cdn_regions") and cdn_region not in s["cdn_regions"]:
            return False
        return True


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------

BASE_SESSIONS = {
    "FR": 21000,
    "DE": 17400,
    "ES": 9800,
    "GB": 15200,
    "US": 34500,
    "CA": 8100,
    "BR": 7300,
    "JP": 6400,
}
CAPTION_ENABLE_RATE = 0.31
DESCRIPTION_ENABLE_RATE = 0.021
SIGN_ENABLE_RATE = 0.009
PLATFORM_SHARE = {"web": 0.34, "ctv": 0.41, "ios": 0.14, "android": 0.11}
PLAYER_VERSION_SHARE = {
    "web-4.12.0": 1.0,
    "ctv-9.3.1": 0.75,
    "ctv-9.4.0": 0.25,
    "ios-6.2.0": 1.0,
    "android-6.2.0": 1.0,
}


class MediaSimulator:
    def __init__(
        self,
        twin: DigitalTwin | None = None,
        seed: int = 20260803,
        event_id: str = "evt-lumiere-premiere",
        start_wall: datetime | None = None,
    ) -> None:
        self.twin = twin or build_reference_twin(event_id)
        self.event_id = event_id
        self.seed = seed
        self.program_s: float = 0.0
        self.start_wall = start_wall or utcnow()
        self.active_faults: list[ActiveFault] = []
        self.changes: list[ChangeEvent] = []
        self._change_seq = 0
        self._fault_seq = 0

        # Remediation state - the levers approved actions can pull.
        self.caption_encoder_pool = "capenc-pool-a"
        self.clock_source = "clock-ptp-primary"
        self.caption_path = "primary"
        self.pinned_player_versions: dict[str, str] = {}
        self.manifest_generation = 1
        self.rerouted_regions: dict[str, str] = {}
        self.disabled_feature_flags: set[str] = set()
        self.restarted_components: set[str] = set()
        self.alternate_language_sources: dict[str, str] = {}
        self.restored_audio_tracks: set[str] = set()
        self.applied_actions: list[tuple[ActionType, str, datetime]] = []
        self.status_updates: list[dict[str, Any]] = []

        self._seed_background_changes()

    # -- clock -------------------------------------------------------------
    @property
    def wall_clock(self) -> datetime:
        return self.start_wall + timedelta(seconds=self.program_s)

    def advance(self, seconds: float) -> None:
        self.program_s += seconds

    def reset(self) -> None:
        self.__init__(  # noqa: PLC2801 - deliberate full reset for judge mode
            twin=build_reference_twin(self.event_id),
            seed=self.seed,
            event_id=self.event_id,
            start_wall=self.start_wall,
        )

    # -- randomness --------------------------------------------------------
    def _rng(self, *parts: Any) -> random.Random:
        key = "|".join(str(p) for p in (self.seed, *parts))
        digest = hashlib.blake2b(key.encode(), digest_size=8).digest()
        return random.Random(int.from_bytes(digest, "big"))

    # -- change events -----------------------------------------------------
    def _next_change_id(self) -> str:
        self._change_seq += 1
        return f"chg-{self._change_seq:04d}"

    def record_change(
        self,
        kind: str,
        component: str,
        description: str,
        at: datetime | None = None,
        actor: str = "release-automation",
        payload: dict[str, Any] | None = None,
    ) -> ChangeEvent:
        ev = ChangeEvent(
            change_id=self._next_change_id(),
            kind=kind,  # type: ignore[arg-type]
            component=component,
            description=description,
            at=at or self.wall_clock,
            actor=actor,
            payload=payload or {},
        )
        self.changes.append(ev)
        return ev

    def _seed_background_changes(self) -> None:
        """Decoys. A real change window is never empty, so correlation must work."""
        base = self.start_wall - timedelta(minutes=45)
        decoys = [
            ("deployment", "packager-main", "packager 7.4.1 routine rollout", 45),
            ("config", "cdn-primary", "cache TTL tuned for manifest objects", 38),
            ("deployment", "auth-svc", "auth-svc 2.9.0 dependency bump", 31),
            ("traffic", "cdn-primary", "pre-show traffic ramp +18%", 22),
            ("config", "origin-main", "origin shield concurrency raised", 16),
            ("deployment", "pv-web-4.12.0", "web player 4.12.0 to 100%", 12),
            ("routing", "region-us-west", "us-west peering change", 7),
        ]
        for kind, component, desc, minutes_ago in decoys:
            self.record_change(kind, component, desc, at=base + timedelta(minutes=45 - minutes_ago))

    def changes_between(self, start: datetime, end: datetime) -> list[ChangeEvent]:
        return [c for c in self.changes if start <= c.at <= end]

    # -- fault injection ---------------------------------------------------
    def inject(
        self,
        fault_id: str,
        scope_override: dict[str, Any] | None = None,
        emit_causal_change: bool = True,
    ) -> ActiveFault:
        spec = FAULT_LIBRARY[fault_id]
        self._fault_seq += 1
        scope = dict(spec.default_scope)
        if scope_override:
            scope.update(scope_override)

        if emit_causal_change and spec.causal_change:
            cc = spec.causal_change
            self.record_change(
                cc["kind"],
                cc["component"],
                cc["description"],
                at=self.wall_clock - timedelta(seconds=30),
                actor="release-automation",
                payload={"linked_fault": fault_id},
            )

        af = ActiveFault(
            uid=f"flt-{self._fault_seq:03d}",
            spec=spec,
            injected_at_wall=self.wall_clock,
            injected_at_program_s=self.program_s,
            scope=scope,
        )
        self.active_faults.append(af)
        self.twin.set_health(
            spec.component.format(language=(scope.get("languages") or ["en"])[0]), False
        )
        return af

    def clear_fault(self, uid: str) -> None:
        for f in self.active_faults:
            if f.uid == uid:
                f.neutralised = True
                f.neutralised_at_wall = self.wall_clock

    def ground_truth(self) -> list[dict[str, Any]]:
        return [
            {
                "uid": f.uid,
                "fault_id": f.spec.fault_id,
                "failure_class": f.spec.failure_class.value,
                "component": f.spec.component,
                "scope": f.scope,
                "neutralised": f.neutralised,
            }
            for f in self.active_faults
        ]

    # -- remediation -------------------------------------------------------
    def apply_action(
        self, action_type: ActionType, target: str, parameters: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Executes an allow-listed action against the environment.

        Returns before/after state. A fault is neutralised only if the action is
        genuinely corrective for it - a plausible-but-wrong action changes state
        and leaves the symptom in place, which is exactly what verification is
        for.
        """
        parameters = parameters or {}
        before = self.state_snapshot()

        if action_type is ActionType.SWITCH_CAPTION_ENCODER_POOL:
            self.caption_encoder_pool = target or "capenc-pool-b"
        elif action_type is ActionType.SELECT_SYNCHRONIZED_STANDBY:
            self.caption_encoder_pool = target or "capenc-pool-b"
            self.clock_source = "clock-ptp-primary"
        elif action_type is ActionType.CHANGE_CLOCK_SOURCE:
            self.clock_source = target or "clock-ptp-primary"
        elif action_type is ActionType.REROUTE_CAPTION_PATH:
            self.caption_path = target or "secondary"
        elif action_type is ActionType.RESTORE_KNOWN_GOOD_PLAYER:
            bad = parameters.get("from_version", target)
            good = parameters.get("to_version", "ctv-9.3.1")
            self.pinned_player_versions[bad] = good
        elif action_type is ActionType.REPUBLISH_MANIFEST:
            self.manifest_generation += 1
        elif action_type is ActionType.REROUTE_REGION:
            # Targets are twin node ids ("region-eu-west"); the delivery path is
            # keyed by the bare region name.
            self.rerouted_regions[target.removeprefix("region-")] = parameters.get(
                "to_region", "eu-central"
            ).removeprefix("region-")
        elif action_type is ActionType.RESTORE_AUDIO_TRACK:
            self.restored_audio_tracks.add(target)
            self.manifest_generation += 1
        elif action_type is ActionType.SWITCH_ALTERNATE_LANGUAGE_SOURCE:
            self.alternate_language_sources[parameters.get("language", "en")] = target
        elif action_type is ActionType.RESTART_SIGN_PIPELINE:
            self.restarted_components.add(target or "signsrc-lsf")
        elif action_type is ActionType.DISABLE_PLAYER_FEATURE_FLAG:
            self.disabled_feature_flags.add(target)
        elif action_type is ActionType.ISSUE_STATUS_UPDATE:
            self.status_updates.append(
                {"at": self.wall_clock.isoformat(), "body": parameters.get("body", "")}
            )

        self.applied_actions.append((action_type, target, self.wall_clock))
        self.record_change(
            "config",
            target or "raccord",
            f"remediation: {action_type.value}",
            actor="raccord.remediation",
            payload={"action": action_type.value, "target": target},
        )

        # Neutralise every fault this action genuinely corrects.
        for f in self.active_faults:
            if f.neutralised:
                continue
            if action_type.value in f.spec.remediation:
                f.neutralised = True
                f.neutralised_at_wall = self.wall_clock
                self.twin.set_health(f.spec.component, True)

        return {"before": before, "after": self.state_snapshot()}

    def state_snapshot(self) -> dict[str, Any]:
        return {
            "caption_encoder_pool": self.caption_encoder_pool,
            "clock_source": self.clock_source,
            "caption_path": self.caption_path,
            "manifest_generation": self.manifest_generation,
            "pinned_player_versions": dict(self.pinned_player_versions),
            "rerouted_regions": dict(self.rerouted_regions),
            "disabled_feature_flags": sorted(self.disabled_feature_flags),
            "restarted_components": sorted(self.restarted_components),
            "active_faults": [f.uid for f in self.active_faults if not f.neutralised],
        }

    # -- effective parameters for a slice ---------------------------------
    def _effective(
        self,
        language: str,
        territory: str,
        platform: str,
        player_version: str,
        cdn_region: str,
        feature: FeatureType | None = None,
    ) -> dict[str, Any]:
        eff: dict[str, Any] = {}
        effective_pv = self.pinned_player_versions.get(player_version, player_version)
        effective_region = self.rerouted_regions.get(cdn_region, cdn_region)
        for f in self.active_faults:
            if f.neutralised:
                continue
            if not f.matches(language, territory, platform, effective_pv, effective_region):
                continue
            if feature is not None and not _feature_affected(
                f.spec.feature, feature, f.spec.params
            ):
                continue
            k = f.intensity(self.program_s)
            if k <= 0:
                continue
            for key, value in f.spec.params.items():
                if key in (
                    "duty_cycle",
                    "period_s",
                    "applies_all_features",
                    "also_affects_sign",
                    "malformed",
                    "expected_channels",
                ):
                    continue
                if isinstance(value, bool):
                    eff[key] = eff.get(key, False) or (k >= 0.5 and value)
                elif isinstance(value, (int, float)):
                    scaled = _scale(key, float(value), k)
                    eff[key] = _combine(key, eff.get(key), scaled)
                else:
                    eff[key] = value
            eff.setdefault("_faults", []).append(f.uid)  # type: ignore[union-attr]
        return eff

    # -- observation -------------------------------------------------------
    def observe(
        self,
        language: str = "en",
        territory: str = "FR",
        platform: str = "ctv",
        player_version: str = "ctv-9.4.0",
        window_s: float = 10.0,
    ) -> SliceObservation:
        cdn_region = (territory_regions(territory) or ["eu-west"])[0]
        start = max(0.0, self.program_s - window_s)
        end = self.program_s
        rng = self._rng("obs", language, territory, platform, player_version, round(start, 1))

        eff_cap = self._effective(
            language, territory, platform, player_version, cdn_region, FeatureType.CAPTIONS
        )
        eff_ad = self._effective(
            language, territory, platform, player_version, cdn_region, FeatureType.AUDIO_DESCRIPTION
        )
        eff_sign = self._effective(
            language, territory, platform, player_version, cdn_region, FeatureType.SIGN_LANGUAGE
        )
        eff_player = self._effective(
            language, territory, platform, player_version, cdn_region, FeatureType.ACCESSIBLE_PLAYER
        )
        eff_auth = self._effective(
            language, territory, platform, player_version, cdn_region, FeatureType.ACCESSIBLE_AUTH
        )
        eff_purchase = self._effective(
            language,
            territory,
            platform,
            player_version,
            cdn_region,
            FeatureType.ACCESSIBLE_PURCHASE,
        )

        reference = media.spoken_tokens(start, end, language)
        cues = self._build_cues(language, start, end, eff_cap, rng)
        caption_tracks, audio_tracks = self._manifest_tracks(eff_cap, eff_ad, language)
        described, described_language = self._build_description(language, start, end, eff_ad, rng)
        sign_declared = territory in ("FR", "CA") and platform in ("web", "ctv")
        sign = self._build_sign(start, end, eff_sign, rng) if sign_declared else None
        player = self._build_player_journey(
            platform, player_version, eff_cap, eff_ad, eff_player, eff_auth, eff_purchase, rng
        )
        transport = self._build_transport(eff_cap, eff_sign, rng)
        sess = self._sessions(territory, platform, player_version)

        return SliceObservation(
            language=language,
            territory=territory,
            platform=platform,
            player_version=player_version,
            cdn_region=cdn_region,
            window_start_s=start,
            window_end_s=end,
            wall_clock=self.wall_clock,
            reference_tokens=reference,
            cues=cues,
            manifest_caption_tracks=caption_tracks,
            manifest_audio_tracks=audio_tracks,
            described=described,
            described_language=described_language,
            sign=sign,
            sign_declared=sign_declared,
            player=player,
            transport=transport,
            sessions_with_captions=sess["captions"],
            sessions_with_description=sess["description"],
            sessions_with_sign=sess["sign"],
            selection_failures=int(
                sess["captions"] * (1.0 - _get(eff_cap, "render_success", 1.0)) * 0.4
            ),
            playback_errors=int(sess["captions"] * _get(eff_cap, "packet_loss", 0.0) * 0.2),
        )

    # -- builders ----------------------------------------------------------
    def _build_cues(
        self,
        language: str,
        start: float,
        end: float,
        eff: dict[str, Any],
        rng: random.Random,
    ) -> list[CaptionCue]:
        drift = _get(eff, "drift_seconds", 0.0)
        rate = _get(eff, "cue_rate_multiplier", 1.0)
        availability = _get(eff, "availability", 1.0)
        drop_p = _get(eff, "drop_probability", 0.0)
        dup_p = _get(eff, "duplicate_probability", 0.0)
        dur_mult = _get(eff, "duration_multiplier", 1.0)
        flicker_p = _get(eff, "flicker_probability", 0.0)
        render_success = _get(eff, "render_success", 1.0)
        sub_lang = eff.get("substitute_language")
        swap_p = _get(eff, "swap_probability", 0.0)
        if eff.get("drop_track"):
            return []

        emit_lang = sub_lang or language
        cues: list[CaptionCue] = []
        prev_text: str | None = None
        # Cues are emitted for dialogue whose *shifted* window falls in [start, end)
        for abs_start, line in media.lines_in(start - drift - 2.0, end - drift + 2.0):
            cue_start = abs_start + drift
            spoken = line.end_s - line.start_s
            # A healthy caption system holds a cue long enough to be read: at
            # least the spoken duration, and never faster than 18 characters
            # per second. Reading-speed faults compress this deliberately.
            readable = len(line.text.get(sub_lang or language, line.text["en"])) / 18.0 + 0.4
            cue_end = cue_start + max(spoken, readable) * dur_mult
            if cue_end < start or cue_start > end:
                continue
            if rng.random() > rate * availability:
                continue
            text = line.text.get(emit_lang, line.text["en"])
            if drop_p > 0:
                toks = text.split()
                text = " ".join(t for t in toks if rng.random() > drop_p)
            speaker = line.speaker
            if swap_p > 0 and rng.random() < swap_p:
                others = [s for s in media.SPEAKERS if s != speaker]
                speaker = rng.choice(others)
            if dup_p > 0 and prev_text and rng.random() < dup_p:
                text = prev_text
            if flicker_p > 0 and rng.random() < flicker_p:
                cue_end = cue_start + 0.4
            cues.append(
                CaptionCue(
                    start_s=cue_start,
                    end_s=cue_end,
                    text=text,
                    speaker=speaker,
                    language=emit_lang,
                    rendered=rng.random() < render_success,
                )
            )
            prev_text = text
        return sorted(cues, key=lambda c: c.start_s)

    def _manifest_tracks(
        self, eff_cap: dict[str, Any], eff_ad: dict[str, Any], language: str
    ) -> tuple[list[str], list[str]]:
        """What the manifest currently declares for this slice.

        Losing a rendition from the audio adaptation set takes out both the
        described variant and the plain alternate-language variant for that
        language - they are entries in the same set. Restoring the track through
        the action catalog puts it back.
        """
        caption_tracks = list(LANGUAGES)
        audio_tracks = ["en", "fr", "en-desc", "fr-desc"]
        if eff_cap.get("drop_track") and language in caption_tracks:
            caption_tracks.remove(language)
        if eff_ad.get("drop_track"):
            for tag in (f"{language}-desc", language):
                if tag in audio_tracks and tag not in self.restored_audio_tracks:
                    audio_tracks.remove(tag)
        return caption_tracks, audio_tracks

    def _build_description(
        self,
        language: str,
        start: float,
        end: float,
        eff: dict[str, Any],
        rng: random.Random,
    ) -> tuple[list[DescribedWindow], str]:
        desc_language = eff.get("substitute_language", language)
        if desc_language not in ("en", "fr"):
            desc_language = "en"
        if eff.get("drop_track"):
            return [], desc_language
        silence_p = _get(eff, "silence_probability", 0.0)
        drift = _get(eff, "drift_seconds", 0.0)
        loudness = _get(eff, "loudness_lufs", TARGET_LUFS, default_is_target=True)
        channels = int(_get(eff, "channels", 2.0))
        out: list[DescribedWindow] = []
        for abs_start, scene in media.scenes_in(start - drift - 2.0, end - drift + 2.0):
            w_start = abs_start + drift
            w_end = w_start + (scene.end_s - scene.start_s)
            if w_end < start or w_start > end:
                continue
            silent = rng.random() < silence_p
            out.append(
                DescribedWindow(
                    start_s=w_start,
                    end_s=w_end,
                    target_scene_start=abs_start,
                    peak_dbfs=(-72.0 if silent else -14.0 + rng.uniform(-2, 2)),
                    loudness_lufs=(-90.0 if silent else loudness + rng.uniform(-0.8, 0.8)),
                    language=desc_language,
                    channels=channels,
                    covers_scene_index=scene.index,
                )
            )
        return out, desc_language

    def _build_sign(
        self, start: float, end: float, eff: dict[str, Any], rng: random.Random
    ) -> SignFeedStats:
        fps = _get(eff, "fps", 50.0, default_is_target=True)
        availability = _get(eff, "availability", 1.0)
        expected = int(50.0 * (end - start))
        delivered = int(expected * (fps / 50.0) * availability)
        frozen_ratio = _get(eff, "frozen_ratio", 0.001)
        black_ratio = _get(eff, "black_ratio", 0.0005)
        visible = _get(eff, "visible_ratio", 0.999, default_is_target=True)
        drift = _get(eff, "drift_seconds", 0.0)
        return SignFeedStats(
            frames_expected=expected,
            frames_delivered=max(0, delivered),
            frozen_frames=int(delivered * frozen_ratio),
            black_frames=int(delivered * black_ratio),
            fps=round(fps * availability, 2),
            interpreter_visible_ratio=round(min(1.0, visible), 4),
            sync_drift_s=round(abs(drift) + rng.uniform(0.0, 0.03), 3),
            pip_overlap_ratio=round(max(0.0, 1.0 - visible) * 0.6, 4),
        )

    def _build_player_journey(
        self,
        platform: str,
        player_version: str,
        eff_cap: dict[str, Any],
        eff_ad: dict[str, Any],
        eff_player: dict[str, Any],
        eff_auth: dict[str, Any],
        eff_purchase: dict[str, Any],
        rng: random.Random,
    ) -> PlayerJourney:
        effective_pv = self.pinned_player_versions.get(player_version, player_version)
        flag_off = any(effective_pv in f or platform in f for f in self.disabled_feature_flags)
        device = {"web": "laptop", "ctv": "smart_tv", "ios": "phone", "android": "phone"}[platform]

        def p(eff: dict[str, Any], key: str, default: float = 1.0) -> float:
            v = _get(eff, key, default, default_is_target=True)
            return 1.0 if flag_off else v

        return PlayerJourney(
            player_version=effective_pv,
            platform=platform,
            device_class=device,
            keyboard_completed=rng.random() < p(eff_player, "keyboard_completion"),
            screenreader_completed=rng.random() < p(eff_player, "screenreader_completion"),
            accessible_name_ratio=round(p(eff_player, "accessible_name_ratio"), 4),
            focus_visible_ratio=round(p(eff_player, "focus_visible_ratio"), 4),
            caption_control_ok=rng.random() < p(eff_player, "caption_control_ok"),
            audio_track_selection_ok=rng.random() < p(eff_ad, "selection_success"),
            reduced_motion_ok=rng.random() < p(eff_player, "reduced_motion_ok"),
            captions_rendered=rng.random() < p(eff_cap, "render_success"),
            auth_completed=rng.random() < p(eff_auth, "auth_completion"),
            purchase_completed=rng.random() < p(eff_purchase, "purchase_completion"),
            console_errors=(
                ["CaptionRendererError: layout out of bounds"]
                if p(eff_cap, "render_success") < 0.9
                else []
            ),
            a11y_tree_nodes=rng.randint(180, 240),
        )

    def _build_transport(
        self, eff_cap: dict[str, Any], eff_sign: dict[str, Any], rng: random.Random
    ) -> TransportStats:
        loss = _get(eff_cap, "packet_loss", 0.0004)
        return TransportStats(
            packet_loss_ratio=round(loss, 5),
            retransmit_ratio=round(loss * 2.4, 5),
            rtt_ms=round(18.0 + loss * 900 + rng.uniform(-2, 2), 2),
            edge_5xx_ratio=round(max(0.0, 1.0 - _get(eff_cap, "availability", 1.0)) * 0.35, 5),
            origin_cpu=round(0.34 + rng.uniform(-0.04, 0.04), 3),
            encoder_cpu=round(_get(eff_cap, "cpu_saturation", 0.42, default_is_target=True), 3),
            gpu_utilisation=round(
                _get(eff_sign, "gpu_saturation", 0.55, default_is_target=True), 3
            ),
        )

    def _sessions(self, territory: str, platform: str, player_version: str) -> dict[str, int]:
        base = BASE_SESSIONS.get(territory, 5000)
        share = PLATFORM_SHARE.get(platform, 0.25) * PLAYER_VERSION_SHARE.get(player_version, 1.0)
        pool = base * share
        return {
            "captions": int(pool * CAPTION_ENABLE_RATE),
            "description": int(pool * DESCRIPTION_ENABLE_RATE),
            "sign": int(pool * SIGN_ENABLE_RATE) if territory in ("FR", "CA") else 0,
        }

    # -- aggregates --------------------------------------------------------
    def session_aggregates(
        self, feature: FeatureType, language: str, k_threshold: int = 50
    ) -> list[SessionAggregate]:
        """Privacy-preserving. Small slices are suppressed, never sharpened."""
        out: list[SessionAggregate] = []
        for territory in TERRITORIES:
            for pv in PLAYER_VERSIONS:
                platform = _platform_of(pv)
                s = self._sessions(territory, platform, pv)
                key = {
                    "captions": "captions",
                    "audio_description": "description",
                    "sign_language": "sign",
                }.get(feature.value, "captions")
                count = s[key]
                obs = self.observe(language, territory, platform, pv, window_s=10.0)
                suppressed = count < k_threshold
                out.append(
                    SessionAggregate(
                        slice_key=f"{territory}/{platform}/{pv}",
                        feature=feature,
                        language=language,
                        territory=territory,
                        platform=Platform(platform),
                        player_version=pv,
                        sessions_with_feature_enabled=0 if suppressed else count,
                        selection_failures=0 if suppressed else obs.selection_failures,
                        playback_errors_after_selection=0 if suppressed else obs.playback_errors,
                        repeated_attempts=0 if suppressed else int(obs.selection_failures * 1.7),
                        support_contacts=0 if suppressed else int(obs.selection_failures * 0.06),
                        k_anonymity_threshold=k_threshold,
                        suppressed=suppressed,
                    )
                )
        return out

    def slices(self) -> Iterable[tuple[str, str, str, str]]:
        for language in LANGUAGES:
            for territory in TERRITORIES:
                for pv in PLAYER_VERSIONS:
                    yield language, territory, _platform_of(pv), pv


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_TARGET_DEFAULTS = {
    "fps": 50.0,
    "visible_ratio": 1.0,
    "loudness_lufs": TARGET_LUFS,
    "channels": 2.0,
    "cpu_saturation": 0.42,
    "gpu_saturation": 0.55,
}
# For "higher is better" parameters a fault lowers the value; for "lower is
# better" parameters a fault raises it. _scale interpolates from the healthy
# default towards the fault value as intensity rises.


def _scale(key: str, fault_value: float, intensity: float) -> float:
    healthy = {
        "drift_seconds": 0.0,
        "cue_rate_multiplier": 1.0,
        "availability": 1.0,
        "drop_probability": 0.0,
        "duplicate_probability": 0.0,
        "duration_multiplier": 1.0,
        "flicker_probability": 0.0,
        "render_success": 1.0,
        "swap_probability": 0.0,
        "silence_probability": 0.0,
        "loudness_lufs": TARGET_LUFS,
        "channels": 2.0,
        "selection_success": 1.0,
        "frozen_ratio": 0.001,
        "black_ratio": 0.0005,
        "visible_ratio": 1.0,
        "fps": 50.0,
        "keyboard_completion": 1.0,
        "screenreader_completion": 1.0,
        "accessible_name_ratio": 1.0,
        "focus_visible_ratio": 1.0,
        "caption_control_ok": 1.0,
        "reduced_motion_ok": 1.0,
        "auth_completion": 1.0,
        "purchase_completion": 1.0,
        "packet_loss": 0.0004,
        "cpu_saturation": 0.42,
        "gpu_saturation": 0.55,
    }.get(key, 0.0)
    return healthy + (fault_value - healthy) * intensity


def _combine(key: str, existing: Any, new: float) -> float:
    if existing is None:
        return new
    if not isinstance(existing, (int, float)):
        return new
    worse_is_lower = key in {
        "cue_rate_multiplier",
        "availability",
        "render_success",
        "selection_success",
        "visible_ratio",
        "fps",
        "keyboard_completion",
        "screenreader_completion",
        "accessible_name_ratio",
        "focus_visible_ratio",
        "caption_control_ok",
        "reduced_motion_ok",
        "auth_completion",
        "purchase_completion",
        "loudness_lufs",
    }
    return min(existing, new) if worse_is_lower else max(existing, new)


def _get(eff: dict[str, Any], key: str, default: float, default_is_target: bool = False) -> float:
    v = eff.get(key)
    if v is None or isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        return float(v)
    return default


# Described audio and alternate-language audio ride the same track machinery -
# the same adaptation set, the same manifest entry, the same player audio menu -
# so a fault on one is observable through the probe that measures the other.
_FEATURE_FAMILY = {
    FeatureType.AUDIO_DESCRIPTION: {FeatureType.AUDIO_DESCRIPTION, FeatureType.ALTERNATE_AUDIO},
    FeatureType.ALTERNATE_AUDIO: {FeatureType.AUDIO_DESCRIPTION, FeatureType.ALTERNATE_AUDIO},
    FeatureType.CAPTIONS: {FeatureType.CAPTIONS, FeatureType.SUBTITLES},
    FeatureType.SUBTITLES: {FeatureType.CAPTIONS, FeatureType.SUBTITLES},
}


def _feature_affected(
    fault_feature: FeatureType, observed: FeatureType, params: dict[str, Any]
) -> bool:
    if observed in _FEATURE_FAMILY.get(fault_feature, {fault_feature}):
        return True
    if params.get("applies_all_features"):
        return True
    if observed is FeatureType.SIGN_LANGUAGE and params.get("also_affects_sign"):
        return True
    return False


def _platform_of(player_version: str) -> str:
    if player_version.startswith("ctv"):
        return "ctv"
    if player_version.startswith("web"):
        return "web"
    if player_version.startswith("ios"):
        return "ios"
    return "android"
