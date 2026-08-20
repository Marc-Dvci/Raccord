"""Telemetry plane: metrics, logs, traces and profiles.

Everything Raccord learns about the delivery chain becomes real telemetry:

* Prometheus series for every probe finding, SLO evaluation, session aggregate,
  agent step, MCP call and remediation action, exposed on /metrics for the local
  Prometheus to scrape;
* structured log lines from the simulated components (encoder pools, packager,
  CDN, players, clock daemon), pushed to Loki;
* spans for the media path and for the agent's own reasoning, exported to Tempo
  over OTLP;
* CPU/alloc profile summaries for the probe fleet, for Pyroscope.

Each store also answers queries in-process, which is what the stub MCP server
uses when the docker stack is not running. That keeps the demo credential-free
while the code path that talks to a real Grafana stays identical.
"""

from __future__ import annotations

import base64
import hashlib
import random
import threading
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable

import httpx

from .config import get_settings
from .contracts import utcnow

Labels = dict[str, str]


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@dataclass
class Sample:
    ts: datetime
    value: float


class MetricStore:
    """A small time-series store with a PromQL-shaped query surface."""

    def __init__(self, retention: int = 4000) -> None:
        self._series: dict[tuple[str, tuple[tuple[str, str], ...]], deque[Sample]] = defaultdict(
            lambda: deque(maxlen=retention)
        )
        self._lock = threading.Lock()

    def record(
        self, name: str, value: float, labels: Labels | None = None, ts: datetime | None = None
    ) -> None:
        key = (name, tuple(sorted((labels or {}).items())))
        with self._lock:
            self._series[key].append(Sample(ts or utcnow(), float(value)))

    def record_many(
        self, metrics: dict[str, float], labels: Labels | None = None, ts: datetime | None = None
    ) -> None:
        for name, value in metrics.items():
            self.record(name, value, labels, ts)

    def series_names(self) -> list[str]:
        with self._lock:
            return sorted({name for name, _ in self._series})

    def label_names(self) -> list[str]:
        with self._lock:
            return sorted({k for _, lbls in self._series for k, _ in lbls})

    def label_values(self, label: str) -> list[str]:
        with self._lock:
            return sorted({v for _, lbls in self._series for k, v in lbls if k == label})

    def query(
        self,
        metric: str,
        matchers: Labels | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        aggregation: str = "last",
    ) -> list[dict[str, Any]]:
        """Subset of PromQL semantics: instant/range selection plus an aggregation.

        Supported aggregations: last, max, min, avg, sum, count.
        """
        matchers = matchers or {}
        out: list[dict[str, Any]] = []
        with self._lock:
            items = list(self._series.items())
        for (name, lbls), samples in items:
            if name != metric:
                continue
            labels = dict(lbls)
            if any(labels.get(k) != v for k, v in matchers.items()):
                continue
            window = [
                s
                for s in samples
                if (start is None or s.ts >= start) and (end is None or s.ts <= end)
            ]
            if not window:
                continue
            values = [s.value for s in window]
            agg = {
                "last": values[-1],
                "max": max(values),
                "min": min(values),
                "avg": sum(values) / len(values),
                "sum": sum(values),
                "count": float(len(values)),
            }[aggregation]
            out.append(
                {
                    "metric": {"__name__": name, **labels},
                    "value": round(agg, 6),
                    "samples": [[s.ts.isoformat(), round(s.value, 6)] for s in window[-60:]],
                    "sample_count": len(window),
                }
            )
        out.sort(key=lambda r: -abs(r["value"]))
        return out

    def snapshot_prometheus(self) -> str:
        """Render the store in Prometheus text exposition format."""
        lines: list[str] = []
        with self._lock:
            items = list(self._series.items())
        by_name: dict[str, list[tuple[Labels, float]]] = defaultdict(list)
        for (name, lbls), samples in items:
            if samples:
                by_name[name].append((dict(lbls), samples[-1].value))
        for name in sorted(by_name):
            lines.append(f"# TYPE {name} gauge")
            for labels, value in by_name[name]:
                if labels:
                    rendered = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(labels.items()))
                    lines.append(f"{name}{{{rendered}}} {value}")
                else:
                    lines.append(f"{name} {value}")
        return "\n".join(lines) + "\n"

    def latest(self) -> list[tuple[str, Labels, Sample]]:
        """Return the latest point of every label set for OTLP gauge export."""
        with self._lock:
            return [
                (name, dict(labels), samples[-1])
                for (name, labels), samples in self._series.items()
                if samples
            ]

    def clear(self) -> None:
        with self._lock:
            self._series.clear()


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _compile_matcher(value: str):
    """Return a predicate for a Loki-style label matcher value."""
    import re as _re

    if value.startswith("~"):
        pattern = _re.compile(value[1:].strip('"') + r"\Z")
        return lambda observed: bool(pattern.match(observed))
    return lambda observed: observed == value


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------


@dataclass
class LogLine:
    ts: datetime
    labels: Labels
    line: str

    def to_loki(self) -> tuple[str, str]:
        return (str(int(self.ts.timestamp() * 1e9)), self.line)


class LogStore:
    def __init__(self, retention: int = 20000) -> None:
        self._lines: deque[LogLine] = deque(maxlen=retention)
        self._lock = threading.Lock()

    def append(self, line: str, labels: Labels, ts: datetime | None = None) -> None:
        with self._lock:
            self._lines.append(LogLine(ts or utcnow(), dict(labels), line))

    def query(
        self,
        selector: dict[str, str] | None = None,
        contains: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
    ) -> list[LogLine]:
        """LogQL-shaped: stream selector plus an optional line filter.

        Selector values may be exact (`service="capenc-pool-a"`) or regular
        expressions (`service=~"clock-.*"`), matching Loki's matcher semantics.
        """
        selector = selector or {}
        matchers = [(k, _compile_matcher(v)) for k, v in selector.items()]
        with self._lock:
            candidates = list(self._lines)
        out = []
        for entry in candidates:
            if any(not match(entry.labels.get(k, "")) for k, match in matchers):
                continue
            if start and entry.ts < start:
                continue
            if end and entry.ts > end:
                continue
            if contains and contains.lower() not in entry.line.lower():
                continue
            out.append(entry)
        return out[-limit:]

    def since(self, ts: datetime) -> list[LogLine]:
        """Lines appended after `ts`, for incremental export to Loki."""
        with self._lock:
            return [e for e in self._lines if e.ts > ts]

    def label_names(self) -> list[str]:
        with self._lock:
            return sorted({k for e in self._lines for k in e.labels})

    def label_values(self, label: str) -> list[str]:
        with self._lock:
            return sorted({e.labels[label] for e in self._lines if label in e.labels})

    def stats(self, selector: dict[str, str] | None = None) -> dict[str, int]:
        lines = self.query(selector, limit=100000)
        return {
            "streams": len({tuple(sorted(entry.labels.items())) for entry in lines}),
            "entries": len(lines),
            "bytes": sum(len(entry.line) for entry in lines),
        }

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()


# ---------------------------------------------------------------------------
# Traces
# ---------------------------------------------------------------------------


@dataclass
class Span:
    span_id: str
    trace_id: str
    parent_id: str | None
    name: str
    service: str
    start: datetime
    duration_ms: float
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"


class TraceStore:
    def __init__(self, retention: int = 4000) -> None:
        self._spans: deque[Span] = deque(maxlen=retention)
        self._lock = threading.Lock()

    def add(self, span: Span) -> None:
        with self._lock:
            self._spans.append(span)

    def new_trace(self) -> str:
        return uuid.uuid4().hex

    def since(self, ts: datetime) -> list[Span]:
        """Spans started after `ts`, for incremental export to Tempo."""
        with self._lock:
            return [s for s in self._spans if s.start > ts]

    def record(
        self,
        name: str,
        service: str,
        duration_ms: float,
        trace_id: str | None = None,
        parent_id: str | None = None,
        attributes: dict[str, Any] | None = None,
        status: str = "ok",
        start: datetime | None = None,
    ) -> Span:
        span = Span(
            span_id=uuid.uuid4().hex[:16],
            trace_id=trace_id or self.new_trace(),
            parent_id=parent_id,
            name=name,
            service=service,
            start=start or utcnow(),
            duration_ms=duration_ms,
            attributes=attributes or {},
            status=status,
        )
        self.add(span)
        return span

    def search(
        self,
        service: str | None = None,
        name_contains: str | None = None,
        attributes: dict[str, str] | None = None,
        min_duration_ms: float | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 20,
    ) -> list[Span]:
        with self._lock:
            candidates = list(self._spans)
        out = []
        for s in candidates:
            if service and s.service != service:
                continue
            if name_contains and name_contains.lower() not in s.name.lower():
                continue
            if min_duration_ms and s.duration_ms < min_duration_ms:
                continue
            if start and s.start < start:
                continue
            if end and s.start > end:
                continue
            if attributes and any(str(s.attributes.get(k)) != v for k, v in attributes.items()):
                continue
            out.append(s)
        return out[-limit:]

    def trace(self, trace_id: str) -> list[Span]:
        with self._lock:
            return [s for s in self._spans if s.trace_id == trace_id]

    def clear(self) -> None:
        with self._lock:
            self._spans.clear()


# ---------------------------------------------------------------------------
# Profiles (Pyroscope-shaped summaries)
# ---------------------------------------------------------------------------


class ProfileStore:
    def __init__(self) -> None:
        self.profiles: dict[str, dict[str, float]] = {}

    def record(self, service: str, samples: dict[str, float]) -> None:
        self.profiles[service] = samples

    def fetch(self, service: str) -> dict[str, float]:
        return self.profiles.get(service, {})

    def services(self) -> list[str]:
        return sorted(self.profiles)


# ---------------------------------------------------------------------------
# Annotations / incidents (Grafana-side state, mirrored locally)
# ---------------------------------------------------------------------------


@dataclass
class Annotation:
    annotation_id: int
    dashboard_uid: str | None
    panel_id: int | None
    time: datetime
    time_end: datetime | None
    text: str
    tags: list[str]


@dataclass
class GrafanaIncident:
    incident_id: str
    title: str
    severity: str
    status: str
    created_at: datetime
    labels: dict[str, str]
    activity: list[dict[str, Any]] = field(default_factory=list)


class GrafanaState:
    """Local mirror of the Grafana objects the agent reads and writes."""

    def __init__(self) -> None:
        self.annotations: list[Annotation] = []
        self.incidents: dict[str, GrafanaIncident] = {}
        self._next_annotation = 1
        self.dashboards = DASHBOARD_INDEX
        self.alert_rules: list[dict[str, Any]] = []

    def add_annotation(
        self,
        text: str,
        tags: list[str],
        dashboard_uid: str | None = None,
        panel_id: int | None = None,
        at: datetime | None = None,
        time_end: datetime | None = None,
    ) -> Annotation:
        a = Annotation(
            self._next_annotation, dashboard_uid, panel_id, at or utcnow(), time_end, text, tags
        )
        self._next_annotation += 1
        self.annotations.append(a)
        return a

    def find_annotations(
        self,
        tags: list[str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 50,
    ) -> list[Annotation]:
        out = []
        for a in self.annotations:
            if tags and not set(tags) & set(a.tags):
                continue
            if start and a.time < start:
                continue
            if end and a.time > end:
                continue
            out.append(a)
        return out[-limit:]

    def create_incident(
        self, title: str, severity: str, labels: dict[str, str] | None = None
    ) -> GrafanaIncident:
        iid = f"gi-{uuid.uuid4().hex[:8]}"
        inc = GrafanaIncident(iid, title, severity, "active", utcnow(), labels or {})
        self.incidents[iid] = inc
        return inc

    def add_activity(self, incident_id: str, body: str, kind: str = "userNote") -> dict[str, Any]:
        inc = self.incidents.get(incident_id)
        if inc is None:
            raise KeyError(incident_id)
        item = {"at": utcnow().isoformat(), "kind": kind, "body": body}
        inc.activity.append(item)
        return item

    def clear(self) -> None:
        self.annotations.clear()
        self.incidents.clear()
        self._next_annotation = 1


DASHBOARD_INDEX = [
    {
        "uid": "raccord-exec",
        "title": "Raccord / Executive accessibility reliability",
        "tags": ["raccord", "executive", "slo"],
        "folderTitle": "Raccord",
        "url": "/d/raccord-exec/executive-accessibility-reliability",
    },
    {
        "uid": "raccord-cockpit",
        "title": "Raccord / Live event command centre",
        "tags": ["raccord", "live", "event"],
        "folderTitle": "Raccord",
        "url": "/d/raccord-cockpit/live-event-command-centre",
    },
    {
        "uid": "raccord-incident",
        "title": "Raccord / Incident investigation",
        "tags": ["raccord", "incident", "captions"],
        "folderTitle": "Raccord",
        "url": "/d/raccord-incident/incident-investigation",
    },
    {
        "uid": "raccord-quality",
        "title": "Raccord / Probe and model quality",
        "tags": ["raccord", "model", "quality"],
        "folderTitle": "Raccord",
        "url": "/d/raccord-quality/probe-and-model-quality",
    },
    {
        "uid": "raccord-agent",
        "title": "Raccord / Agent and MCP observability",
        "tags": ["raccord", "agent", "mcp", "ai-observability"],
        "folderTitle": "Raccord",
        "url": "/d/raccord-agent/agent-and-mcp-observability",
    },
]


# ---------------------------------------------------------------------------
# Component log generation
# ---------------------------------------------------------------------------


def emit_component_logs(sim, store: LogStore, now: datetime | None = None) -> int:
    """Produce the log lines the delivery components would actually write.

    Healthy components are noisy too - that is the point. The clock daemon logs
    every resynchronisation whether or not it caused a problem, so finding the
    relevant line is a real search rather than a lookup.
    """
    now = now or sim.wall_clock
    rng = random.Random(int(now.timestamp()) ^ sim.seed)
    written = 0

    def w(component: str, level: str, line: str, **extra) -> None:
        nonlocal written
        store.append(
            line, {"service": component, "level": level, "event": sim.event_id, **extra}, now
        )
        written += 1

    pool = sim.caption_encoder_pool
    w(
        pool,
        "info",
        f'msg="cue batch flushed" pool={pool} clock={sim.clock_source} '
        f"batch={rng.randint(18, 34)} queue_depth={rng.randint(0, 4)}",
    )
    w(
        "packager-main",
        "info",
        f'msg="segment published" generation={sim.manifest_generation} '
        f"renditions={rng.randint(9, 12)} duration_ms={rng.randint(1800, 2200)}",
    )
    w(
        "cdn-primary",
        "info",
        f'msg="edge summary" hit_ratio={rng.uniform(0.93, 0.985):.3f} 5xx={rng.randint(0, 3)}',
    )
    if rng.random() < 0.35:
        w(
            sim.clock_source,
            "info",
            f'msg="ptp sync" offset_ns={rng.randint(-4000, 4000)} '
            f"path_delay_ns={rng.randint(1000, 9000)}",
        )

    for f in sim.active_faults:
        if f.neutralised or f.intensity(sim.program_s) <= 0:
            continue
        fid = f.spec.fault_id
        if fid in ("cap.progressive_drift", "infra.clock_source_change", "cap.clock_offset"):
            w(
                sim.clock_source,
                "warn",
                'msg="clock resynchronisation" reason=grandmaster_unreachable '
                f"previous=clock-ptp-primary current=clock-ntp-fallback "
                f"step_ms={int(f.intensity(sim.program_s) * 8000)}",
            )
            w(
                f.spec.component,
                "warn",
                f'msg="caption pts realigned to new reference" pool={f.spec.component} '
                f"offset_ms={int(f.intensity(sim.program_s) * 8000)} "
                f"languages={','.join(f.scope.get('languages') or ['all'])}",
            )
        elif fid == "cap.encoder_failure":
            w(
                f.spec.component,
                "error",
                'msg="encoder worker exited" signal=SIGSEGV restarts='
                f"{rng.randint(2, 9)} pool={f.spec.component}",
            )
        elif fid in (
            "cap.manifest_omission",
            "ad.track_omission",
            "alt.track_omission",
            "infra.malformed_manifest",
        ):
            w(
                "manifest-main",
                "error",
                'msg="rendition dropped from manifest" reason=track_map_mismatch '
                f"generation={sim.manifest_generation}",
            )
        elif fid in ("sign.frozen", "sign.black", "sign.low_framerate", "infra.gpu_saturation"):
            w(
                "signsrc-lsf",
                "error",
                f'msg="interpreter feed degraded" fps={f.spec.params.get("fps", 12)} '
                f"gpu_util={f.spec.params.get('gpu_saturation', 0.99)}",
            )
        elif fid in ("infra.cdn_regional", "sign.regional_delivery"):
            region = (f.scope.get("cdn_regions") or ["eu-west"])[0]
            w(
                "cdn-primary",
                "error",
                f'msg="origin fetch failures" region={region} '
                f"status=503 count={rng.randint(40, 200)}",
            )
        elif fid.startswith("player.") or fid == "infra.deploy_regression":
            w(
                f.spec.component,
                "error",
                'msg="accessibility assertion failed in canary" '
                f"build={f.spec.component} check={fid}",
            )
        elif fid == "infra.encoder_cpu":
            w(
                f.spec.component,
                "warn",
                f'msg="cpu saturation" utilisation={f.spec.params.get("cpu_saturation", 0.97)} '
                f"queue_depth={rng.randint(20, 90)}",
            )
        elif fid == "auth.failure":
            w(
                "auth-svc",
                "error",
                'msg="challenge served without accessible alternative" '
                "challenge=bot-protection a11y_fallback=absent",
            )
    return written


# ---------------------------------------------------------------------------
# Remote exporters (used when the docker stack is running)
# ---------------------------------------------------------------------------


class RemoteExporters:
    """Best-effort local or authenticated Grafana Cloud telemetry export."""

    def __init__(self) -> None:
        s = get_settings()
        self.loki_url = s.loki_url.rstrip("/")
        self.otlp_endpoint = s.otlp_endpoint.rstrip("/")
        self.grafana_url = s.grafana_url.rstrip("/")
        self.grafana_token = s.grafana_service_account_token
        self.otlp_username = s.otlp_username
        self.otlp_auth_token = s.otlp_auth_token
        self.enabled = False
        self.last_error: str | None = None

    @property
    def cloud_otlp(self) -> bool:
        return bool(self.otlp_username and self.otlp_auth_token)

    @property
    def otlp_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/x-protobuf"}
        if self.cloud_otlp:
            value = base64.b64encode(
                f"{self.otlp_username}:{self.otlp_auth_token}".encode()
            ).decode()
            headers["Authorization"] = f"Basic {value}"
        return headers

    def _post_otlp(self, signal: str, request, timeout: float = 5.0) -> bool:
        try:
            response = httpx.post(
                f"{self.otlp_endpoint}/v1/{signal}",
                content=request.SerializeToString(),
                headers=self.otlp_headers,
                timeout=timeout,
            )
            if response.status_code >= 300:
                self.last_error = f"OTLP {signal} returned HTTP {response.status_code}"
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            return False

    def probe(self, timeout: float = 1.0) -> bool:
        if self.cloud_otlp:
            try:
                from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
                    ExportMetricsServiceRequest,
                )

                self.enabled = self._post_otlp(
                    "metrics", ExportMetricsServiceRequest(), timeout=timeout
                )
                return self.enabled
            except ImportError as exc:
                self.last_error = f"OTLP protobuf support is missing: {exc}"
                self.enabled = False
                return False
        try:
            r = httpx.get(f"{self.loki_url}/ready", timeout=timeout)
            self.enabled = r.status_code < 500
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            self.enabled = False
        return self.enabled

    def push_annotations(
        self, annotations: Iterable[Annotation], offset: timedelta = timedelta(0)
    ) -> int:
        """Write change and recovery annotations onto the real Grafana timeline.

        This is Raccord *emitting* its own telemetry, not the agent reading
        evidence: the agent still learns nothing except through the MCP server
        (ADR 0002). Without it, `find_annotations` over a real Grafana returns
        nothing and change correlation has no candidates to rank.
        """
        if not self.grafana_token:
            return 0
        headers = {"Authorization": f"Bearer {self.grafana_token}"}
        written = 0
        for a in annotations:
            payload: dict[str, Any] = {
                "text": a.text,
                "tags": list(a.tags),
                "time": int((a.time + offset).timestamp() * 1000),
            }
            if a.time_end:
                payload["timeEnd"] = int((a.time_end + offset).timestamp() * 1000)
            try:
                r = httpx.post(
                    f"{self.grafana_url}/api/annotations",
                    json=payload,
                    headers=headers,
                    timeout=3.0,
                )
                written += 1 if r.status_code < 300 else 0
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
        return written

    def push_logs(self, lines: Iterable[LogLine], offset: timedelta = timedelta(0)) -> bool:
        lines = list(lines)
        if self.cloud_otlp:
            return self._push_otlp_logs(lines, offset)
        streams: dict[tuple, list[list[str]]] = defaultdict(list)
        for entry in lines:
            shifted = LogLine(entry.ts + offset, entry.labels, entry.line)
            streams[tuple(sorted(entry.labels.items()))].append(list(shifted.to_loki()))
        if not streams:
            return True
        payload = {"streams": [{"stream": dict(k), "values": v} for k, v in streams.items()]}
        try:
            r = httpx.post(f"{self.loki_url}/loki/api/v1/push", json=payload, timeout=3.0)
            return r.status_code < 300
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            return False

    def _push_otlp_logs(self, lines: list[LogLine], offset: timedelta) -> bool:
        if not lines:
            return True
        from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
            ExportLogsServiceRequest,
        )
        from opentelemetry.proto.common.v1.common_pb2 import AnyValue, InstrumentationScope
        from opentelemetry.proto.logs.v1.logs_pb2 import LogRecord, ResourceLogs, ScopeLogs
        from opentelemetry.proto.resource.v1.resource_pb2 import Resource

        severity = {"debug": 5, "info": 9, "warn": 13, "warning": 13, "error": 17}
        records = []
        for entry in lines:
            ts = int((entry.ts + offset).timestamp() * 1e9)
            level = entry.labels.get("level", "info").lower()
            records.append(
                LogRecord(
                    time_unix_nano=ts,
                    observed_time_unix_nano=ts,
                    severity_number=severity.get(level, 9),
                    severity_text=level.upper(),
                    body=AnyValue(string_value=entry.line),
                    attributes=_otlp_attributes(entry.labels),
                )
            )
        request = ExportLogsServiceRequest(
            resource_logs=[
                ResourceLogs(
                    resource=Resource(
                        attributes=_otlp_attributes({"service.name": "raccord-media"})
                    ),
                    scope_logs=[
                        ScopeLogs(
                            scope=InstrumentationScope(name="raccord", version="0.1.0"),
                            log_records=records,
                        )
                    ],
                )
            ]
        )
        return self._post_otlp("logs", request)

    def push_spans(self, spans: Iterable[Span], offset: timedelta = timedelta(0)) -> bool:
        spans = list(spans)
        if not spans:
            return True
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )
        from opentelemetry.proto.common.v1.common_pb2 import InstrumentationScope
        from opentelemetry.proto.resource.v1.resource_pb2 import Resource
        from opentelemetry.proto.trace.v1.trace_pb2 import (
            ResourceSpans,
            ScopeSpans,
            Status,
        )
        from opentelemetry.proto.trace.v1.trace_pb2 import Span as OTLPSpan

        resource_spans: dict[str, list] = defaultdict(list)
        for s in spans:
            start = s.start + offset
            resource_spans[s.service].append(
                OTLPSpan(
                    trace_id=_otlp_id(s.trace_id, 16),
                    span_id=_otlp_id(s.span_id, 8),
                    parent_span_id=_otlp_id(s.parent_id, 8) if s.parent_id else b"",
                    name=s.name,
                    kind=1,
                    start_time_unix_nano=int(start.timestamp() * 1e9),
                    end_time_unix_nano=int(
                        (start + timedelta(milliseconds=s.duration_ms)).timestamp() * 1e9
                    ),
                    attributes=_otlp_attributes(s.attributes),
                    status=Status(code=1 if s.status == "ok" else 2),
                )
            )
        request = ExportTraceServiceRequest(
            resource_spans=[
                ResourceSpans(
                    resource=Resource(attributes=_otlp_attributes({"service.name": service})),
                    scope_spans=[
                        ScopeSpans(
                            scope=InstrumentationScope(name="raccord", version="0.1.0"),
                            spans=spans_,
                        )
                    ],
                )
                for service, spans_ in resource_spans.items()
            ]
        )
        return self._post_otlp("traces", request)

    def push_metrics(self, samples: Iterable[tuple[str, Labels, Sample]]) -> bool:
        samples = list(samples)
        if not samples or not self.cloud_otlp:
            return True
        from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
            ExportMetricsServiceRequest,
        )
        from opentelemetry.proto.common.v1.common_pb2 import InstrumentationScope
        from opentelemetry.proto.metrics.v1.metrics_pb2 import (
            Gauge,
            Metric,
            NumberDataPoint,
            ResourceMetrics,
            ScopeMetrics,
        )
        from opentelemetry.proto.resource.v1.resource_pb2 import Resource

        grouped: dict[str, list] = defaultdict(list)
        for name, labels, sample in samples:
            grouped[name].append(
                NumberDataPoint(
                    attributes=_otlp_attributes(labels),
                    time_unix_nano=int(sample.ts.timestamp() * 1e9),
                    as_double=sample.value,
                )
            )
        request = ExportMetricsServiceRequest(
            resource_metrics=[
                ResourceMetrics(
                    resource=Resource(
                        attributes=_otlp_attributes({"service.name": "raccord"})
                    ),
                    scope_metrics=[
                        ScopeMetrics(
                            scope=InstrumentationScope(name="raccord", version="0.1.0"),
                            metrics=[
                                Metric(name=name, gauge=Gauge(data_points=points))
                                for name, points in sorted(grouped.items())
                            ],
                        )
                    ],
                )
            ]
        )
        return self._post_otlp("metrics", request)


def _otlp_attributes(values: dict[str, Any]):
    from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

    return [
        KeyValue(key=str(key), value=AnyValue(string_value=str(value)))
        for key, value in sorted(values.items())
    ]


def _otlp_id(value: str, size: int) -> bytes:
    try:
        parsed = bytes.fromhex(value)
    except ValueError:
        parsed = b""
    return parsed if len(parsed) == size else hashlib.sha256(value.encode()).digest()[:size]


# ---------------------------------------------------------------------------
# Facade
# ---------------------------------------------------------------------------


class TelemetryPlane:
    def __init__(self) -> None:
        self.metrics = MetricStore()
        self.logs = LogStore()
        self.traces = TraceStore()
        self.profiles = ProfileStore()
        self.grafana = GrafanaState()
        self.exporters = RemoteExporters()
        self.mcp_calls: list[dict[str, Any]] = []
        self.agent_steps: list[dict[str, Any]] = []
        # A point on the simulated event clock; unset until the first export.
        self._export_cursor: datetime | None = None
        self._annotations_exported = 0

    def export(self, offset: timedelta = timedelta(0)) -> dict[str, int]:
        """Push telemetry produced since the last call to the real Grafana stack.

        `offset` maps the simulated event clock onto the real one. The programme
        advances faster than wall time, so a log line or span carries a simulated
        timestamp that can be minutes ahead of now - while Prometheus stamps the
        same findings at scrape time. Shifting on export keeps metric, log and
        trace evidence for one incident inside one window, which is the whole
        point of correlating them.

        No-op and never fatal when the stack is absent: the offline demo and the
        1,000-scenario benchmark must not depend on a Grafana being there.
        """
        if not self.exporters.enabled:
            return {"metrics": 0, "logs": 0, "spans": 0, "annotations": 0}
        # The cursor is a point on the *simulated* clock, not the real one, so it
        # starts unset rather than at `now`: the two are unrelated until the
        # first tick establishes where the event's timeline actually is.
        cursor = self._export_cursor
        lines = self.logs.since(cursor) if cursor else self.logs.query(limit=100000)
        spans = self.traces.since(cursor) if cursor else self.traces.search(limit=100000)
        pending = self.grafana.annotations[self._annotations_exported :]

        metrics = self.metrics.latest()
        self.exporters.push_metrics(metrics)
        self.exporters.push_logs(lines, offset)
        self.exporters.push_spans(spans, offset)
        written = self.exporters.push_annotations(pending, offset)

        self._annotations_exported = len(self.grafana.annotations)
        seen = [e.ts for e in lines] + [s.start for s in spans]
        if seen:
            self._export_cursor = max(seen)
        return {
            "metrics": len(metrics),
            "logs": len(lines),
            "spans": len(spans),
            "annotations": written,
        }

    def record_mcp_call(
        self, tool: str, args: dict, duration_ms: float, ok: bool, result_size: int
    ) -> None:
        self.mcp_calls.append(
            {
                "at": utcnow().isoformat(),
                "tool": tool,
                "args": args,
                "duration_ms": round(duration_ms, 2),
                "ok": ok,
                "result_size": result_size,
            }
        )
        self.metrics.record("raccord_mcp_call_duration_ms", duration_ms, {"tool": tool})
        self.metrics.record(
            "raccord_mcp_calls_total",
            float(len(self.mcp_calls)),
            {"tool": tool, "status": "ok" if ok else "error"},
        )

    def record_agent_step(
        self,
        agent: str,
        step: str,
        duration_ms: float,
        tokens_in: int = 0,
        tokens_out: int = 0,
        model: str = "deterministic",
    ) -> None:
        self.agent_steps.append(
            {
                "at": utcnow().isoformat(),
                "agent": agent,
                "step": step,
                "duration_ms": round(duration_ms, 2),
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "model": model,
            }
        )
        labels = {"agent": agent, "model": model}
        self.metrics.record("raccord_agent_step_duration_ms", duration_ms, labels)
        self.metrics.record("raccord_agent_tokens_in", float(tokens_in), labels)
        self.metrics.record("raccord_agent_tokens_out", float(tokens_out), labels)
        # Cost model mirrors the published Gemini rates; deterministic mode is free.
        cost = (
            (tokens_in / 1e6) * 1.25 + (tokens_out / 1e6) * 10.0
            if model != "deterministic"
            else 0.0
        )
        self.metrics.record("raccord_agent_cost_usd", cost, labels)

    def flush_remote(self) -> dict[str, bool]:
        return {
            "metrics": self.exporters.push_metrics(self.metrics.latest()),
            "logs": self.exporters.push_logs(list(self.logs._lines)[-500:]),
            "traces": self.exporters.push_spans(list(self.traces._spans)[-500:]),
        }

    def clear(self) -> None:
        self.metrics.clear()
        self.logs.clear()
        self.traces.clear()
        self.grafana.clear()
        self.mcp_calls.clear()
        self.agent_steps.clear()
        self._export_cursor = None
        self._annotations_exported = 0
