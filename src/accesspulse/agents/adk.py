"""Gemini reasoning plane, built with the Google Agent Development Kit.

Layering, deliberately:

* The deterministic agents in `core.py` and `evidence.py` produce the evidence,
  the ranking arithmetic, the policy decision and the verification result. Those
  are the operational facts and they are not model outputs.
* Gemini is given those typed records and asked to do what a language model is
  actually good at: synthesise a multimodal picture across metrics, logs,
  traces, probe findings and change events; explain what is uncertain and why;
  and write the six audience-specific communications in the right register.
* Gemini is exposed to the Grafana MCP tool surface through ADK's MCP toolset,
  so a follow-up question from an operator can pull one more piece of evidence
  through the same governed path.

Every tool Gemini can reach is read-only or already policy-gated. It cannot
execute a remediation action: `RemediationExecutor` requires a redeemed approval
token bound to an action hash, and no agent can mint one.

When `AP_REASONING_MODE=offline` (the default) this module is not imported at
call time and AccessPulse runs entirely on the deterministic core, which is what
makes the benchmark reproducible.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..config import get_settings
from ..contracts import Incident

SYNTHESIS_INSTRUCTION = """
You are the reasoning plane of AccessPulse, an accessible-experience reliability
platform for live media. You are given a typed incident record: the accessibility
promise that was made, the SLO that breached, the audience slices affected, the
evidence retrieved through the Grafana MCP server, the probe findings with their
confidence and abstention state, the ranked change-correlation candidates and the
deterministic diagnosis.

Your job:
1. Explain, in operator language, what the audience is actually experiencing.
2. State which evidence supports the leading hypothesis and which contradicts it.
3. Name explicitly what is still unknown, and what single piece of evidence would
   resolve it.
4. Never assert a cause the evidence does not support. If the leading posterior is
   below 0.4, say that the evidence is insufficient and recommend escalation.
5. Never infer anything about an individual viewer's disability or assistive
   technology. Talk about features and sessions, never about people's bodies.

Return JSON with keys: narrative, supporting, contradicting, unknowns,
recommended_next_evidence, confidence_statement.
""".strip()

COMMUNICATION_INSTRUCTION = """
You write the audience-specific communications for an accessibility incident.
You are given the approved incident record only. You may not invent facts, add
internal hostnames, name staff, or speculate about cause.

Write for each audience in its own register:
- operator: terse, imperative, what to watch next
- accessibility_specialist: precise about which promise, which threshold, which evidence
- technical_director: decision, risk, reversibility, blast radius
- viewer_support: what viewers may notice, what to say, what not to ask
- executive: impact, duration, protected sessions, systemic risk
- public_status: plain language, short sentences, one idea per sentence, no jargon,
  no colour-only status, written to be read aloud by a screen reader, and it must
  contain no internal detail whatsoever

Return JSON: {"audience": "...", "subject": "...", "body": "..."} for each.
""".strip()


@dataclass
class ReasoningResult:
    narrative: str
    supporting: list[str]
    contradicting: list[str]
    unknowns: list[str]
    recommended_next_evidence: str
    confidence_statement: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0


def available() -> bool:
    settings = get_settings()
    if not settings.gemini_available:
        return False
    try:
        import google.adk  # noqa: F401
        import google.genai  # noqa: F401
    except ImportError:
        return False
    return True


def build_mcp_toolset():
    """Expose the Grafana MCP server to Gemini through ADK's MCP toolset.

    Read-only capabilities only: the model may pull more evidence, never write.
    """
    from google.adk.tools.mcp_tool.mcp_toolset import (  # type: ignore
        MCPToolset,
        StdioServerParameters,
    )

    settings = get_settings()
    return MCPToolset(
        connection_params=StdioServerParameters(
            command=settings.mcp_stdio_command,
            args=settings.mcp_stdio_argv,
            env={
                "GRAFANA_URL": settings.grafana_url,
                "GRAFANA_SERVICE_ACCOUNT_TOKEN": settings.grafana_service_account_token,
            },
        ),
        tool_filter=[
            "list_datasources",
            "list_alert_rules",
            "get_alert_rule_by_uid",
            "query_prometheus",
            "query_loki_logs",
            "query_tempo_traces",
            "search_dashboards",
            "get_dashboard_by_uid",
            "generate_deeplink",
            "find_annotations",
        ],
    )


def build_agents():
    """Construct the ADK agent graph. Returns (synthesis_agent, comms_agent)."""
    from google.adk.agents import LlmAgent  # type: ignore

    settings = get_settings()
    model = settings.gemini_model

    synthesis = LlmAgent(
        name="accesspulse_synthesis",
        model=model,
        instruction=SYNTHESIS_INSTRUCTION,
        description=(
            "Synthesises multimodal accessibility incident evidence into an operator "
            "explanation with explicit uncertainty."
        ),
        tools=[build_mcp_toolset()],
    )
    comms = LlmAgent(
        name="accesspulse_communications",
        model=model,
        instruction=COMMUNICATION_INSTRUCTION,
        description="Writes role-specific incident communications from an approved record.",
    )
    return synthesis, comms


def incident_payload(incident: Incident, quality_notes: list[str],
                     causal: list) -> dict[str, Any]:
    """The typed record handed to Gemini. No raw telemetry, no PII, no free text."""
    scope = incident.scope
    return {
        "incident_id": incident.incident_id,
        "title": incident.title,
        "severity": incident.severity.value,
        "scope": scope.model_dump(mode="json") if scope else None,
        "alert": incident.alert.model_dump(mode="json") if incident.alert else None,
        "evidence": [
            {
                "id": e.evidence_id, "kind": e.kind.value, "tool": e.source_tool,
                "summary": e.summary, "query": e.query, "deep_link": e.deep_link,
            }
            for e in incident.evidence
        ],
        "findings": [
            {
                "metric": f.metric, "score": f.score, "unit": f.unit,
                "confidence": f.confidence, "abstained": f.abstained,
                "data_quality": f.data_quality, "limitations": list(f.known_limitations),
            }
            for f in incident.findings[:40]
        ],
        "probe_disagreement_notes": quality_notes,
        "change_candidates": [
            {
                "change_id": c.change.change_id, "component": c.change.component,
                "kind": c.change.kind, "description": c.change.description,
                "at": c.change.at.isoformat(), "score": c.score,
                "supporting": c.supporting, "contradicting": c.contradicting,
            }
            for c in causal[:5]
        ],
        "hypotheses": [h.model_dump(mode="json") for h in incident.hypotheses],
        "policy_decision": (
            incident.policy_decision.model_dump(mode="json")
            if incident.policy_decision else None
        ),
        "proposed_action": (
            incident.proposed_action.model_dump(mode="json")
            if incident.proposed_action else None
        ),
        "verification": [a.model_dump(mode="json") for a in incident.assertions],
    }


async def synthesise(incident: Incident, quality_notes: list[str],
                     causal: list) -> ReasoningResult | None:
    """Run the Gemini synthesis agent. Returns None when the plane is unavailable."""
    if not available():
        return None
    from google.adk.runners import InMemoryRunner  # type: ignore
    from google.genai import types  # type: ignore

    synthesis, _ = build_agents()
    runner = InMemoryRunner(agent=synthesis, app_name="accesspulse")
    session = await runner.session_service.create_session(
        app_name="accesspulse", user_id="accesspulse-coordinator"
    )
    payload = json.dumps(incident_payload(incident, quality_notes, causal), indent=2)
    message = types.Content(role="user", parts=[types.Part(text=payload)])

    text = ""
    tokens_in = tokens_out = 0
    async for event in runner.run_async(
        user_id="accesspulse-coordinator", session_id=session.id, new_message=message
    ):
        if event.usage_metadata:
            tokens_in += event.usage_metadata.prompt_token_count or 0
            tokens_out += event.usage_metadata.candidates_token_count or 0
        if event.is_final_response() and event.content and event.content.parts:
            text = "".join(p.text or "" for p in event.content.parts)

    data = _loads(text)
    return ReasoningResult(
        narrative=data.get("narrative", text[:2000]),
        supporting=list(data.get("supporting", [])),
        contradicting=list(data.get("contradicting", [])),
        unknowns=list(data.get("unknowns", [])),
        recommended_next_evidence=data.get("recommended_next_evidence", ""),
        confidence_statement=data.get("confidence_statement", ""),
        model=get_settings().gemini_model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )


def _loads(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        text = text.split("\n", 1)[1] if "\n" in text else text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


def deploy_to_agent_engine(staging_bucket: str, display_name: str = "accesspulse"):
    """Deploy the reasoning plane to Vertex AI Agent Engine.

    Called by `tools/deploy_agent_engine.py`; kept here so the agent definition
    and its deployment stay in one place.
    """
    import os

    import vertexai  # type: ignore
    from vertexai import agent_engines  # type: ignore
    from vertexai.preview import reasoning_engines  # type: ignore

    vertexai.init(
        project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
        location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
        staging_bucket=staging_bucket,
    )
    synthesis, _ = build_agents()
    app = reasoning_engines.AdkApp(agent=synthesis, enable_tracing=True)
    return agent_engines.create(
        agent_engine=app,
        display_name=display_name,
        requirements=[
            "google-cloud-aiplatform[agent_engines,adk]>=1.101.0",
            "google-adk>=1.0.0",
            "mcp>=1.2.0",
        ],
        extra_packages=["src/accesspulse"],
    )
