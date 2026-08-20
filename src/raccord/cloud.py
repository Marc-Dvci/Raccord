"""Optional Google Cloud evidence persistence.

The local product never imports a Google SDK.  A deployed instance can publish
de-identified live summaries to Pub/Sub and persist completed incident bundles
to create-only Cloud Storage plus aggregate BigQuery analytics.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .config import Settings, get_settings
from .contracts import Incident, PostIncidentReview

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class CloudWrite:
    target: str
    ok: bool
    detail: str = ""


class CloudEvidenceSink:
    """Lazy, injectable adapters for the three provisioned evidence services."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        publisher: Any | None = None,
        storage_client: Any | None = None,
        bigquery_client: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._publisher = publisher
        self._storage = storage_client
        self._bigquery = bigquery_client
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.writes: list[CloudWrite] = []

    @property
    def enabled(self) -> bool:
        return self.settings.cloud_evidence_enabled

    def _handle(self, target: str, operation: Callable[[], str]) -> bool:
        try:
            detail = operation()
        except Exception as exc:  # cloud outages must be visible, never disguised
            self.writes.append(CloudWrite(target, False, f"{type(exc).__name__}: {exc}"))
            LOG.exception("Raccord cloud evidence write failed: %s", target)
            if self.settings.cloud_persistence_strict:
                raise
            return False
        self.writes.append(CloudWrite(target, True, detail))
        return True

    def _publisher_client(self):
        if self._publisher is None:
            from google.cloud import pubsub_v1  # type: ignore

            self._publisher = pubsub_v1.PublisherClient()
        return self._publisher

    def _storage_client_value(self):
        if self._storage is None:
            from google.cloud import storage  # type: ignore

            self._storage = storage.Client()
        return self._storage

    def _bigquery_client_value(self):
        if self._bigquery is None:
            from google.cloud import bigquery  # type: ignore

            self._bigquery = bigquery.Client()
        return self._bigquery

    def publish_tick(self, event_id: str, snapshot: dict[str, Any]) -> bool:
        """Publish only event-level metrics; never session or viewer identifiers."""
        if not self.settings.probe_findings_topic:
            return False
        payload = {
            "schema": "raccord.probe-summary.v1",
            "event_id": event_id,
            "recorded_at": self._clock().isoformat(),
            "program_s": snapshot.get("program_s"),
            "series_published": snapshot.get("series_published"),
            "evaluations": snapshot.get("evaluations"),
            "breached_slos": snapshot.get("breached_slos", []),
        }

        def send() -> str:
            client = self._publisher_client()
            topic = self.settings.probe_findings_topic
            if not topic.startswith("projects/"):
                project = self.settings.google_cloud_project
                topic = client.topic_path(project, topic)
            future = client.publish(
                topic,
                json.dumps(payload, sort_keys=True).encode("utf-8"),
                schema="raccord.probe-summary.v1",
                event_id=event_id,
            )
            return str(future.result(timeout=10))

        return self._handle("pubsub", send)

    def persist_incident(
        self,
        event_id: str,
        incident: Incident,
        review: PostIncidentReview,
    ) -> dict[str, bool]:
        """Persist a complete immutable bundle and one de-identified aggregate row."""
        recorded_at = self._clock()
        bundle = {
            "schema": "raccord.incident-bundle.v1",
            "event_id": event_id,
            "recorded_at": recorded_at.isoformat(),
            "incident": incident.model_dump(mode="json"),
            "review": review.model_dump(mode="json"),
        }
        results: dict[str, bool] = {}
        if self.settings.evidence_bucket:

            def upload() -> str:
                client = self._storage_client_value()
                object_name = (
                    f"incidents/{recorded_at:%Y/%m/%d}/"
                    f"{incident.incident_id}-{incident.evidence_hash()[:16]}.json"
                )
                blob = client.bucket(self.settings.evidence_bucket).blob(object_name)
                blob.upload_from_string(
                    json.dumps(bundle, sort_keys=True),
                    content_type="application/json",
                    if_generation_match=0,
                )
                return f"gs://{self.settings.evidence_bucket}/{object_name}"

            results["storage"] = self._handle("storage", upload)

        if self.settings.analytics_dataset:

            def insert() -> str:
                client = self._bigquery_client_value()
                top = incident.hypotheses[0] if incident.hypotheses else None
                row = {
                    "incident_id": incident.incident_id,
                    "event_id": event_id,
                    "recorded_at": recorded_at.isoformat(),
                    "severity": incident.severity.value,
                    "root_cause": review.root_cause.value,
                    "diagnosis_confidence": top.posterior if top else 0.0,
                    "affected_sessions": incident.scope.affected_sessions if incident.scope else 0,
                    "protected_sessions": incident.scope.protected_sessions
                    if incident.scope
                    else 0,
                    "time_to_recovery_s": incident.timings.get("time_to_recovery_s"),
                    "outage_seconds": incident.timings.get("outage_seconds"),
                    "diagnosis_correct": review.diagnosis_correct,
                    "audit_chain_valid": True,
                }
                errors = client.insert_rows_json(
                    self.settings.analytics_table,
                    [row],
                    row_ids=[incident.incident_id],
                )
                if errors:
                    raise RuntimeError(f"BigQuery insert rejected: {errors}")
                return self.settings.analytics_table

            results["bigquery"] = self._handle("bigquery", insert)
        return results
