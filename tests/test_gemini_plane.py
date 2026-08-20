"""Gemini/ADK integration without network or credentials."""

from __future__ import annotations

import json

import pytest

from raccord.agents import adk
from raccord.config import Settings
from raccord.contracts import (
    Communication,
    Evidence,
    EvidenceKind,
    FailureClass,
    Incident,
    PostIncidentReview,
)


def _settings(**changes):
    defaults = {
        "data_dir": "var/test-gemini",
        "reasoning_mode": "gemini",
        "mcp_transport": "http",
        "mcp_http_url": "https://mcp.example/mcp",
        "mcp_grafana_url": "https://grafana.example",
        "grafana_service_account_token": "secret",
        "mcp_bearer_token": "gateway-secret",
    }
    defaults.update(changes)
    return Settings(**defaults)


def test_http_mcp_toolset_is_remote_authenticated_and_read_only(monkeypatch):
    monkeypatch.setattr(adk, "get_settings", lambda: _settings())
    toolset = adk.build_mcp_toolset()
    connection = toolset._connection_params
    assert type(connection).__name__ == "StreamableHTTPConnectionParams"
    assert connection.url == "https://mcp.example/mcp"
    assert connection.headers["Authorization"] == "Bearer gateway-secret"
    assert connection.headers["Authorization"] != "Bearer secret"
    assert connection.headers["X-Grafana-URL"] == "https://grafana.example"
    assert "query_prometheus" in toolset.tool_filter
    assert "grafana_api_request" not in toolset.tool_filter
    assert "alerting_manage_rules" not in toolset.tool_filter


async def test_agent_engine_resource_routes_every_task_remotely(monkeypatch):
    settings = _settings(agent_engine_resource="projects/p/locations/l/reasoningEngines/1")
    monkeypatch.setattr(adk, "get_settings", lambda: settings)
    monkeypatch.setattr(adk, "require_available", lambda: None)
    seen = []

    async def remote(payload, user_id):
        seen.append((payload, user_id))
        return '{"answer":"grounded","evidence_ids":[],"fetched":[]}', 3, 4

    monkeypatch.setattr(adk, "_run_remote", remote)
    text, tokens_in, tokens_out, runtime = await adk._run_task(
        "ask", {"question": "why"}, object(), "operator"
    )
    assert json.loads(text)["answer"] == "grounded"
    assert (tokens_in, tokens_out, runtime) == (3, 4, "agent-engine")
    assert seen[0][0]["task"] == "ask"


async def test_synthesis_and_all_communications_are_typed(monkeypatch):
    settings = _settings(mcp_transport="stub")
    monkeypatch.setattr(adk, "get_settings", lambda: settings)
    monkeypatch.setattr(adk, "build_agents", lambda observer=None: (object(), object()))
    responses = [
        json.dumps(
            {
                "narrative": "Captions are drifting for connected-TV sessions.",
                "supporting": ["ev-1"],
                "contradicting": [],
                "unknowns": ["provider clock state"],
                "recommended_next_evidence": "query encoder clock telemetry",
                "confidence_statement": "High, bounded by the available slices.",
            }
        ),
        json.dumps(
            {
                "communications": [
                    {
                        "audience": audience,
                        "subject": f"Update for {audience}",
                        "body": "Verified information only.",
                        "internal": audience != "public_status",
                        "reading_level": "plain",
                    }
                    for audience in (
                        "operator",
                        "accessibility_specialist",
                        "technical_director",
                        "viewer_support",
                        "executive",
                        "public_status",
                    )
                ]
            }
        ),
    ]

    async def run_task(task, payload, local_agent, user_id):
        return responses.pop(0), 11, 7, "agent-engine"

    monkeypatch.setattr(adk, "_run_task", run_task)
    incident = Incident(
        incident_id="inc-gem",
        event_id="event",
        title="Drift",
        evidence=[
            Evidence(
                evidence_id="ev-1",
                incident_id="inc-gem",
                kind=EvidenceKind.PROM_QUERY,
                source_tool="grafana.mcp:query_prometheus",
            )
        ],
    )
    synthesis = await adk.synthesise(incident, [], [])
    assert synthesis.runtime == "agent-engine"
    assert synthesis.supporting == ("ev-1",)
    drafts = [
        Communication(
            communication_id=f"d-{audience}",
            incident_id=incident.incident_id,
            audience=audience,
            subject="Draft",
            body="Draft",
            contains_internal_detail=audience != "public_status",
        )
        for audience in (
            "operator",
            "accessibility_specialist",
            "technical_director",
            "viewer_support",
            "executive",
            "public_status",
        )
    ]
    communications, meta = await adk.generate_communications(incident, drafts)
    assert {c.audience for c in communications} == {c.audience for c in drafts}
    assert meta["runtime"] == "agent-engine"


async def test_gemini_public_copy_cannot_leak_internal_identifiers(monkeypatch):
    settings = _settings(mcp_transport="stub")
    monkeypatch.setattr(adk, "get_settings", lambda: settings)
    monkeypatch.setattr(adk, "build_agents", lambda observer=None: (object(), object()))

    async def run_task(task, payload, local_agent, user_id):
        return (
            json.dumps(
                {
                    "communications": [
                        {
                            "audience": "public_status",
                            "subject": "Caption update",
                            "body": "We switched capenc-pool-a and service is restored.",
                            "internal": False,
                            "reading_level": "plain",
                        }
                    ]
                }
            ),
            1,
            1,
            "agent-engine",
        )

    monkeypatch.setattr(adk, "_run_task", run_task)
    incident = Incident(incident_id="inc-leak", event_id="event", title="Drift")
    draft = Communication(
        communication_id="draft",
        incident_id=incident.incident_id,
        audience="public_status",
        subject="Update",
        body="Captions are restored.",
    )
    with pytest.raises(adk.ReasoningUnavailable, match="leaked internal identifiers"):
        await adk.generate_communications(incident, [draft])


def test_gemini_mode_fails_closed_without_credentials(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setattr(adk, "get_settings", lambda: _settings())
    with pytest.raises(adk.ReasoningUnavailable, match="GOOGLE_CLOUD_PROJECT"):
        adk.require_available()


async def test_post_incident_learning_preserves_deterministic_root_cause(monkeypatch):
    settings = _settings(mcp_transport="stub")
    monkeypatch.setattr(adk, "get_settings", lambda: settings)

    async def run_task(task, payload, local_agent, user_id):
        assert task == "learning"
        return (
            json.dumps(
                {
                    "learning_narrative": "Clock failover needs a progressive-drift canary.",
                    "proposed_experiments": [
                        "Inject failover and require detection within 45 seconds."
                    ],
                }
            ),
            8,
            5,
            "agent-engine",
        )

    monkeypatch.setattr(adk, "_run_task", run_task)
    monkeypatch.setattr(adk, "build_mcp_toolset", lambda: None)
    incident = Incident(incident_id="inc-learn", event_id="event", title="Drift")
    review = PostIncidentReview(
        incident_id=incident.incident_id,
        root_cause=FailureClass.CAPTION_PROGRESSIVE_DRIFT,
        contributing_factors=(),
        time_to_detect_s=10,
        time_to_scope_s=1,
        time_to_evidence_s=2,
        time_to_approval_s=3,
        time_to_recovery_s=4,
        error_budget_consumed=0.1,
        affected_sessions=250,
        protected_sessions=100,
        missed_signals=(),
        unnecessary_tool_calls=0,
        diagnosis_correct=True,
        verification_complete=True,
        proposed_improvements=(),
    )
    learned, meta = await adk.analyse_review(incident, review)
    assert learned.root_cause is review.root_cause
    assert learned.proposed_experiments
    assert learned.reasoning_runtime == "agent-engine"
    assert meta["tokens_out"] == 5


def test_agent_engine_deployment_uses_current_adk_app_and_custom_identity(monkeypatch):
    from vertexai import agent_engines

    settings = _settings(mcp_transport="stub")
    monkeypatch.setattr(adk, "get_settings", lambda: settings)
    built = {}

    def build_agent(mcp_http_url=None):
        built["mcp_http_url"] = mcp_http_url
        return object()

    monkeypatch.setattr(adk, "build_agent_engine_agent", build_agent)
    init = {}
    created = {}

    monkeypatch.setattr("vertexai.init", lambda **kwargs: init.update(kwargs))

    class FakeApp:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(agent_engines, "AdkApp", FakeApp)
    monkeypatch.setattr(
        agent_engines,
        "create",
        lambda **kwargs: created.update(kwargs) or "engine",
    )
    result = adk.deploy_to_agent_engine(
        "gs://staging",
        service_account="raccord-reasoning@project.iam.gserviceaccount.com",
        mcp_url="https://raccord-mcp.example/mcp",
        mcp_token_secret="projects/project/secrets/raccord-mcp-token",
    )
    assert result == "engine"
    assert init["location"] == "us-central1"
    assert isinstance(created["agent_engine"], FakeApp)
    assert created["service_account"].startswith("raccord-reasoning@")
    assert created["env_vars"]["GOOGLE_CLOUD_LOCATION"] == "global"
    assert built["mcp_http_url"] == "https://raccord-mcp.example/mcp"
    assert created["env_vars"]["RACCORD_MCP_HTTP_URL"] == "https://raccord-mcp.example/mcp"
    assert (
        created["env_vars"]["RACCORD_MCP_BEARER_TOKEN"].secret
        == "projects/project/secrets/raccord-mcp-token"
    )
