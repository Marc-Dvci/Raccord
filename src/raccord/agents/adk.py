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

When `RACCORD_REASONING_MODE=offline` (the default) this module is not imported at
call time and Raccord runs entirely on the deterministic core, which is what
makes the benchmark reproducible.
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..config import get_settings
from ..contracts import Communication, Incident, PostIncidentReview, ReasoningSynthesis

SYNTHESIS_INSTRUCTION = """
You are the reasoning plane of Raccord, an accessible-experience reliability
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
recommended_next_evidence, confidence_statement. `supporting` and
`contradicting` may contain only evidence ids present in the supplied record.
""".strip()

ASK_INSTRUCTION = """
You are the reasoning plane of Raccord, answering an operator who is looking
at a live incident and interrogating its diagnosis. You are given the typed
incident record and one question.

You hold the Grafana MCP toolset. If the record already answers the question,
answer from it. If it does not, **use the tools** to retrieve what is missing —
that is the point of you having them, and every call is audited on the same
Grafana timeline as the investigation itself.

Rules:
1. Answer the question that was asked, in two or three sentences. An operator is
   reading this during an incident, not afterwards.
2. Ground every claim in evidence. Cite the evidence ids you used, and say which
   Grafana tool produced a fact you fetched yourself.
3. If the evidence does not settle the question, say so plainly and name the one
   query that would.
4. Never assert a cause the evidence does not support, and never contradict the
   deterministic diagnosis without saying that is what you are doing.
5. Never infer anything about an individual viewer's disability or assistive
   technology. Talk about features and sessions, never about people's bodies.
6. You are read-only. You cannot change scope, policy, actions or verification.
   If asked to do any of those, say that it requires the approval path.

Return JSON with keys: answer (string), evidence_ids (array of strings),
fetched (array of strings naming any Grafana tool you called).
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

Return one JSON object with a `communications` array. Every array item must have
exactly these keys: audience, subject, body, internal, reading_level.
""".strip()

LEARNING_INSTRUCTION = """
You are Raccord's post-incident learning analyst. You receive a completed,
verified incident and a deterministic review. Find cross-cutting lessons that
typed rules alone are likely to miss. Do not rewrite the root cause, claim an
unverified fact, infer disability, or recommend removing approval/verification
controls. Propose small falsifiable experiments with a measurable success
criterion. Return JSON with keys: learning_narrative and proposed_experiments
(an array of at most five strings).
""".strip()

REMOTE_INSTRUCTION = f"""
You are Raccord's managed reasoning plane. The input JSON contains a `task` key.

For `synthesis`, follow these instructions:
{SYNTHESIS_INSTRUCTION}

For `communications`, follow these instructions:
{COMMUNICATION_INSTRUCTION}

For `ask`, follow these instructions:
{ASK_INSTRUCTION}

For `learning`, follow these instructions:
{LEARNING_INSTRUCTION}

Return only the JSON shape requested by the selected task. Deterministic facts,
policy, approval, remediation and verification are outside your authority.
""".strip()


class ReasoningUnavailable(RuntimeError):
    """Raised when Gemini mode is configured but its runtime is unusable."""


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


def require_available() -> None:
    """Fail closed when an operator explicitly selected Gemini mode."""
    settings = get_settings()
    if settings.reasoning_mode == "offline":
        return
    if not settings.gemini_available:
        raise ReasoningUnavailable(
            "RACCORD_REASONING_MODE=gemini requires GOOGLE_CLOUD_PROJECT or GOOGLE_API_KEY"
        )
    try:
        import google.adk  # noqa: F401
        import google.genai  # noqa: F401
    except ImportError as exc:
        raise ReasoningUnavailable(
            "Gemini mode requires the cloud image or `pip install .[cloud]`"
        ) from exc


READ_ONLY_MCP_TOOLS = [
    "list_datasources",
    "get_datasource",
    "get_datasource_by_uid",
    "get_datasource_by_name",
    "list_alert_rules",
    "get_alert_rule_by_uid",
    "query_prometheus",
    "list_prometheus_metric_names",
    "list_prometheus_label_values",
    "query_loki_logs",
    "query_loki_stats",
    "query_tempo_traces",
    "find_traces",
    "search_traces",
    "get_trace_by_id",
    "get_trace",
    "search_dashboards",
    "get_dashboard_by_uid",
    "generate_deeplink",
    "find_annotations",
    "get_annotations",
]


def _mcp_callbacks(observer):
    """Return callbacks that merge ADK-owned tool calls into Raccord telemetry."""
    if observer is None:
        return None, None, None
    starts: dict[tuple[str, str], float] = {}

    def key(tool, context) -> tuple[str, str]:
        invocation = getattr(context, "invocation_id", None) or str(id(context))
        return invocation, getattr(tool, "name", type(tool).__name__)

    async def before(tool, args, context):
        starts[key(tool, context)] = time.perf_counter()
        return None

    async def after(tool, args, context, result):
        duration = (
            time.perf_counter() - starts.pop(key(tool, context), time.perf_counter())
        ) * 1000
        observer.record_external_call(
            getattr(tool, "name", type(tool).__name__),
            dict(args),
            duration,
            True,
            len(str(result)),
        )
        return None

    async def on_error(tool, args, context, exc):
        duration = (
            time.perf_counter() - starts.pop(key(tool, context), time.perf_counter())
        ) * 1000
        observer.record_external_call(
            getattr(tool, "name", type(tool).__name__),
            dict(args),
            duration,
            False,
            0,
        )
        return None

    return before, after, on_error


def build_mcp_toolset(mcp_http_url: str | None = None):
    """Expose the Grafana MCP server to Gemini through ADK's MCP toolset.

    Read-only capabilities only: the model may pull more evidence, never write.
    """
    from google.adk.tools.mcp_tool.mcp_toolset import McpToolset  # type: ignore

    settings = get_settings()
    if settings.mcp_transport == "stub":
        return None

    if settings.mcp_transport == "http":
        from google.adk.tools.mcp_tool.mcp_session_manager import (  # type: ignore
            StreamableHTTPConnectionParams,
        )

        headers = {}
        if settings.mcp_grafana_url:
            headers["X-Grafana-URL"] = settings.mcp_grafana_url
        if settings.mcp_bearer_token:
            headers["Authorization"] = f"Bearer {settings.mcp_bearer_token}"
        connection = StreamableHTTPConnectionParams(
            url=mcp_http_url or settings.mcp_http_url,
            headers=headers,
            timeout=30,
            sse_read_timeout=120,
        )
    else:
        from google.adk.tools.mcp_tool.mcp_session_manager import (  # type: ignore
            StdioConnectionParams,
        )
        from mcp import StdioServerParameters

        server = StdioServerParameters(
            command=settings.mcp_stdio_command,
            args=settings.mcp_stdio_argv,
            env={
                "GRAFANA_URL": settings.grafana_url,
                "GRAFANA_SERVICE_ACCOUNT_TOKEN": settings.grafana_service_account_token,
            },
        )
        connection = StdioConnectionParams(server_params=server)

    return McpToolset(
        connection_params=connection,
        # Never expose generic or write-capable tools such as
        # alerting_manage_rules and grafana_api_request to Gemini.
        tool_filter=READ_ONLY_MCP_TOOLS,
    )


def build_agents(observer=None):
    """Construct the ADK agent graph. Returns (synthesis_agent, comms_agent)."""
    from google.adk.agents import LlmAgent  # type: ignore

    settings = get_settings()
    model = settings.gemini_model

    toolset = build_mcp_toolset()
    before_tool, after_tool, on_tool_error = _mcp_callbacks(observer)
    common_callbacks = (
        {
            "before_tool_callback": before_tool,
            "after_tool_callback": after_tool,
            "on_tool_error_callback": on_tool_error,
        }
        if toolset is not None and observer is not None
        else {}
    )
    from ..observability import with_google_adk_observability

    common_callbacks = with_google_adk_observability(common_callbacks)

    synthesis = LlmAgent(
        name="raccord_synthesis",
        model=model,
        instruction=SYNTHESIS_INSTRUCTION,
        description=(
            "Synthesises multimodal accessibility incident evidence into an operator "
            "explanation with explicit uncertainty."
        ),
        tools=[toolset] if toolset is not None else [],
        **common_callbacks,
    )
    comms = LlmAgent(
        name="raccord_communications",
        model=model,
        instruction=COMMUNICATION_INSTRUCTION,
        description="Writes role-specific incident communications from an approved record.",
        **common_callbacks,
    )
    return synthesis, comms


def build_agent_engine_agent(observer=None, mcp_http_url: str | None = None):
    """One managed dispatcher so every Gemini skill can use one Agent Engine."""
    from google.adk.agents import LlmAgent  # type: ignore

    settings = get_settings()
    toolset = build_mcp_toolset(mcp_http_url)
    before_tool, after_tool, on_tool_error = _mcp_callbacks(observer)
    callbacks = (
        {
            "before_tool_callback": before_tool,
            "after_tool_callback": after_tool,
            "on_tool_error_callback": on_tool_error,
        }
        if toolset is not None and observer is not None
        else {}
    )
    from ..observability import with_google_adk_observability

    callbacks = with_google_adk_observability(callbacks)
    return LlmAgent(
        name="raccord_reasoning",
        model=settings.gemini_model,
        instruction=REMOTE_INSTRUCTION,
        description="Managed synthesis, communication and operator Q&A for Raccord.",
        tools=[toolset] if toolset is not None else [],
        **callbacks,
    )


def incident_payload(incident: Incident, quality_notes: list[str], causal: list) -> dict[str, Any]:
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
                "id": e.evidence_id,
                "kind": e.kind.value,
                "tool": e.source_tool,
                "summary": e.summary,
                "query": e.query,
                "deep_link": e.deep_link,
            }
            for e in incident.evidence
        ],
        "findings": [
            {
                "metric": f.metric,
                "score": f.score,
                "unit": f.unit,
                "confidence": f.confidence,
                "abstained": f.abstained,
                "data_quality": f.data_quality,
                "limitations": list(f.known_limitations),
            }
            for f in incident.findings[:40]
        ],
        "probe_disagreement_notes": quality_notes,
        "change_candidates": [
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
            for c in causal[:5]
        ],
        "hypotheses": [h.model_dump(mode="json") for h in incident.hypotheses],
        "policy_decision": (
            incident.policy_decision.model_dump(mode="json") if incident.policy_decision else None
        ),
        "proposed_action": (
            incident.proposed_action.model_dump(mode="json") if incident.proposed_action else None
        ),
        "verification": [a.model_dump(mode="json") for a in incident.assertions],
    }


def _validate_evidence_ids(incident: Incident, values: Any, field: str) -> tuple[str, ...]:
    if not isinstance(values, list):
        raise ReasoningUnavailable(f"Gemini returned invalid {field}")
    result = tuple(str(value) for value in values)
    known = {item.evidence_id for item in incident.evidence}
    invalid = sorted(set(result) - known)
    if invalid:
        raise ReasoningUnavailable(f"Gemini cited unknown evidence ids in {field}: {invalid}")
    return result


def _public_detail_leaks(incident: Incident, subject: str, body: str) -> list[str]:
    """Deterministically reject internal identifiers from model-written public text."""
    candidates = {
        incident.incident_id,
        *(incident.scope.components if incident.scope else ()),
        *(incident.scope.player_versions if incident.scope else ()),
    }
    if incident.proposed_action:
        candidates.add(incident.proposed_action.target)
    candidates.update({"capenc", "signsrc", "pool-", "origin-", "pv-", "eu-west"})
    text = f"{subject}\n{body}".lower()
    return sorted(value for value in candidates if value and value.lower() in text)


async def _run_local(agent, payload: dict[str, Any], user_id: str) -> tuple[str, int, int]:
    """Run an ADK agent in-process against Vertex AI."""
    from google.adk.runners import InMemoryRunner  # type: ignore
    from google.genai import types  # type: ignore

    runner = InMemoryRunner(agent=agent, app_name="raccord")
    session = await runner.session_service.create_session(app_name="raccord", user_id=user_id)
    message = types.Content(role="user", parts=[types.Part(text=json.dumps(payload, indent=2))])

    text = ""
    tokens_in = tokens_out = 0
    async for event in runner.run_async(
        user_id=user_id, session_id=session.id, new_message=message
    ):
        if event.usage_metadata:
            tokens_in += event.usage_metadata.prompt_token_count or 0
            tokens_out += event.usage_metadata.candidates_token_count or 0
        if event.is_final_response() and event.content and event.content.parts:
            text = "".join(p.text or "" for p in event.content.parts)
    return text, tokens_in, tokens_out


def _remote_event_text(event: Any) -> str:
    """Extract text from either SDK event objects or Agent Engine dictionaries."""
    if not isinstance(event, dict):
        content = getattr(event, "content", None)
        return "".join(getattr(p, "text", "") or "" for p in (content.parts if content else []))
    content = event.get("content") or {}
    return "".join((part.get("text") or "") for part in content.get("parts", []))


async def _run_remote(payload: dict[str, Any], user_id: str) -> tuple[str, int, int]:
    """Query the configured Vertex AI Agent Engine resource."""
    import os

    import vertexai  # type: ignore
    from vertexai import agent_engines  # type: ignore

    settings = get_settings()
    vertexai.init(
        project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
        location=settings.agent_engine_location,
    )
    engine = agent_engines.get(settings.agent_engine_resource)
    method = getattr(engine, "async_stream_query", None)
    if method is None:
        raise ReasoningUnavailable(
            "the configured Agent Engine does not advertise async_stream_query"
        )
    text = ""
    tokens_in = tokens_out = 0
    async for event in method(message=json.dumps(payload), user_id=user_id):
        chunk = _remote_event_text(event)
        if chunk:
            text = chunk
        usage = event.get("usage_metadata", {}) if isinstance(event, dict) else {}
        tokens_in += int(usage.get("prompt_token_count", 0) or 0)
        tokens_out += int(usage.get("candidates_token_count", 0) or 0)
    return text, tokens_in, tokens_out


async def _run_task(
    task: str,
    payload: dict[str, Any],
    local_agent,
    user_id: str,
) -> tuple[str, int, int, str]:
    require_available()
    settings = get_settings()
    request = {"task": task, **payload}
    if settings.agent_engine_resource:
        text, tokens_in, tokens_out = await _run_remote(request, user_id)
        return text, tokens_in, tokens_out, "agent-engine"
    text, tokens_in, tokens_out = await _run_local(local_agent, request, user_id)
    return text, tokens_in, tokens_out, "vertex-direct"


async def synthesise(
    incident: Incident, quality_notes: list[str], causal: list, observer=None
) -> ReasoningSynthesis | None:
    """Run Gemini over the settled facts without granting it authority."""
    if get_settings().reasoning_mode == "offline":
        return None
    synthesis, _ = build_agents(observer)
    text, tokens_in, tokens_out, runtime = await _run_task(
        "synthesis",
        {"incident": incident_payload(incident, quality_notes, causal)},
        synthesis,
        "raccord-coordinator",
    )

    data = _loads(text)
    return ReasoningSynthesis(
        narrative=data.get("narrative", text[:2000]),
        supporting=_validate_evidence_ids(incident, data.get("supporting", []), "supporting"),
        contradicting=_validate_evidence_ids(
            incident, data.get("contradicting", []), "contradicting"
        ),
        unknowns=tuple(data.get("unknowns", [])),
        recommended_next_evidence=data.get("recommended_next_evidence", ""),
        confidence_statement=data.get("confidence_statement", ""),
        model=get_settings().gemini_model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        runtime=runtime,
    )


async def generate_communications(
    incident: Incident,
    deterministic: list[Communication],
    observer=None,
) -> tuple[list[Communication], dict[str, Any]] | None:
    """Let Gemini rewrite approved drafts, then validate every returned field."""
    if get_settings().reasoning_mode == "offline":
        return None
    _, comms_agent = build_agents(observer)
    text, tokens_in, tokens_out, runtime = await _run_task(
        "communications",
        {
            "incident": incident_payload(incident, [], []),
            "approved_drafts": [c.model_dump(mode="json") for c in deterministic],
        },
        comms_agent,
        "raccord-communications",
    )
    data = _loads(text)
    rows = data.get("communications")
    if not isinstance(rows, list):
        raise ReasoningUnavailable("Gemini returned no communications array")
    allowed = {c.audience for c in deterministic}
    generated: list[Communication] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("audience") not in allowed:
            raise ReasoningUnavailable("Gemini returned an unknown communication audience")
        subject = str(row.get("subject", ""))[:200].strip()
        body = str(row.get("body", ""))[:8000].strip()
        if not subject or not body:
            raise ReasoningUnavailable("Gemini returned an empty communication")
        if row["audience"] == "public_status" and row.get("internal"):
            raise ReasoningUnavailable("Gemini marked the public update as internal")
        leaks = _public_detail_leaks(incident, subject, body)
        if row["audience"] == "public_status" and leaks:
            raise ReasoningUnavailable(
                f"Gemini leaked internal identifiers into the public update: {leaks}"
            )
        generated.append(
            Communication(
                communication_id=f"gem-{incident.incident_id}-{row['audience']}",
                incident_id=incident.incident_id,
                audience=row["audience"],
                subject=subject,
                body=body,
                contains_internal_detail=row["audience"] != "public_status",
                reading_level_note=str(row.get("reading_level", ""))[:80],
            )
        )
    if len(generated) != len(allowed) or {c.audience for c in generated} != allowed:
        raise ReasoningUnavailable("Gemini duplicated or omitted a required audience")
    return generated, {
        "model": get_settings().gemini_model,
        "runtime": runtime,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
    }


async def ask(incident: Incident, question: str, observer=None) -> dict:
    """Answer one operator question about an incident, with MCP tools in hand.

    Unlike `synthesise`, which runs once on a settled record, this is the path an
    operator drives interactively — so the agent is expected to reach for the
    Grafana MCP toolset when the retrieved evidence does not already answer what
    was asked.
    """
    from google.adk.agents import LlmAgent  # type: ignore

    settings = get_settings()
    require_available()
    toolset = build_mcp_toolset()
    before_tool, after_tool, on_tool_error = _mcp_callbacks(observer)
    callbacks = (
        {
            "before_tool_callback": before_tool,
            "after_tool_callback": after_tool,
            "on_tool_error_callback": on_tool_error,
        }
        if toolset is not None and observer is not None
        else {}
    )
    from ..observability import with_google_adk_observability

    callbacks = with_google_adk_observability(callbacks)
    agent = LlmAgent(
        name="raccord_operator_qa",
        model=settings.gemini_model,
        instruction=ASK_INSTRUCTION,
        description="Answers operator questions about a live accessibility incident.",
        tools=[toolset] if toolset is not None else [],
        **callbacks,
    )
    text, tokens_in, tokens_out, runtime = await _run_task(
        "ask",
        {"question": question, "incident": incident_payload(incident, [], [])},
        agent,
        "raccord-operator",
    )

    data = _loads(text)
    evidence_ids = list(data.get("evidence_ids", []))
    _validate_evidence_ids(incident, evidence_ids, "evidence_ids")
    fetched = list(data.get("fetched", []))
    invalid_tools = sorted(set(fetched) - set(READ_ONLY_MCP_TOOLS))
    if invalid_tools:
        raise ReasoningUnavailable(f"Gemini reported unknown/non-read-only tools: {invalid_tools}")
    # Remote Agent Engine callbacks run in another process. Reconcile the
    # model's explicit fetched list into the local operator timeline.
    if settings.agent_engine_resource and observer is not None:
        for tool in fetched:
            observer.record_external_call(str(tool), {}, 0.0, True, 0, "agent-engine-mcp")
    return {
        "answer": data.get("answer") or text[:2000] or "No answer was returned.",
        "evidence_ids": evidence_ids,
        "fetched": fetched,
        "model": settings.gemini_model,
        "runtime": runtime,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
    }


async def analyse_review(
    incident: Incident,
    review: PostIncidentReview,
    observer=None,
) -> tuple[PostIncidentReview, dict[str, Any]]:
    """Add non-authoritative Gemini learning to the deterministic review."""
    if get_settings().reasoning_mode == "offline":
        return review, {
            "model": "offline",
            "runtime": "offline",
            "tokens_in": 0,
            "tokens_out": 0,
        }
    from google.adk.agents import LlmAgent  # type: ignore

    settings = get_settings()
    toolset = build_mcp_toolset()
    before_tool, after_tool, on_tool_error = _mcp_callbacks(observer)
    callbacks = (
        {
            "before_tool_callback": before_tool,
            "after_tool_callback": after_tool,
            "on_tool_error_callback": on_tool_error,
        }
        if toolset is not None and observer is not None
        else {}
    )
    from ..observability import with_google_adk_observability

    callbacks = with_google_adk_observability(callbacks)
    agent = LlmAgent(
        name="raccord_learning",
        model=settings.gemini_model,
        instruction=LEARNING_INSTRUCTION,
        description="Finds falsifiable reliability lessons after verified recovery.",
        tools=[toolset] if toolset is not None else [],
        **callbacks,
    )
    text, tokens_in, tokens_out, runtime = await _run_task(
        "learning",
        {
            "incident": incident_payload(incident, [], []),
            "deterministic_review": review.model_dump(mode="json"),
        },
        agent,
        "raccord-learning",
    )
    data = _loads(text)
    experiments = data.get("proposed_experiments", [])
    if not isinstance(experiments, list):
        raise ReasoningUnavailable("Gemini returned invalid proposed_experiments")
    return review.model_copy(
        update={
            "learning_narrative": str(data.get("learning_narrative", text[:2000]))[:4000],
            "proposed_experiments": tuple(str(item)[:500] for item in experiments[:5]),
            "reasoning_model": settings.gemini_model,
            "reasoning_runtime": runtime,
        }
    ), {
        "model": settings.gemini_model,
        "runtime": runtime,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
    }


def _loads(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        text = text.split("\n", 1)[1] if "\n" in text else text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


def deploy_to_agent_engine(
    staging_bucket: str,
    display_name: str = "raccord",
    service_account: str | None = None,
    mcp_url: str | None = None,
    mcp_token_secret: str | None = None,
    agento11y_token_secret: str | None = None,
    min_instances: int = 0,
    max_instances: int = 1,
):
    """Deploy the reasoning plane to Vertex AI Agent Engine.

    Called by `tools/deploy_agent_engine.py`; kept here so the agent definition
    and its deployment stay in one place.
    """
    import os

    import vertexai  # type: ignore
    from vertexai import agent_engines  # type: ignore

    vertexai.init(
        project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
        location=get_settings().agent_engine_location,
        staging_bucket=staging_bucket,
    )
    settings = get_settings()
    resolved_mcp_url = mcp_url or settings.mcp_http_url
    agent = build_agent_engine_agent(mcp_http_url=resolved_mcp_url)
    app = agent_engines.AdkApp(agent=agent, enable_tracing=True)
    env_vars: dict[str, Any] = {
        "RACCORD_REASONING_MODE": "gemini",
        "RACCORD_GEMINI_MODEL": settings.gemini_model,
        "RACCORD_GEMINI_LOCATION": settings.gemini_location,
        "GOOGLE_CLOUD_PROJECT": os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
        "GOOGLE_CLOUD_LOCATION": settings.gemini_location,
        "GOOGLE_GENAI_USE_VERTEXAI": "TRUE",
        "RACCORD_MCP_TRANSPORT": settings.mcp_transport,
        "RACCORD_MCP_HTTP_URL": resolved_mcp_url,
        "RACCORD_MCP_GRAFANA_URL": settings.mcp_grafana_url,
        "RACCORD_GRAFANA_URL": settings.grafana_url,
    }
    if mcp_token_secret or agento11y_token_secret:
        from google.cloud.aiplatform_v1.types.env_var import SecretRef  # type: ignore

    if mcp_token_secret:
        env_vars["RACCORD_MCP_BEARER_TOKEN"] = SecretRef(
            secret=mcp_token_secret,
            version="latest",
        )
    agento11y_endpoint = os.environ.get("AGENTO11Y_ENDPOINT", "")
    otlp_endpoint = settings.otlp_endpoint
    if agento11y_endpoint:
        env_vars.update(
            {
                "AGENTO11Y_ENDPOINT": agento11y_endpoint,
                "AGENTO11Y_PROTOCOL": os.environ.get("AGENTO11Y_PROTOCOL", "http"),
                "AGENTO11Y_AUTH_MODE": os.environ.get("AGENTO11Y_AUTH_MODE", "basic"),
                "AGENTO11Y_AUTH_TENANT_ID": os.environ.get(
                    "AGENTO11Y_AUTH_TENANT_ID", settings.otlp_username
                ),
                "RACCORD_OTLP_ENDPOINT": otlp_endpoint,
                "RACCORD_OTLP_USERNAME": settings.otlp_username,
            }
        )
        if agento11y_token_secret:
            token_ref = SecretRef(secret=agento11y_token_secret, version="latest")
            env_vars["AGENTO11Y_AUTH_TOKEN"] = token_ref
            env_vars["RACCORD_OTLP_AUTH_TOKEN"] = token_ref
    return agent_engines.create(
        agent_engine=app,
        display_name=display_name,
        requirements=[
            "google-cloud-aiplatform>=1.163,<2",
            "google-adk[mcp]>=2.6,<3",
            "google-genai>=2.17,<3",
            "agento11y>=0.16,<1",
            "agento11y-google-adk>=0.16,<1",
            "opentelemetry-sdk>=1.25",
            "opentelemetry-exporter-otlp-proto-http>=1.25",
            "mcp>=1.29,<2",
            "pydantic>=2.13,<3",
            "pydantic-settings>=2.15,<3",
            "httpx>=0.28,<1",
            "prometheus-client>=0.26,<1",
        ],
        extra_packages=["src/raccord"],
        env_vars=env_vars,
        service_account=service_account,
        min_instances=min_instances,
        max_instances=max_instances,
    )
