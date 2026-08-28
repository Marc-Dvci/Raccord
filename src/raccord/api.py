"""Raccord HTTP API and web application.

Serves the operational product (readiness studio, live cockpit, evidence replay,
approval, verification, public status, post-incident review, benchmark lab) and
exposes /metrics for Prometheus so the Grafana stack sees exactly what the
operator sees.

Every state-changing endpoint goes through the same coordinator, policy engine
and executor the CLI and benchmark use. There is no privileged API path.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import media
from .agents import adk
from .agents import ask as ask_agent
from .certification import ReleaseGate
from .certification import summarise as summarise_cert
from .config import get_settings
from .contracts import (
    IncidentState,
    Role,
)
from .faults import FAULT_LIBRARY, HERO_FAULT_ID
from .policy import ACTION_CATALOG, RULES
from .probes import caption as caption_probe
from .runtime import RaccordRuntime
from .simulator import _platform_of
from .slo import ALL_SLOS, OPERATIONAL_SLOS
from .verification import SUITES

WEB_DIR = Path(__file__).parent / "web"

runtime: RaccordRuntime | None = None
_lock = asyncio.Lock()


def get_runtime() -> RaccordRuntime:
    if runtime is None:
        raise HTTPException(503, "runtime not started")
    return runtime


@asynccontextmanager
async def lifespan(app: FastAPI):
    global runtime
    adk.require_available()
    from .observability import require_agent_observability

    require_agent_observability()
    runtime = RaccordRuntime(db_prefix="api")
    await runtime.connect()
    # Warm the event with a healthy baseline so the cockpit is never empty.
    for _ in range(3):
        runtime.tick(15)
    yield
    await runtime.aclose()


app = FastAPI(
    title="Raccord",
    version="0.1.0",
    description="Accessible Experience Reliability Platform",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Web app
# ---------------------------------------------------------------------------

if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index() -> Any:
    path = WEB_DIR / "index.html"
    if not path.exists():
        return HTMLResponse("<h1>Raccord</h1><p>Web assets not built.</p>")
    return FileResponse(path)


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


@app.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
async def metrics() -> str:
    """Prometheus exposition of every probe finding, SLO state and agent metric."""
    rt = get_runtime()
    return rt.telemetry.metrics.snapshot_prometheus()


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False)
async def readyz() -> dict:
    """Readiness includes the explicitly selected reasoning plane."""
    try:
        adk.require_available()
    except adk.ReasoningUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    rt = get_runtime()
    if not rt._connected:
        raise HTTPException(503, "Grafana MCP connection has not completed")
    return {
        "status": "ready",
        "reasoning_mode": get_settings().reasoning_mode,
        "mcp_transport": rt.mcp.transport,
        "agent_observability": get_settings().agent_observability_enabled,
    }


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@app.get("/api/state")
async def state() -> dict:
    return get_runtime().snapshot()


@app.post("/api/tick")
async def tick(seconds: float = Query(15.0, ge=1.0, le=300.0)) -> dict:
    async with _lock:
        return get_runtime().tick(seconds)


@app.post("/api/reset")
async def reset() -> dict:
    """Deterministic judge reset: same seed, same timeline, clean state."""
    async with _lock:
        rt = get_runtime()
        rt.reset()
        await rt.connect()
        for _ in range(3):
            rt.tick(15)
        return {"reset": True, **rt.snapshot()}


@app.get("/api/event")
async def event() -> dict:
    rt = get_runtime()
    return {
        "event_id": rt.event_id,
        "media": media.MEDIA_MANIFEST,
        "promises": [p.model_dump(mode="json") for p in rt.promises],
        "slos": [
            {
                "slo_id": s.slo_id,
                "name": s.name,
                "description": s.description,
                "feature": s.feature.value,
                "metric": s.sli_metric,
                "unit": s.unit,
                "comparator": s.comparator.value,
                "hard_gate": s.hard_gate,
                "objective_tier0": s.threshold(list(s.objectives)[0]),
            }
            for s in ALL_SLOS
        ],
        "operational_slos": [
            {
                "slo_id": s.slo_id,
                "name": s.name,
                "metric": s.metric,
                "max_seconds": s.max_seconds,
                "max_ratio": s.max_ratio,
            }
            for s in OPERATIONAL_SLOS
        ],
    }


@app.get("/api/twin")
async def twin() -> dict:
    return get_runtime().twin.to_dict()


@app.get("/api/twin/blast-radius")
async def blast_radius(component: str = Query(...)) -> dict:
    rt = get_runtime()
    br = rt.twin.blast_radius([component])
    return {
        "origin": br.origin_nodes,
        "downstream": br.downstream_nodes,
        "features": [f.value for f in br.features],
        "languages": br.languages,
        "territories": br.territories,
        "platforms": [p.value for p in br.platforms],
        "player_versions": br.player_versions,
        "cdn_regions": br.cdn_regions,
        "providers": br.providers,
        "promises": br.promise_ids,
        "owners": br.owners,
        "safe_remediation_targets": br.safe_remediation_targets,
        "at_risk_adjacent": br.at_risk_adjacent,
    }


@app.get("/api/policy")
async def policy() -> dict:
    return {
        "rules": [{"rule_id": r.rule_id, "description": r.description} for r in RULES],
        "actions": [
            {
                "action_type": s.action_type.value,
                "title": s.title,
                "description": s.description,
                "features": [f.value for f in s.features],
                "preconditions": list(s.preconditions),
                "allowed_targets": list(s.allowed_targets),
                "required_role": s.default_required_role.value,
                "expected_metric_change": s.expected_metric_change,
                "verification_suite": s.verification_suite,
                "rollback": s.rollback_behaviour,
                "audience_visible": s.audience_visible,
            }
            for s in ACTION_CATALOG.values()
        ],
        "verification_suites": {
            name: [
                {
                    "name": a.name,
                    "description": a.description,
                    "slo": a.slo_id,
                    "scope": a.scope_kind,
                    "mandatory": a.mandatory,
                }
                for a in suite
            ]
            for name, suite in SUITES.items()
        },
    }


@app.get("/api/faults")
async def faults() -> list[dict]:
    return [
        {
            "fault_id": f.fault_id,
            "name": f.name,
            "description": f.description,
            "feature": f.feature.value,
            "failure_class": f.failure_class.value,
            "component": f.component,
            "onset": f.onset,
            "expected_slos": list(f.expected_slos),
            "remediation": list(f.remediation),
            "difficulty": f.difficulty,
            "scope": {k: v for k, v in f.default_scope.items() if v},
        }
        for f in FAULT_LIBRARY.values()
    ]


# ---------------------------------------------------------------------------
# Preflight certification
# ---------------------------------------------------------------------------


@app.post("/api/certify")
async def certify() -> dict:
    rt = get_runtime()
    gate = ReleaseGate(rt.sim, rt.registry, rt.event_id, rt.mcp)
    record = await gate.run()
    return {
        "summary": summarise_cert(record),
        "blockers": list(record.blockers),
        "assertions": [a.model_dump(mode="json") for a in record.assertions],
        "model_versions": record.model_versions,
        "signature": record.signature,
    }


# ---------------------------------------------------------------------------
# Faults and incidents
# ---------------------------------------------------------------------------


class InjectRequest(BaseModel):
    fault_id: str
    ticks: int = 8
    seconds_per_tick: float = 20.0


class DemoRunRequest(BaseModel):
    fault_id: str = HERO_FAULT_ID
    ticks: int = 9
    seconds_per_tick: float = 20.0


@app.post("/api/inject")
async def inject(req: InjectRequest = Body(...)) -> dict:
    if req.fault_id not in FAULT_LIBRARY:
        raise HTTPException(404, f"unknown fault: {req.fault_id}")
    async with _lock:
        rt = get_runtime()
        uid = rt.inject(req.fault_id)
        last = {}
        for _ in range(req.ticks):
            last = rt.tick(req.seconds_per_tick)
        return {"fault_uid": uid, "fault_id": req.fault_id, **last}


@app.post("/api/demo/run")
async def run_judge_demo(req: DemoRunRequest = Body(...)) -> dict:
    """Atomically reset, inject, investigate and verify one judge scenario.

    The lock prevents two public-demo visitors from interleaving mutations in
    the deliberately single-instance simulator. Production mode never exposes
    automatic approval or this destructive reset path.
    """
    settings = get_settings()
    if not settings.demo_mode:
        raise HTTPException(403, "the one-click judge run is available only in demo mode")
    if req.fault_id not in FAULT_LIBRARY:
        raise HTTPException(404, f"unknown fault: {req.fault_id}")
    if not 1 <= req.ticks <= 30 or not 1.0 <= req.seconds_per_tick <= 300.0:
        raise HTTPException(422, "ticks must be 1..30 and seconds_per_tick must be 1..300")

    async with _lock:
        rt = get_runtime()
        writes_before = len(rt.cloud.writes)
        rt.reset()
        await rt.connect()
        for _ in range(3):
            rt.tick(15)
        rt.inject(req.fault_id)
        for _ in range(req.ticks):
            rt.tick(req.seconds_per_tick)
        result = await rt.run_incident(auto_approve=True)
        cloud_writes = rt.cloud.writes[writes_before:]
        payload = _scenario_payload(rt, result)
        payload["execution"] = {
            "reasoning_mode": settings.reasoning_mode,
            "reasoning_runtime": (
                "agent-engine" if settings.agent_engine_resource else "local"
            ),
            "mcp_transport": rt.mcp.transport,
            "mcp_tools_available": len(rt.mcp.tool_names),
            "agent_observability": settings.agent_observability_enabled,
        }
        payload["cloud_evidence"] = {
            "enabled": rt.cloud.enabled,
            "writes": len(cloud_writes),
            "successful": sum(write.ok for write in cloud_writes),
            "failed": sum(not write.ok for write in cloud_writes),
            "targets": sorted({write.target for write in cloud_writes}),
        }
        payload["telemetry_export"] = {
            "configured": settings.export_telemetry,
            "connected": rt.telemetry.exporters.enabled,
            "healthy": rt.telemetry.exporters.last_error is None,
        }
        return payload


@app.get("/api/cloud-status")
async def cloud_status() -> dict:
    """Non-secret proof of configured integrations and observed write outcomes."""
    rt = get_runtime()
    writes = rt.cloud.writes
    return {
        "project": rt.settings.google_cloud_project,
        "evidence_enabled": rt.cloud.enabled,
        "evidence_targets": {
            "pubsub": bool(rt.settings.probe_findings_topic),
            "storage": bool(rt.settings.evidence_bucket),
            "bigquery": bool(rt.settings.analytics_dataset),
        },
        "writes": {
            "total": len(writes),
            "successful": sum(write.ok for write in writes),
            "failed": sum(not write.ok for write in writes),
            "targets": sorted({write.target for write in writes}),
        },
        "telemetry": {
            "configured": rt.settings.export_telemetry,
            "connected": rt.telemetry.exporters.enabled,
            "healthy": rt.telemetry.exporters.last_error is None,
        },
        "agent_observability": rt.settings.agent_observability_enabled,
    }


@app.post("/api/incident/run")
async def run_incident(
    auto_approve: bool = Query(False),
    approver: str = Query("t.duval@studio.example"),
) -> dict:
    if auto_approve and not get_settings().demo_mode:
        raise HTTPException(403, "automatic approval is available only in isolated demo mode")
    async with _lock:
        rt = get_runtime()
        result = await rt.run_incident(approver=approver, auto_approve=auto_approve)
        return _scenario_payload(rt, result)


@app.post("/api/incident/step/detect")
async def step_detect() -> dict:
    """Detect and open an incident, stopping at SCOPED for human review."""
    async with _lock:
        rt = get_runtime()
        pairs = rt.coordinator.detect(rt.fault_onset)
        if not pairs:
            return {"detected": False, "alerts": []}
        alert, group = rt._pick_primary(pairs, None)
        incident = rt.coordinator.open_incident(alert, group, rt.fault_onset)
        if incident is None:
            return {"detected": True, "suppressed": True}
        return {
            "detected": True,
            "incident_id": incident.incident_id,
            "alerts": [a.model_dump(mode="json") for a, _ in pairs],
            "incident": incident.model_dump(mode="json"),
        }


@app.post("/api/incident/{incident_id}/step/investigate")
async def step_investigate(incident_id: str) -> dict:
    async with _lock:
        rt = get_runtime()
        incident = _incident(rt, incident_id)
        await rt.coordinator.investigate(incident)
        return _incident_payload(rt, incident)


@app.post("/api/incident/{incident_id}/step/diagnose")
async def step_diagnose(incident_id: str) -> dict:
    async with _lock:
        rt = get_runtime()
        incident = _incident(rt, incident_id)
        rt.coordinator.diagnose(incident)
        await rt.coordinator.enrich_reasoning(incident)
        return _incident_payload(rt, incident)


@app.post("/api/incident/{incident_id}/step/policy")
async def step_policy(incident_id: str) -> dict:
    async with _lock:
        rt = get_runtime()
        incident = _incident(rt, incident_id)
        rt.coordinator.evaluate_policy(incident, live=True)
        return _incident_payload(rt, incident)


class ApproveRequest(BaseModel):
    approver: str = "t.duval@studio.example"
    role: str = Role.TECHNICAL_DIRECTOR.value


@app.post("/api/incident/{incident_id}/approve")
async def approve(
    incident_id: str,
    request: Request,
    req: ApproveRequest = Body(...),
) -> dict:
    async with _lock:
        rt = get_runtime()
        incident = _incident(rt, incident_id)
        try:
            approver, role = _operator_identity(request, req)
            approval = rt.coordinator.approve(incident, approver, role)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(403, str(exc)) from exc
        return {"approval": approval.model_dump(mode="json"), **_incident_payload(rt, incident)}


@app.post("/api/incident/{incident_id}/reject")
async def reject(
    incident_id: str,
    request: Request,
    reason: str = Query("operator declined"),
) -> dict:
    async with _lock:
        rt = get_runtime()
        incident = _incident(rt, incident_id)
        actor, _ = _operator_identity(request, ApproveRequest())
        rt.coordinator.reject(incident, actor, reason)
        return _incident_payload(rt, incident)


@app.post("/api/incident/{incident_id}/step/remediate")
async def step_remediate(incident_id: str) -> dict:
    async with _lock:
        rt = get_runtime()
        incident = _incident(rt, incident_id)
        await rt.coordinator.remediate(incident)
        return _incident_payload(rt, incident)


@app.post("/api/incident/{incident_id}/step/verify")
async def step_verify(incident_id: str, settle: float = Query(20.0)) -> dict:
    async with _lock:
        rt = get_runtime()
        incident = _incident(rt, incident_id)
        ok = await rt.coordinator.verify(incident, settle)
        return {"recovered": ok, **_incident_payload(rt, incident)}


@app.post("/api/incident/{incident_id}/step/communicate")
async def step_communicate(incident_id: str) -> dict:
    async with _lock:
        rt = get_runtime()
        incident = _incident(rt, incident_id)
        await rt.coordinator.communicate(incident, incident.state is IncidentState.RECOVERED)
        return _incident_payload(rt, incident)


@app.post("/api/incident/{incident_id}/step/review")
async def step_review(incident_id: str) -> dict:
    async with _lock:
        rt = get_runtime()
        incident = _incident(rt, incident_id)
        gt = FAULT_LIBRARY[rt.injected_fault_id].failure_class if rt.injected_fault_id else None
        review = rt.coordinator.review(incident, gt)
        review = await rt.coordinator.enrich_review(incident, review)
        if rt.cloud.enabled:
            rt.cloud.persist_incident(rt.event_id, incident, review)
        return {"review": review.model_dump(mode="json"), **_incident_payload(rt, incident)}


@app.get("/api/incidents")
async def incidents() -> list[dict]:
    rt = get_runtime()
    return [
        {
            "incident_id": m.incident.incident_id,
            "title": m.incident.title,
            "state": m.incident.state.value,
            "severity": m.incident.severity.value,
            "opened_at": m.incident.opened_at.isoformat(),
            "affected_sessions": m.incident.scope.affected_sessions if m.incident.scope else 0,
            "audit_chain_valid": m.verify_audit_chain(),
        }
        for m in rt.coordinator.machines.values()
    ]


@app.get("/api/incident/{incident_id}")
async def incident(incident_id: str) -> dict:
    rt = get_runtime()
    return _incident_payload(rt, _incident(rt, incident_id))


@app.post("/api/incident/{incident_id}/ask")
async def ask_incident(
    incident_id: str,
    body: dict = Body(..., examples=[{"question": "why did you rule out a clock offset?"}]),
) -> dict:
    """Answer an operator's question about an open incident.

    Read-only by construction: the answer is composed from the typed incident
    record, and when the Gemini plane is configured it may retrieve more through
    the Grafana MCP server — the same audited path the investigation used. There
    is no route from this endpoint to the executor.
    """
    rt = get_runtime()
    inc = _incident(rt, incident_id)
    question = str(body.get("question", "")).strip()
    if not question:
        raise HTTPException(422, "a question is required")
    if len(question) > 500:
        raise HTTPException(422, "question is too long (500 characters maximum)")
    result = await ask_agent.answer(
        inc,
        question,
        mcp=rt.mcp,
        causal=rt.coordinator.causal.get(incident_id, []),
    )
    return result.as_dict()


@app.get("/api/incident/{incident_id}/replay")
async def replay(incident_id: str, window_s: float = Query(30.0)) -> dict:
    """Synchronised evidence replay: reference transcript against rendered captions."""
    rt = get_runtime()
    inc = _incident(rt, incident_id)
    scope = inc.scope
    language = scope.languages[0] if scope and scope.languages else "en"
    territory = scope.territories[0] if scope and scope.territories else "FR"
    pv = scope.player_versions[0] if scope and scope.player_versions else "ctv-9.4.0"
    obs = rt.sim.observe(language, territory, _platform_of(pv), pv, window_s)
    report = caption_probe.run(obs, language)
    drift = report.by_metric("cap.drift")
    return {
        "slice": {
            "language": language,
            "territory": territory,
            "player_version": pv,
            "platform": _platform_of(pv),
            "cdn_region": obs.cdn_region,
        },
        "window": [obs.window_start_s, obs.window_end_s],
        "reference": [{"t": round(t, 2), "token": tok} for t, tok in obs.reference_tokens],
        "cues": [
            {
                "start": round(c.start_s, 2),
                "end": round(c.end_s, 2),
                "text": c.text,
                "speaker": c.speaker,
                "language": c.language,
                "rendered": c.rendered,
            }
            for c in obs.cues
        ],
        "described": [
            {
                "start": round(w.start_s, 2),
                "end": round(w.end_s, 2),
                "target": round(w.target_scene_start, 2),
                "peak_dbfs": w.peak_dbfs,
                "language": w.language,
            }
            for w in obs.described
        ],
        "sign": obs.sign.__dict__ if obs.sign else None,
        "drift": {
            "seconds": drift.score if drift else 0.0,
            "confidence": drift.confidence if drift else 0.0,
            "detail": drift.detail if drift else {},
            "interval": drift.evidence_interval if drift else None,
        },
        "metrics": report.metrics,
        "environment": rt.sim.state_snapshot(),
    }


@app.get("/api/incident/{incident_id}/audit")
async def audit(incident_id: str) -> dict:
    rt = get_runtime()
    machine = rt.coordinator.machines.get(incident_id)
    if machine is None:
        raise HTTPException(404, incident_id)
    return {
        "chain_valid": machine.verify_audit_chain(),
        "events": [e.model_dump(mode="json") for e in machine.incident.audit],
    }


# ---------------------------------------------------------------------------
# MCP + observability
# ---------------------------------------------------------------------------


@app.get("/api/mcp")
async def mcp_info() -> dict:
    rt = get_runtime()
    from .grafana_mcp.client import CAPABILITIES

    return {
        "transport": rt.mcp.transport,
        "tools": rt.mcp.tool_names,
        "capabilities": [
            {
                "key": c.key,
                "required": c.required,
                "purpose": c.purpose,
                "resolved_to": rt.mcp.tool_for(c.key) if rt.mcp.has(c.key) else None,
            }
            for c in CAPABILITIES
        ],
        "calls": rt.mcp.call_log[-60:],
        "call_count": len(rt.mcp.call_log),
    }


@app.get("/api/agent-observability")
async def agent_observability() -> dict:
    rt = get_runtime()
    steps = rt.telemetry.agent_steps
    return {
        "steps": steps[-80:],
        "total_steps": len(steps),
        "total_duration_ms": round(sum(s["duration_ms"] for s in steps), 2),
        "by_agent": _group_durations(steps),
        "mcp_calls": len(rt.mcp.call_log),
        "mcp_total_ms": round(sum(c["duration_ms"] for c in rt.mcp.call_log), 2),
        "reasoning_mode": get_settings().reasoning_mode,
        "profiles": {s: rt.telemetry.profiles.fetch(s) for s in rt.telemetry.profiles.services()},
    }


@app.get("/api/logs")
async def logs(
    service: str | None = None, contains: str | None = None, limit: int = 60
) -> list[dict]:
    rt = get_runtime()
    lines = rt.telemetry.logs.query(
        {"service": service} if service else None, contains, limit=limit
    )
    return [{"ts": e.ts.isoformat(), "labels": e.labels, "line": e.line} for e in lines]


@app.get("/api/status-page", response_class=HTMLResponse)
async def status_page() -> HTMLResponse:
    """The audience-facing status page: plain language, screen-reader first."""
    rt = get_runtime()
    updates = []
    for m in rt.coordinator.machines.values():
        for c in m.incident.communications:
            if c.audience == "public_status":
                updates.append((m.incident, c))
    body = (
        "".join(
            f"<article aria-labelledby='u{i}'><h2 id='u{i}'>{c.subject}</h2>"
            f"<p>{'</p><p>'.join(c.body.split(chr(10) + chr(10)))}</p></article>"
            for i, (_, c) in enumerate(updates)
        )
        or "<p>All accessibility features are working normally.</p>"
    )
    return HTMLResponse(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Accessibility status - The Lumiere Protocol premiere</title>"
        "<style>body{font:1.15rem/1.7 system-ui,sans-serif;max-width:44rem;margin:2rem auto;"
        "padding:0 1rem;color:#111;background:#fff}h1{font-size:1.6rem}"
        "article{border-top:2px solid #333;padding-top:1rem;margin-top:2rem}"
        "@media(prefers-color-scheme:dark){body{background:#111;color:#f2f2f2}"
        "article{border-color:#888}}</style></head><body>"
        "<h1>Accessibility status</h1>"
        "<p>This page tells you whether captions, described audio, the sign-language "
        "window and the playback controls are working.</p>"
        f"<main>{body}</main></body></html>"
    )


# ---------------------------------------------------------------------------
# Benchmark results
# ---------------------------------------------------------------------------


@app.get("/api/benchmark")
async def benchmark() -> dict:
    path = get_settings().data_dir / "bench" / "summary.json"
    package_copy = Path(__file__).resolve().parent / "data" / "benchmark_summary.json"
    repo_copy = Path(__file__).resolve().parents[2] / "bench" / "results" / "summary.json"
    for candidate in (path, package_copy, repo_copy):
        if candidate.exists():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise HTTPException(404, "no benchmark results yet; run `raccord bench`")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _operator_identity(request: Request, claimed: ApproveRequest) -> tuple[str, Role]:
    """Resolve approval authority from trusted infrastructure outside demo mode.

    The request body is convenient theatre data in the isolated simulator. In a
    deployment it cannot assert either identity or role: IAP supplies identity
    and an operator-controlled mapping supplies roles.
    """
    settings = get_settings()
    if settings.demo_mode:
        try:
            return claimed.approver, Role(claimed.role)
        except ValueError as exc:
            raise HTTPException(400, f"unknown role: {claimed.role}") from exc

    raw_identity = request.headers.get(settings.trusted_identity_header, "").strip()
    if raw_identity.startswith("accounts.google.com:"):
        raw_identity = raw_identity.split(":", 1)[1]
    if not raw_identity:
        raise HTTPException(401, "authenticated operator identity is required")
    try:
        bindings = json.loads(settings.operator_role_bindings_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(503, "operator role bindings are invalid") from exc
    configured = bindings.get(raw_identity, []) if isinstance(bindings, dict) else []
    if isinstance(configured, str):
        configured = [configured]
    try:
        requested = Role(claimed.role)
        roles = {Role(value) for value in configured}
    except (TypeError, ValueError) as exc:
        raise HTTPException(403, "operator role binding is invalid") from exc
    if requested not in roles:
        raise HTTPException(403, f"{raw_identity} is not authorized as {requested.value}")
    return raw_identity, requested


def _incident(rt: RaccordRuntime, incident_id: str):
    machine = rt.coordinator.machines.get(incident_id)
    if machine is None:
        raise HTTPException(404, f"unknown incident {incident_id}")
    return machine.incident


def _incident_payload(rt: RaccordRuntime, incident) -> dict:
    machine = rt.coordinator.machines[incident.incident_id]
    causal = rt.coordinator.causal.get(incident.incident_id, [])
    review = rt.coordinator.reviews.get(incident.incident_id)
    legal = {s.value: machine.can(s)[0] for s in IncidentState}
    return {
        "incident": incident.model_dump(mode="json"),
        "state": incident.state.value,
        "legal_transitions": [k for k, v in legal.items() if v],
        "audit_chain_valid": machine.verify_audit_chain(),
        "quality_notes": rt.coordinator.quality_notes.get(incident.incident_id, []),
        "deep_link": rt.coordinator.deep_links.get(incident.incident_id),
        "causal_candidates": [
            {
                "change_id": c.change.change_id,
                "component": c.change.component,
                "kind": c.change.kind,
                "description": c.change.description,
                "at": c.change.at.isoformat(),
                "score": c.score,
                "supporting": c.supporting,
                "contradicting": c.contradicting,
            }
            for c in causal[:6]
        ],
        "review": review.model_dump(mode="json") if review else None,
        "mcp_calls": rt.mcp.call_log[-40:],
    }


def _scenario_payload(rt: RaccordRuntime, result) -> dict:
    payload = {
        "fault_id": result.fault_id,
        "ground_truth": result.ground_truth.value,
        "detected": result.detected,
        "diagnosis_correct": result.diagnosis_correct,
        "top_posterior": result.top_posterior,
        "recovered": result.recovered,
        "rolled_back": result.rolled_back,
        "action_taken": result.action_taken,
        "mcp_calls": result.mcp_calls,
        "scope_precision": result.scope_precision,
        "scope_recall": result.scope_recall,
        "assertions": [result.assertions_passing, result.assertions_total],
        "affected_sessions": result.affected_sessions,
        "protected_sessions": result.protected_sessions,
        "unsafe_action": result.unsafe_action,
        "error": result.error,
    }
    if result.incident is not None:
        payload.update(_incident_payload(rt, result.incident))
        # `_incident_payload` includes the recent call records for incident
        # inspection; scenario summaries keep the stable numeric contract.
        payload["mcp_call_log"] = payload.pop("mcp_calls")
        payload["mcp_calls"] = result.mcp_calls
    return payload


def _group_durations(steps: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for s in steps:
        row = out.setdefault(s["agent"], {"calls": 0, "ms": 0.0})
        row["calls"] += 1
        row["ms"] = round(row["ms"] + s["duration_ms"], 2)
    return out
