"""Telemetry plane: metrics, logs, traces and profiles.

Everything AccessPulse learns about the delivery chain becomes real telemetry:

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
        self._series: dict[tuple[str, tuple[tuple[str, str], ...]], deque[Sample]] = (
            defaultdict(lambda: deque(maxlen=retention))
        )
        self._lock = threading.Lock()

    def record(self, name: str, value: float, labels: Labels | None = None,
               ts: datetime | None = None) -> None:
        key = (name, tuple(sorted((labels or {}).items())))
        with self._lock:
            self._series[key].append(Sample(ts or utcnow(), float(value)))

    def record_many(self, metrics: dict[str, float], labels: Labels | None = None,
                    ts: datetime | None = None) -> None:
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
                s for s in samples
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
            out.append({
                "metric": {"__name__": name, **labels},
                "value": round(agg, 6),
                "samples": [[s.ts.isoformat(), round(s.value, 6)] for s in window[-60:]],
                "sample_count": len(window),
            })
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
                    rendered = ",".join(
                        f'{k}="{_escape(v)}"' for k, v in sorted(labels.items())
                    )
                    lines.append(f"{name}{{{rendered}}} {value}")
                else:
                    lines.append(f"{name} {value}")
        return "\n".join(lines) + "\n"

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

    def record(
        self, name: str, service: str, duration_ms: float, trace_id: str | None = None,
        parent_id: str | None = None, attributes: dict[str, Any] | None = None,
        status: str = "ok", start: datetime | None = None,
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

    def add_annotation(self, text: str, tags: list[str], dashboard_uid: str | None = None,
                       panel_id: int | None = None, at: datetime | None = None,
                       time_end: datetime | None = None) -> Annotation:
        a = Annotation(self._next_annotation, dashboard_uid, panel_id,
                       at or utcnow(), time_end, text, tags)
        self._next_annotation += 1
        self.annotations.append(a)
        return a

    def find_annotations(self, tags: list[str] | None = None, start: datetime | None = None,
                         end: datetime | None = None, limit: int = 50) -> list[Annotation]:
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

    def create_incident(self, title: str, severity: str,
                        labels: dict[str, str] | None = None) -> GrafanaIncident:
        iid = f"gi-{uuid.uuid4().hex[:8]}"
        inc = GrafanaIncident(iid, title, severity, "active", utcnow(), labels or {})
        self.incidents[iid] = inc
        return inc

    def add_activity(self, incident_id: str, body: str,
                     kind: str = "userNote") -> dict[str, Any]:
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
    {"uid": "ap-exec", "title": "AccessPulse / Executive accessibility reliability",
     "tags": ["accesspulse", "executive", "slo"], "folderTitle": "AccessPulse",
     "url": "/d/ap-exec/executive-accessibility-reliability"},
    {"uid": "ap-cockpit", "title": "AccessPulse / Live event command centre",
     "tags": ["accesspulse", "live", "event"], "folderTitle": "AccessPulse",
     "url": "/d/ap-cockpit/live-event-command-centre"},
    {"uid": "ap-incident", "title": "AccessPulse / Incident investigation",
     "tags": ["accesspulse", "incident", "captions"], "folderTitle": "AccessPulse",
     "url": "/d/ap-incident/incident-investigation"},
    {"uid": "ap-quality", "title": "AccessPulse / Probe and model quality",
     "tags": ["accesspulse", "model", "quality"], "folderTitle": "AccessPulse",
     "url": "/d/ap-quality/probe-and-model-quality"},
    {"uid": "ap-agent", "title": "AccessPulse / Agent and MCP observability",
     "tags": ["accesspulse", "agent", "mcp", "ai-observability"],
     "folderTitle": "AccessPulse", "url": "/d/ap-agent/agent-and-mcp-observability"},
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
    w(pool, "info",
      f'msg="cue batch flushed" pool={pool} clock={sim.clock_source} '
      f'batch={rng.randint(18, 34)} queue_depth={rng.randint(0, 4)}')
    w("packager-main", "info",
      f'msg="segment published" generation={sim.manifest_generation} '
      f'renditions={rng.randint(9, 12)} duration_ms={rng.randint(1800, 2200)}')
    w("cdn-primary", "info",
      f'msg="edge summary" hit_ratio={rng.uniform(0.93, 0.985):.3f} '
      f'5xx={rng.randint(0, 3)}')
    if rng.random() < 0.35:
        w(sim.clock_source, "info",
          f'msg="ptp sync" offset_ns={rng.randint(-4000, 4000)} '
          f'path_delay_ns={rng.randint(1000, 9000)}')

    for f in sim.active_faults:
        if f.neutralised or f.intensity(sim.program_s) <= 0:
            continue
        fid = f.spec.fault_id
        if fid in ("cap.progressive_drift", "infra.clock_source_change", "cap.clock_offset"):
            w(sim.clock_source, "warn",
              'msg="clock resynchronisation" reason=grandmaster_unreachable '
              f'previous=clock-ptp-primary current=clock-ntp-fallback '
              f'step_ms={int(f.intensity(sim.program_s) * 8000)}')
            w(f.spec.component, "warn",
              f'msg="caption pts realigned to new reference" pool={f.spec.component} '
              f'offset_ms={int(f.intensity(sim.program_s) * 8000)} '
              f'languages={",".join(f.scope.get("languages") or ["all"])}')
        elif fid == "cap.encoder_failure":
            w(f.spec.component, "error",
              'msg="encoder worker exited" signal=SIGSEGV restarts='
              f'{rng.randint(2, 9)} pool={f.spec.component}')
        elif fid in ("cap.manifest_omission", "ad.track_omission", "alt.track_omission",
                     "infra.malformed_manifest"):
            w("manifest-main", "error",
              'msg="rendition dropped from manifest" reason=track_map_mismatch '
              f'generation={sim.manifest_generation}')
        elif fid in ("sign.frozen", "sign.black", "sign.low_framerate",
                     "infra.gpu_saturation"):
            w("signsrc-lsf", "error",
              f'msg="interpreter feed degraded" fps={f.spec.params.get("fps", 12)} '
              f'gpu_util={f.spec.params.get("gpu_saturation", 0.99)}')
        elif fid in ("infra.cdn_regional", "sign.regional_delivery"):
            region = (f.scope.get("cdn_regions") or ["eu-west"])[0]
            w("cdn-primary", "error",
              f'msg="origin fetch failures" region={region} '
              f'status=503 count={rng.randint(40, 200)}')
        elif fid.startswith("player.") or fid == "infra.deploy_regression":
            w(f.spec.component, "error",
              'msg="accessibility assertion failed in canary" '
              f'build={f.spec.component} check={fid}')
        elif fid == "infra.encoder_cpu":
            w(f.spec.component, "warn",
              f'msg="cpu saturation" utilisation={f.spec.params.get("cpu_saturation", 0.97)} '
              f'queue_depth={rng.randint(20, 90)}')
        elif fid == "auth.failure":
            w("auth-svc", "error",
              'msg="challenge served without accessible alternative" '
              'challenge=bot-protection a11y_fallback=absent')
    return written


# ---------------------------------------------------------------------------
# Remote exporters (used when the docker stack is running)
# ---------------------------------------------------------------------------


class RemoteExporters:
    """Best-effort push to a local Grafana stack. Never fatal if it is absent."""

    def __init__(self) -> None:
        s = get_settings()
        self.loki_url = s.loki_url.rstrip("/")
        self.otlp_endpoint = s.otlp_endpoint.rstrip("/")
        self.enabled = False
        self.last_error: str | None = None

    def probe(self, timeout: float = 1.0) -> bool:
        try:
            r = httpx.get(f"{self.loki_url}/ready", timeout=timeout)
            self.enabled = r.status_code < 500
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            self.enabled = False
        return self.enabled

    def push_logs(self, lines: Iterable[LogLine]) -> bool:
        streams: dict[tuple, list[list[str]]] = defaultdict(list)
        for entry in lines:
            streams[tuple(sorted(entry.labels.items()))].append(list(entry.to_loki()))
        if not streams:
            return True
        payload = {
            "streams": [
                {"stream": dict(k), "values": v} for k, v in streams.items()
            ]
        }
        try:
            r = httpx.post(f"{self.loki_url}/loki/api/v1/push", json=payload, timeout=3.0)
            return r.status_code < 300
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            return False

    def push_spans(self, spans: Iterable[Span]) -> bool:
        resource_spans = defaultdict(list)
        for s in spans:
            resource_spans[s.service].append({
                "traceId": s.trace_id,
                "spanId": s.span_id,
                "parentSpanId": s.parent_id or "",
                "name": s.name,
                "kind": 1,
                "startTimeUnixNano": str(int(s.start.timestamp() * 1e9)),
                "endTimeUnixNano": str(
                    int((s.start + timedelta(milliseconds=s.duration_ms)).timestamp() * 1e9)
                ),
                "attributes": [
                    {"key": k, "value": {"stringValue": str(v)}}
                    for k, v in s.attributes.items()
                ],
                "status": {"code": 1 if s.status == "ok" else 2},
            })
        if not resource_spans:
            return True
        payload = {
            "resourceSpans": [
                {
                    "resource": {"attributes": [
                        {"key": "service.name", "value": {"stringValue": service}}
                    ]},
                    "scopeSpans": [{"scope": {"name": "accesspulse"}, "spans": spans_}],
                }
                for service, spans_ in resource_spans.items()
            ]
        }
        try:
            r = httpx.post(f"{self.otlp_endpoint}/v1/traces", json=payload, timeout=3.0)
            return r.status_code < 300
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            return False


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

    def record_mcp_call(self, tool: str, args: dict, duration_ms: float,
                        ok: bool, result_size: int) -> None:
        self.mcp_calls.append({
            "at": utcnow().isoformat(), "tool": tool, "args": args,
            "duration_ms": round(duration_ms, 2), "ok": ok, "result_size": result_size,
        })
        self.metrics.record("accesspulse_mcp_call_duration_ms", duration_ms, {"tool": tool})
        self.metrics.record("accesspulse_mcp_calls_total", float(len(self.mcp_calls)),
                            {"tool": tool, "status": "ok" if ok else "error"})

    def record_agent_step(self, agent: str, step: str, duration_ms: float,
                          tokens_in: int = 0, tokens_out: int = 0,
                          model: str = "deterministic") -> None:
        self.agent_steps.append({
            "at": utcnow().isoformat(), "agent": agent, "step": step,
            "duration_ms": round(duration_ms, 2), "tokens_in": tokens_in,
            "tokens_out": tokens_out, "model": model,
        })
        labels = {"agent": agent, "model": model}
        self.metrics.record("accesspulse_agent_step_duration_ms", duration_ms, labels)
        self.metrics.record("accesspulse_agent_tokens_in", float(tokens_in), labels)
        self.metrics.record("accesspulse_agent_tokens_out", float(tokens_out), labels)
        # Cost model mirrors the published Gemini rates; deterministic mode is free.
        cost = (tokens_in / 1e6) * 1.25 + (tokens_out / 1e6) * 10.0 \
            if model != "deterministic" else 0.0
        self.metrics.record("accesspulse_agent_cost_usd", cost, labels)

    def flush_remote(self) -> dict[str, bool]:
        return {
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
