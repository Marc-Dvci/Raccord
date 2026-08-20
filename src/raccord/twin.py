"""Accessible Experience Digital Twin.

A live, versioned graph of the delivery chain: what produces what, what depends
on what, who owns it, and which accessibility promises ride on top of it.

The twin answers the blast-radius question in one traversal: given a failing
component, which features, languages, territories, platforms, devices, player
versions, promises, owners and session aggregates are affected, and which
remediation paths remain safe.
"""

from __future__ import annotations

import itertools
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable

from .contracts import DeviceClass, FeatureType, Platform, stable_hash, utcnow

# ---------------------------------------------------------------------------
# Entity / edge vocabulary
# ---------------------------------------------------------------------------

ENTITY_KINDS = (
    "event",
    "title",
    "program_feed",
    "caption_source",
    "caption_encoder_pool",
    "audio_description_source",
    "alternate_language_source",
    "sign_language_source",
    "packager",
    "manifest",
    "origin",
    "cdn",
    "region",
    "edge_node",
    "player",
    "player_version",
    "device_class",
    "operating_system",
    "auth_service",
    "entitlement_service",
    "purchase_flow",
    "synthetic_probe",
    "session_aggregate",
    "deployment",
    "configuration",
    "promise",
    "slo",
    "clock_source",
    "provider",
    "owner",
)

EDGE_KINDS = (
    "produces",
    "encodes",
    "packages",
    "references",
    "delivers",
    "renders",
    "depends_on",
    "deployed_by",
    "configured_by",
    "monitored_by",
    "owned_by",
    "serves_territory",
    "serves_platform",
    "carries_feature",
)


@dataclass
class Node:
    node_id: str
    kind: str
    name: str
    attrs: dict[str, Any] = field(default_factory=dict)
    version: int = 1
    effective_from: datetime = field(default_factory=utcnow)
    effective_to: datetime | None = None
    healthy: bool = True


@dataclass
class Edge:
    src: str
    dst: str
    kind: str
    attrs: dict[str, Any] = field(default_factory=dict)
    effective_from: datetime = field(default_factory=utcnow)
    effective_to: datetime | None = None


@dataclass
class BlastRadius:
    origin_nodes: list[str]
    downstream_nodes: list[str]
    features: list[FeatureType]
    languages: list[str]
    territories: list[str]
    platforms: list[Platform]
    device_classes: list[DeviceClass]
    player_versions: list[str]
    cdn_regions: list[str]
    providers: list[str]
    promise_ids: list[str]
    owners: dict[str, str]
    safe_remediation_targets: list[str]
    at_risk_adjacent: list[str]


class DigitalTwin:
    """Versioned topology graph with point-in-time reads."""

    def __init__(self) -> None:
        self._nodes: dict[str, list[Node]] = defaultdict(list)
        self._edges: list[Edge] = []
        self._out: dict[str, list[Edge]] = defaultdict(list)
        self._in: dict[str, list[Edge]] = defaultdict(list)
        self._revision = 0

    # -- mutation ----------------------------------------------------------
    def upsert_node(
        self,
        node_id: str,
        kind: str,
        name: str,
        attrs: dict[str, Any] | None = None,
        at: datetime | None = None,
    ) -> Node:
        if kind not in ENTITY_KINDS:
            raise ValueError(f"unknown entity kind: {kind}")
        at = at or utcnow()
        history = self._nodes[node_id]
        version = 1
        if history:
            prev = history[-1]
            # Keep validity intervals strictly ordered even when two upserts
            # land in the same clock tick, so point-in-time reads stay unambiguous.
            at = max(at, prev.effective_from + timedelta(microseconds=1))
            if prev.effective_to is None:
                prev.effective_to = at
            version = prev.version + 1
        node = Node(
            node_id=node_id,
            kind=kind,
            name=name,
            attrs=dict(attrs or {}),
            version=version,
            effective_from=at,
        )
        history.append(node)
        self._revision += 1
        return node

    def add_edge(
        self,
        src: str,
        dst: str,
        kind: str,
        attrs: dict[str, Any] | None = None,
        at: datetime | None = None,
    ) -> Edge:
        if kind not in EDGE_KINDS:
            raise ValueError(f"unknown edge kind: {kind}")
        edge = Edge(
            src=src, dst=dst, kind=kind, attrs=dict(attrs or {}), effective_from=at or utcnow()
        )
        self._edges.append(edge)
        self._out[src].append(edge)
        self._in[dst].append(edge)
        self._revision += 1
        return edge

    def retire_edge(self, src: str, dst: str, kind: str, at: datetime | None = None) -> None:
        at = at or utcnow()
        for e in self._out[src]:
            if e.dst == dst and e.kind == kind and e.effective_to is None:
                e.effective_to = at
        self._revision += 1

    def set_health(self, node_id: str, healthy: bool) -> None:
        node = self.node(node_id)
        if node:
            node.healthy = healthy

    # -- reads -------------------------------------------------------------
    @property
    def revision(self) -> int:
        return self._revision

    def node(self, node_id: str, at: datetime | None = None) -> Node | None:
        history = self._nodes.get(node_id)
        if not history:
            return None
        if at is None:
            return history[-1]
        for n in reversed(history):
            if n.effective_from <= at and (n.effective_to is None or n.effective_to > at):
                return n
        return None

    def nodes(self, kind: str | None = None, at: datetime | None = None) -> list[Node]:
        out = []
        for node_id in self._nodes:
            n = self.node(node_id, at)
            if n and (kind is None or n.kind == kind):
                out.append(n)
        return out

    def edges(self, at: datetime | None = None) -> list[Edge]:
        if at is None:
            return [e for e in self._edges if e.effective_to is None]
        return [
            e
            for e in self._edges
            if e.effective_from <= at and (e.effective_to is None or e.effective_to > at)
        ]

    def out_edges(self, node_id: str, at: datetime | None = None) -> list[Edge]:
        live = self._out.get(node_id, [])
        if at is None:
            return [e for e in live if e.effective_to is None]
        return [
            e
            for e in live
            if e.effective_from <= at and (e.effective_to is None or e.effective_to > at)
        ]

    def in_edges(self, node_id: str, at: datetime | None = None) -> list[Edge]:
        live = self._in.get(node_id, [])
        if at is None:
            return [e for e in live if e.effective_to is None]
        return [
            e
            for e in live
            if e.effective_from <= at and (e.effective_to is None or e.effective_to > at)
        ]

    # -- traversal ---------------------------------------------------------
    def downstream(self, node_ids: Iterable[str], at: datetime | None = None) -> list[str]:
        """Everything reachable by delivery/render/depends edges."""
        seen: set[str] = set()
        queue = deque(node_ids)
        while queue:
            cur = queue.popleft()
            for e in self.out_edges(cur, at):
                if e.dst not in seen:
                    seen.add(e.dst)
                    queue.append(e.dst)
        return sorted(seen)

    def upstream(self, node_ids: Iterable[str], at: datetime | None = None) -> list[str]:
        seen: set[str] = set()
        queue = deque(node_ids)
        while queue:
            cur = queue.popleft()
            for e in self.in_edges(cur, at):
                if e.src not in seen:
                    seen.add(e.src)
                    queue.append(e.src)
        return sorted(seen)

    # -- blast radius ------------------------------------------------------
    def blast_radius(
        self,
        origin_nodes: list[str],
        at: datetime | None = None,
    ) -> BlastRadius:
        down = self.downstream(origin_nodes, at)
        touched = sorted(set(origin_nodes) | set(down))

        features: set[FeatureType] = set()
        languages: set[str] = set()
        territories: set[str] = set()
        platforms: set[Platform] = set()
        devices: set[DeviceClass] = set()
        player_versions: set[str] = set()
        cdn_regions: set[str] = set()
        providers: set[str] = set()
        promises: set[str] = set()
        owners: dict[str, str] = {}

        for nid in touched:
            n = self.node(nid, at)
            if not n:
                continue
            a = n.attrs
            if "feature" in a:
                try:
                    features.add(FeatureType(a["feature"]))
                except ValueError:
                    pass
            if "language" in a:
                languages.add(str(a["language"]).lower())
            for t in a.get("territories", []) or []:
                territories.add(t)
            if "territory" in a:
                territories.add(a["territory"])
            if "platform" in a:
                try:
                    platforms.add(Platform(a["platform"]))
                except ValueError:
                    pass
            for d in a.get("device_classes", []) or []:
                try:
                    devices.add(DeviceClass(d))
                except ValueError:
                    pass
            if n.kind == "player_version":
                player_versions.add(n.name)
            if n.kind == "region":
                cdn_regions.add(n.name)
            if "provider" in a:
                providers.add(a["provider"])
            if n.kind == "promise":
                promises.add(nid)
            for e in self.out_edges(nid, at):
                if e.kind == "owned_by":
                    owners[nid] = e.dst

        # Promises whose carrying path intersects the blast radius
        for p in self.nodes("promise", at):
            deps = set(self.upstream([p.node_id], at))
            if deps & set(touched):
                promises.add(p.node_id)
                for e in self.out_edges(p.node_id, at):
                    if e.kind == "owned_by":
                        owners[p.node_id] = e.dst

        # Safe remediation targets: healthy siblings of failing components
        safe: set[str] = set()
        at_risk: set[str] = set()
        for nid in origin_nodes:
            n = self.node(nid, at)
            if not n:
                continue
            for sibling in self.nodes(n.kind, at):
                if sibling.node_id == nid:
                    continue
                if sibling.healthy and sibling.attrs.get("standby_for") in (None, nid):
                    safe.add(sibling.node_id)
            for e in self.in_edges(nid, at):
                for peer in self.out_edges(e.src, at):
                    if peer.dst != nid:
                        at_risk.add(peer.dst)

        return BlastRadius(
            origin_nodes=list(origin_nodes),
            downstream_nodes=down,
            features=sorted(features, key=lambda f: f.value),
            languages=sorted(languages),
            territories=sorted(territories),
            platforms=sorted(platforms, key=lambda p: p.value),
            device_classes=sorted(devices, key=lambda d: d.value),
            player_versions=sorted(player_versions),
            cdn_regions=sorted(cdn_regions),
            providers=sorted(providers),
            promise_ids=sorted(promises),
            owners=owners,
            safe_remediation_targets=sorted(safe),
            at_risk_adjacent=sorted(at_risk - set(touched)),
        )

    # -- integrity ---------------------------------------------------------
    def topology_hash(self, at: datetime | None = None) -> str:
        nodes = [(n.node_id, n.kind, n.version) for n in self.nodes(at=at)]
        edges = [(e.src, e.dst, e.kind) for e in self.edges(at)]
        return stable_hash({"nodes": sorted(nodes), "edges": sorted(edges)})

    def to_dict(self, at: datetime | None = None) -> dict[str, Any]:
        return {
            "revision": self._revision,
            "topology_hash": self.topology_hash(at),
            "nodes": [
                {
                    "id": n.node_id,
                    "kind": n.kind,
                    "name": n.name,
                    "version": n.version,
                    "healthy": n.healthy,
                    "attrs": n.attrs,
                }
                for n in self.nodes(at=at)
            ],
            "edges": [
                {"src": e.src, "dst": e.dst, "kind": e.kind, "attrs": e.attrs}
                for e in self.edges(at)
            ],
        }


# ---------------------------------------------------------------------------
# Reference topology for the festival premiere demonstration
# ---------------------------------------------------------------------------

TERRITORIES = ["FR", "DE", "ES", "GB", "US", "CA", "BR", "JP"]
WEST_EUROPE = ["FR", "DE", "ES", "GB"]
LANGUAGES = ["en", "fr", "de", "es"]
CDN_REGIONS = ["eu-west", "eu-central", "us-east", "us-west", "sa-east", "ap-northeast"]
PLAYER_VERSIONS = ["web-4.12.0", "ctv-9.3.1", "ctv-9.4.0", "ios-6.2.0", "android-6.2.0"]


def build_reference_twin(event_id: str = "evt-lumiere-premiere") -> DigitalTwin:
    """The instrumented festival-premiere delivery chain used by the demo.

    Original, fully authorised fictional programme: 'The Lumière Protocol',
    a film-festival premiere with EN/FR/DE/ES captions, EN+FR audio description,
    FR alternate audio and an LSF sign-language feed.
    """
    t = DigitalTwin()

    t.upsert_node(
        event_id,
        "event",
        "The Lumiere Protocol - World Premiere",
        {"tier": "tier0_global_live", "territories": TERRITORIES},
    )
    t.upsert_node(
        "title-lumiere",
        "title",
        "The Lumiere Protocol",
        {"runtime_s": 5820, "rights": "original_authorised"},
    )
    t.add_edge(event_id, "title-lumiere", "references")

    t.upsert_node(
        "feed-program", "program_feed", "Program feed (1080p50)", {"provider": "studio-inhouse"}
    )
    t.add_edge(event_id, "feed-program", "produces")

    # Clock sources -------------------------------------------------------
    t.upsert_node(
        "clock-ptp-primary",
        "clock_source",
        "PTP grandmaster (primary)",
        {"provider": "studio-inhouse", "offset_ms": 0},
    )
    t.upsert_node(
        "clock-ntp-fallback",
        "clock_source",
        "NTP pool (fallback)",
        {"provider": "public-ntp", "offset_ms": 0, "standby_for": "clock-ptp-primary"},
    )

    # Caption chain -------------------------------------------------------
    for lang in LANGUAGES:
        src = f"capsrc-{lang}"
        t.upsert_node(
            src,
            "caption_source",
            f"Caption source [{lang}]",
            {"feature": "captions", "language": lang, "provider": "verbaflow"},
        )
        t.add_edge("feed-program", src, "produces")

    t.upsert_node(
        "capenc-pool-a",
        "caption_encoder_pool",
        "Caption encoder pool A",
        {
            "feature": "captions",
            "provider": "verbaflow",
            "region": "eu-west",
            "nodes": 4,
            "clock": "clock-ptp-primary",
        },
    )
    t.upsert_node(
        "capenc-pool-b",
        "caption_encoder_pool",
        "Caption encoder pool B (standby)",
        {
            "feature": "captions",
            "provider": "verbaflow",
            "region": "eu-central",
            "nodes": 4,
            "clock": "clock-ptp-primary",
            "standby_for": "capenc-pool-a",
        },
    )
    for lang in LANGUAGES:
        t.add_edge(f"capsrc-{lang}", "capenc-pool-a", "encodes", {"language": lang})
    t.add_edge("clock-ptp-primary", "capenc-pool-a", "depends_on")
    t.add_edge("clock-ptp-primary", "capenc-pool-b", "depends_on")

    # Audio description / alternate audio / sign -------------------------
    for lang in ("en", "fr"):
        nid = f"adsrc-{lang}"
        t.upsert_node(
            nid,
            "audio_description_source",
            f"Audio description [{lang}]",
            {"feature": "audio_description", "language": lang, "provider": "describa"},
        )
        t.add_edge("feed-program", nid, "produces")
    t.upsert_node(
        "altsrc-fr",
        "alternate_language_source",
        "Alternate audio [fr]",
        {"feature": "alternate_audio", "language": "fr", "provider": "studio-inhouse"},
    )
    t.add_edge("feed-program", "altsrc-fr", "produces")
    t.upsert_node(
        "signsrc-lsf",
        "sign_language_source",
        "Sign language feed (LSF)",
        {"feature": "sign_language", "language": "fr-lsf", "provider": "signcast", "fps": 50},
    )
    t.add_edge("feed-program", "signsrc-lsf", "produces")

    # Packaging / manifest / origin --------------------------------------
    t.upsert_node("packager-main", "packager", "HLS+DASH packager", {"provider": "studio-inhouse"})
    for nid in (
        "capenc-pool-a",
        "adsrc-en",
        "adsrc-fr",
        "altsrc-fr",
        "signsrc-lsf",
        "feed-program",
    ):
        t.add_edge(nid, "packager-main", "packages")
    t.upsert_node("manifest-main", "manifest", "Master manifest", {"tracks": len(LANGUAGES) + 4})
    t.add_edge("packager-main", "manifest-main", "produces")
    t.upsert_node("origin-main", "origin", "Origin shield", {"provider": "studio-inhouse"})
    t.add_edge("manifest-main", "origin-main", "delivers")

    # CDN + regions -------------------------------------------------------
    t.upsert_node("cdn-primary", "cdn", "Primary CDN", {"provider": "swiftedge"})
    t.add_edge("origin-main", "cdn-primary", "delivers")
    for region in CDN_REGIONS:
        t.upsert_node(
            f"region-{region}",
            "region",
            region,
            {"provider": "swiftedge", "territories": _region_territories(region)},
        )
        t.add_edge("cdn-primary", f"region-{region}", "delivers")

    # Players -------------------------------------------------------------
    for pv in PLAYER_VERSIONS:
        platform = (
            "ctv"
            if pv.startswith("ctv")
            else "web"
            if pv.startswith("web")
            else "ios"
            if pv.startswith("ios")
            else "android"
        )
        devices = {
            "ctv": ["smart_tv", "streaming_stick", "console"],
            "web": ["desktop", "laptop"],
            "ios": ["phone", "tablet"],
            "android": ["phone", "tablet"],
        }[platform]
        t.upsert_node(
            f"pv-{pv}",
            "player_version",
            pv,
            {"platform": platform, "device_classes": devices, "provider": "studio-inhouse"},
        )
        for region in CDN_REGIONS:
            t.add_edge(f"region-{region}", f"pv-{pv}", "renders")

    # Access flows --------------------------------------------------------
    t.upsert_node(
        "auth-svc",
        "auth_service",
        "Accessible authentication",
        {"feature": "accessible_auth", "provider": "studio-inhouse"},
    )
    t.upsert_node(
        "entitlement-svc",
        "entitlement_service",
        "Entitlement service",
        {"provider": "studio-inhouse"},
    )
    t.upsert_node(
        "purchase-flow",
        "purchase_flow",
        "Festival ticket purchase",
        {"feature": "accessible_purchase", "provider": "studio-inhouse"},
    )
    t.add_edge("auth-svc", "entitlement-svc", "depends_on")
    t.add_edge("entitlement-svc", "purchase-flow", "depends_on")
    for pv in PLAYER_VERSIONS:
        t.add_edge("auth-svc", f"pv-{pv}", "depends_on")

    # Owners --------------------------------------------------------------
    for owner in (
        "owner-a11y-ops",
        "owner-streaming-sre",
        "owner-broadcast-ops",
        "owner-technical-director",
    ):
        t.upsert_node(owner, "owner", owner.replace("owner-", "").replace("-", " "))
    t.add_edge("capenc-pool-a", "owner-broadcast-ops", "owned_by")
    t.add_edge("capenc-pool-b", "owner-broadcast-ops", "owned_by")
    t.add_edge("clock-ptp-primary", "owner-streaming-sre", "owned_by")
    t.add_edge("signsrc-lsf", "owner-a11y-ops", "owned_by")
    t.add_edge("cdn-primary", "owner-streaming-sre", "owned_by")
    t.add_edge(event_id, "owner-technical-director", "owned_by")

    return t


def attach_promises(twin: DigitalTwin, promises: Iterable[Any]) -> None:
    """Bind registered promises into the topology.

    Each promise becomes a node whose upstream closure is its delivery path, so
    a blast-radius traversal from any failing component immediately yields the
    operational contracts that component is currently carrying.
    """
    for p in promises:
        nid = f"promise-{p.promise_id}"
        twin.upsert_node(
            nid,
            "promise",
            p.promise_id,
            {
                "feature": p.feature.value,
                "language": p.language,
                "territories": list(p.territories),
                "slo_tier": p.slo_tier.value,
                "provider": p.provider,
                "version": p.version,
            },
        )
        for component in str(p.delivery_path).split("->"):
            component = component.strip()
            if component == "capsrc":
                component = f"capsrc-{p.language}"
            if component == "adsrc":
                component = f"adsrc-{p.language}"
            if component == "player":
                for pv in PLAYER_VERSIONS:
                    twin.add_edge(f"pv-{pv}", nid, "carries_feature")
                continue
            if twin.node(component):
                twin.add_edge(component, nid, "carries_feature")
        owner = "a11y-ops" if p.feature.value.startswith("accessible") else "broadcast-ops"
        twin.add_edge(nid, f"owner-{owner}", "owned_by")


def _region_territories(region: str) -> list[str]:
    return {
        "eu-west": ["FR", "GB", "ES"],
        "eu-central": ["DE"],
        "us-east": ["US", "CA"],
        "us-west": ["US"],
        "sa-east": ["BR"],
        "ap-northeast": ["JP"],
    }.get(region, [])


def territory_regions(territory: str) -> list[str]:
    return [r for r in CDN_REGIONS if territory in _region_territories(r)]


def all_slices() -> list[tuple[str, str, str]]:
    """(language, territory, player_version) slices used for session aggregates."""
    return list(itertools.product(LANGUAGES, TERRITORIES, PLAYER_VERSIONS))
