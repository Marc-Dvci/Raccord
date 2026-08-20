"""Accessibility Promise Registry.

Every accessibility experience an event promises is registered as a versioned
operational contract *before* monitoring begins. A promise cannot be silently
changed once the event has started: amendments create a new version, the old
version stays readable, and incident reasoning always resolves the version that
was effective at the incident timestamp.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .contracts import (
    AccessibilityPromise,
    DeviceClass,
    FeatureType,
    Platform,
    SLOTier,
    utcnow,
)
from .twin import PLAYER_VERSIONS, TERRITORIES

_SCHEMA = """
CREATE TABLE IF NOT EXISTS promises (
    promise_id   TEXT NOT NULL,
    version      INTEGER NOT NULL,
    event_id     TEXT NOT NULL,
    feature      TEXT NOT NULL,
    language     TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to   TEXT,
    content_hash TEXT NOT NULL,
    body         TEXT NOT NULL,
    PRIMARY KEY (promise_id, version)
);
CREATE INDEX IF NOT EXISTS idx_promise_event ON promises(event_id);
CREATE TABLE IF NOT EXISTS amendments (
    promise_id TEXT NOT NULL,
    version    INTEGER NOT NULL,
    at         TEXT NOT NULL,
    actor      TEXT NOT NULL,
    reason     TEXT NOT NULL
);
"""


class PromiseRegistry:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- writes ------------------------------------------------------------
    def register(self, promise: AccessibilityPromise) -> AccessibilityPromise:
        cur = self._conn.execute(
            "SELECT MAX(version) AS v FROM promises WHERE promise_id = ?",
            (promise.promise_id,),
        )
        row = cur.fetchone()
        if row and row["v"] is not None:
            raise ValueError(
                f"{promise.promise_id} already registered; use amend() to create a new version"
            )
        self._insert(promise)
        return promise

    def amend(self, promise_id: str, actor: str, reason: str, **changes) -> AccessibilityPromise:
        current = self.current(promise_id)
        if current is None:
            raise KeyError(promise_id)
        # Versions must be strictly ordered in time. Two amendments inside one
        # clock tick would otherwise produce overlapping validity intervals and
        # make as_of() ambiguous - which is exactly the read incident reasoning
        # depends on.
        at = max(utcnow(), current.effective_from + timedelta(microseconds=1))
        self._conn.execute(
            "UPDATE promises SET effective_to = ? WHERE promise_id = ? AND version = ?",
            (at.isoformat(), promise_id, current.version),
        )
        new = current.model_copy(
            update={
                **changes,
                "version": current.version + 1,
                "effective_from": at,
                "effective_to": None,
            }
        )
        self._insert(new)
        self._conn.execute(
            "INSERT INTO amendments VALUES (?,?,?,?,?)",
            (promise_id, new.version, at.isoformat(), actor, reason),
        )
        self._conn.commit()
        return new

    def _insert(self, p: AccessibilityPromise) -> None:
        self._conn.execute(
            "INSERT INTO promises VALUES (?,?,?,?,?,?,?,?,?)",
            (
                p.promise_id,
                p.version,
                p.event_id,
                p.feature.value,
                p.language,
                p.effective_from.isoformat(),
                p.effective_to.isoformat() if p.effective_to else None,
                p.content_hash(),
                p.model_dump_json(),
            ),
        )
        self._conn.commit()

    # -- reads -------------------------------------------------------------
    def current(self, promise_id: str) -> AccessibilityPromise | None:
        row = self._conn.execute(
            "SELECT body FROM promises WHERE promise_id = ? AND effective_to IS NULL"
            " ORDER BY version DESC LIMIT 1",
            (promise_id,),
        ).fetchone()
        return AccessibilityPromise.model_validate_json(row["body"]) if row else None

    def as_of(self, promise_id: str, at: datetime) -> AccessibilityPromise | None:
        """Point-in-time read. Incident reasoning must use this, never current()."""
        rows = self._conn.execute(
            "SELECT body, effective_from, effective_to FROM promises WHERE promise_id = ?"
            " ORDER BY version DESC",
            (promise_id,),
        ).fetchall()
        for r in rows:
            start = datetime.fromisoformat(r["effective_from"])
            end = datetime.fromisoformat(r["effective_to"]) if r["effective_to"] else None
            if start <= at and (end is None or end > at):
                return AccessibilityPromise.model_validate_json(r["body"])
        return None

    def for_event(self, event_id: str, at: datetime | None = None) -> list[AccessibilityPromise]:
        rows = self._conn.execute(
            "SELECT promise_id FROM promises WHERE event_id = ? GROUP BY promise_id",
            (event_id,),
        ).fetchall()
        out = []
        for r in rows:
            p = self.as_of(r["promise_id"], at) if at else self.current(r["promise_id"])
            if p:
                out.append(p)
        return sorted(out, key=lambda p: (p.feature.value, p.language))

    def matching(
        self,
        event_id: str,
        feature: FeatureType | None = None,
        language: str | None = None,
        territory: str | None = None,
        platform: Platform | None = None,
        player_version: str | None = None,
        at: datetime | None = None,
    ) -> list[AccessibilityPromise]:
        out = []
        for p in self.for_event(event_id, at):
            if feature and p.feature != feature:
                continue
            if language and p.language != language.lower():
                continue
            if territory and territory not in p.territories:
                continue
            if platform and platform not in p.platforms:
                continue
            if player_version and p.player_versions and player_version not in p.player_versions:
                continue
            out.append(p)
        return out

    def history(self, promise_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT version, effective_from, effective_to, content_hash FROM promises"
            " WHERE promise_id = ? ORDER BY version",
            (promise_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def count(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(DISTINCT promise_id) AS c FROM promises"
        ).fetchone()["c"]

    def reset(self) -> None:
        self._conn.executescript("DELETE FROM promises; DELETE FROM amendments;")
        self._conn.commit()


# ---------------------------------------------------------------------------
# Seed: the promises made by the festival premiere
# ---------------------------------------------------------------------------

CTV_VERSIONS = [v for v in PLAYER_VERSIONS if v.startswith("ctv")]
ALL_PLATFORMS = (Platform.WEB, Platform.CTV, Platform.MOBILE_IOS, Platform.MOBILE_ANDROID)
ALL_DEVICES = (
    DeviceClass.DESKTOP,
    DeviceClass.LAPTOP,
    DeviceClass.PHONE,
    DeviceClass.TABLET,
    DeviceClass.SMART_TV,
    DeviceClass.STREAMING_STICK,
)


def seed_promises(
    registry: PromiseRegistry,
    event_id: str = "evt-lumiere-premiere",
    start: datetime | None = None,
) -> list[AccessibilityPromise]:
    start = start or utcnow().replace(microsecond=0)
    end = start + timedelta(hours=3)
    made: list[AccessibilityPromise] = []

    def add(**kw) -> None:
        p = AccessibilityPromise(
            event_id=event_id,
            planned_start=start,
            planned_end=end,
            slo_tier=SLOTier.TIER_0_GLOBAL_LIVE,
            business_owner="owner-technical-director",
            technical_owner="owner-streaming-sre",
            escalation_owner="owner-a11y-ops",
            **kw,
        )
        registry.register(p)
        made.append(p)

    # Captions: 4 languages, every platform, global
    for lang in ("en", "fr", "de", "es"):
        add(
            promise_id=f"pr-captions-{lang}",
            feature=FeatureType.CAPTIONS,
            language=lang,
            territories=tuple(TERRITORIES),
            platforms=ALL_PLATFORMS,
            device_classes=ALL_DEVICES,
            player_versions=tuple(PLAYER_VERSIONS),
            delivery_path="capsrc->capenc-pool-a->packager-main->cdn-primary",
            provider="verbaflow",
            max_latency_ms=1500,
            max_sync_drift_ms=1500,
            min_availability=0.999,
            required_behaviour=(
                "Captions render within 1.5 s of spoken dialogue, are selectable from the "
                "player caption menu, and are operable with keyboard and screen reader."
            ),
            approved_fallback="capenc-pool-b",
            remediation_policy="live_event_standby_switch",
        )

    # Audio description: EN + FR
    for lang in ("en", "fr"):
        add(
            promise_id=f"pr-ad-{lang}",
            feature=FeatureType.AUDIO_DESCRIPTION,
            language=lang,
            territories=tuple(TERRITORIES),
            platforms=ALL_PLATFORMS,
            device_classes=ALL_DEVICES,
            player_versions=tuple(PLAYER_VERSIONS),
            delivery_path="adsrc->packager-main->cdn-primary",
            provider="describa",
            max_latency_ms=1000,
            max_sync_drift_ms=800,
            min_availability=0.995,
            required_behaviour=(
                "A described audio track is declared in the manifest, contains audible "
                "description in gaps between dialogue, and is selectable on every platform."
            ),
        )

    # Alternate audio FR
    add(
        promise_id="pr-altaudio-fr",
        feature=FeatureType.ALTERNATE_AUDIO,
        language="fr",
        territories=("FR", "CA", "BR"),
        platforms=ALL_PLATFORMS,
        device_classes=ALL_DEVICES,
        player_versions=tuple(PLAYER_VERSIONS),
        delivery_path="altsrc-fr->packager-main->cdn-primary",
        provider="studio-inhouse",
        min_availability=0.995,
        required_behaviour="French audio track selectable and synchronised with picture.",
    )

    # Sign language (LSF) - francophone territories, CTV + web
    add(
        promise_id="pr-sign-lsf",
        feature=FeatureType.SIGN_LANGUAGE,
        language="fr-lsf",
        territories=("FR", "CA"),
        platforms=(Platform.WEB, Platform.CTV),
        device_classes=(DeviceClass.DESKTOP, DeviceClass.LAPTOP, DeviceClass.SMART_TV),
        player_versions=tuple(v for v in PLAYER_VERSIONS if not v.startswith(("ios", "android"))),
        delivery_path="signsrc-lsf->packager-main->cdn-primary",
        provider="signcast",
        max_sync_drift_ms=500,
        min_availability=0.99,
        required_behaviour=(
            "Continuous 50 fps interpreter feed, interpreter fully visible, no overlap "
            "with burned-in text, picture-in-picture repositionable."
        ),
    )

    # Player, auth and purchase flows
    add(
        promise_id="pr-player-a11y",
        feature=FeatureType.ACCESSIBLE_PLAYER,
        language="en",
        territories=tuple(TERRITORIES),
        platforms=ALL_PLATFORMS,
        device_classes=ALL_DEVICES,
        player_versions=tuple(PLAYER_VERSIONS),
        delivery_path="player",
        provider="studio-inhouse",
        min_availability=0.999,
        required_behaviour=(
            "Every playback control reachable by keyboard, exposed with an accessible "
            "name, visible focus, no keyboard trap, reduced-motion respected."
        ),
    )
    add(
        promise_id="pr-auth-a11y",
        feature=FeatureType.ACCESSIBLE_AUTH,
        language="en",
        territories=tuple(TERRITORIES),
        platforms=ALL_PLATFORMS,
        device_classes=ALL_DEVICES,
        player_versions=tuple(PLAYER_VERSIONS),
        delivery_path="auth-svc",
        provider="studio-inhouse",
        min_availability=0.999,
        required_behaviour="Sign-in completable with screen reader and keyboard only.",
    )
    add(
        promise_id="pr-purchase-a11y",
        feature=FeatureType.ACCESSIBLE_PURCHASE,
        language="en",
        territories=tuple(TERRITORIES),
        platforms=ALL_PLATFORMS,
        device_classes=ALL_DEVICES,
        player_versions=tuple(PLAYER_VERSIONS),
        delivery_path="purchase-flow",
        provider="studio-inhouse",
        min_availability=0.995,
        required_behaviour="Ticket purchase completable with screen reader and keyboard only.",
    )
    return made
