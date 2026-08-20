"""Cloud evidence adapters and the production identity trust boundary."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from raccord.api import ApproveRequest, _operator_identity
from raccord.cloud import CloudEvidenceSink
from raccord.config import Settings
from raccord.contracts import FailureClass, Incident, PostIncidentReview, Role


class _Future:
    def result(self, timeout):
        assert timeout == 10
        return "message-1"


class _Publisher:
    def __init__(self):
        self.sent = []

    def topic_path(self, project, topic):
        return f"projects/{project}/topics/{topic}"

    def publish(self, topic, data, **attributes):
        self.sent.append((topic, json.loads(data), attributes))
        return _Future()


class _Blob:
    def __init__(self, name, uploads):
        self.name = name
        self.uploads = uploads

    def upload_from_string(self, data, **kwargs):
        self.uploads.append((self.name, json.loads(data), kwargs))


class _Bucket:
    def __init__(self, uploads):
        self.uploads = uploads

    def blob(self, name):
        return _Blob(name, self.uploads)


class _Storage:
    def __init__(self):
        self.uploads = []

    def bucket(self, name):
        assert name == "evidence-bucket"
        return _Bucket(self.uploads)


class _BigQuery:
    def __init__(self):
        self.inserts = []

    def insert_rows_json(self, table, rows, row_ids):
        self.inserts.append((table, rows, row_ids))
        return []


def _settings(**changes):
    return Settings(
        data_dir="var/test-cloud",
        google_cloud_project="project-1",
        evidence_bucket="evidence-bucket",
        probe_findings_topic="probe-findings",
        analytics_dataset="raccord_analytics",
        **changes,
    )


def _review(incident_id):
    return PostIncidentReview(
        incident_id=incident_id,
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


def test_cloud_sink_uses_all_provisioned_services_without_person_data():
    publisher, storage, bigquery = _Publisher(), _Storage(), _BigQuery()
    sink = CloudEvidenceSink(
        _settings(),
        publisher=publisher,
        storage_client=storage,
        bigquery_client=bigquery,
        clock=lambda: datetime(2026, 8, 9, tzinfo=timezone.utc),
    )
    assert sink.publish_tick(
        "event-1",
        {
            "program_s": 45,
            "series_published": 12,
            "evaluations": 9,
            "breached_slos": ["cap.drift"],
        },
    )
    incident = Incident(incident_id="inc-1", event_id="event-1", title="Drift")
    result = sink.persist_incident("event-1", incident, _review("inc-1"))

    assert result == {"storage": True, "bigquery": True}
    assert publisher.sent[0][0] == "projects/project-1/topics/probe-findings"
    assert "viewer" not in json.dumps(publisher.sent[0][1]).lower()
    assert storage.uploads[0][2]["if_generation_match"] == 0
    assert bigquery.inserts[0][0] == "project-1.raccord_analytics.incident_outcomes"
    assert bigquery.inserts[0][2] == ["inc-1"]


def _request(email: str | None = None):
    headers = []
    if email:
        headers.append((b"x-goog-authenticated-user-email", email.encode()))
    return Request({"type": "http", "headers": headers})


def test_production_identity_ignores_claimed_name_and_requires_bound_role(monkeypatch):
    settings = Settings(
        data_dir="var/test-auth",
        demo_mode=False,
        operator_role_bindings_json=json.dumps(
            {"director@example.com": [Role.TECHNICAL_DIRECTOR.value]}
        ),
    )
    monkeypatch.setattr("raccord.api.get_settings", lambda: settings)
    identity, role = _operator_identity(
        _request("accounts.google.com:director@example.com"),
        ApproveRequest(approver="attacker@example.com", role=Role.TECHNICAL_DIRECTOR.value),
    )
    assert identity == "director@example.com"
    assert role is Role.TECHNICAL_DIRECTOR


def test_production_identity_refuses_missing_header_and_role_escalation(monkeypatch):
    settings = Settings(
        data_dir="var/test-auth-refusal",
        demo_mode=False,
        operator_role_bindings_json=json.dumps({"support@example.com": [Role.SUPPORT_LEAD.value]}),
    )
    monkeypatch.setattr("raccord.api.get_settings", lambda: settings)
    with pytest.raises(HTTPException) as missing:
        _operator_identity(_request(), ApproveRequest())
    assert missing.value.status_code == 401
    with pytest.raises(HTTPException) as escalated:
        _operator_identity(
            _request("support@example.com"),
            ApproveRequest(role=Role.TECHNICAL_DIRECTOR.value),
        )
    assert escalated.value.status_code == 403


def test_strict_cloud_persistence_surfaces_an_outage():
    class BrokenPublisher:
        def topic_path(self, project, topic):
            return f"projects/{project}/topics/{topic}"

        def publish(self, *args, **kwargs):
            raise RuntimeError("Pub/Sub unavailable")

    sink = CloudEvidenceSink(
        _settings(cloud_persistence_strict=True),
        publisher=BrokenPublisher(),
    )
    with pytest.raises(RuntimeError, match="Pub/Sub unavailable"):
        sink.publish_tick("event", {"breached_slos": []})
    assert sink.writes[-1].ok is False
