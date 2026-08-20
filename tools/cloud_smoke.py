"""Verify a deployed Raccord instance and emit a non-secret evidence summary.

Example:
    python tools/cloud_smoke.py https://raccord-xyz.run.app \
      --expect-gemini --expect-agent-engine --expect-agent-observability \
      --expect-cloud-evidence --expect-telemetry --out var/cloud-smoke.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


def _check(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def run(args: argparse.Namespace) -> dict[str, Any]:
    base = args.base_url.rstrip("/")
    timeout = httpx.Timeout(args.timeout, connect=20.0)
    with httpx.Client(base_url=base, timeout=timeout, follow_redirects=True) as client:
        health = client.get("/healthz")
        health.raise_for_status()
        ready = client.get("/readyz")
        ready.raise_for_status()
        ready_data = ready.json()
        result_response = client.post(
            "/api/demo/run",
            json={"fault_id": args.fault, "ticks": 9, "seconds_per_tick": 20},
        )
        result_response.raise_for_status()
        result = result_response.json()
        cloud_response = client.get("/api/cloud-status")
        cloud_response.raise_for_status()
        cloud = cloud_response.json()

    failures: list[str] = []
    execution = result.get("execution", {})
    evidence = result.get("cloud_evidence", {})
    telemetry = result.get("telemetry_export", {})
    assertions = result.get("assertions", [0, 0])

    _check(result.get("detected") is True, "fault was not detected", failures)
    _check(result.get("diagnosis_correct") is True, "root cause was incorrect", failures)
    _check(result.get("recovered") is True, "incident did not recover", failures)
    _check(result.get("unsafe_action") is False, "unsafe action was reported", failures)
    _check(result.get("error") in (None, ""), f"incident error: {result.get('error')}", failures)
    _check(assertions[1] > 0 and assertions[0] == assertions[1], "verification failed", failures)
    _check(result.get("audit_chain_valid") is True, "audit chain is invalid", failures)
    _check(result.get("mcp_calls", 0) > 0, "no Grafana MCP calls were recorded", failures)
    _check(
        execution.get("mcp_transport") in {"http", "stdio"},
        f"deployed MCP transport is {execution.get('mcp_transport')!r}",
        failures,
    )
    _check(
        execution.get("mcp_tools_available", 0) >= 12,
        "fewer than 12 required Grafana MCP capabilities are available",
        failures,
    )
    if args.expect_gemini:
        _check(execution.get("reasoning_mode") == "gemini", "Gemini mode is not active", failures)
    if args.expect_agent_engine:
        _check(
            execution.get("reasoning_runtime") == "agent-engine",
            "Vertex AI Agent Engine is not active",
            failures,
        )
    if args.expect_agent_observability:
        _check(
            execution.get("agent_observability") is True
            and cloud.get("agent_observability") is True,
            "Grafana Agent Observability is not active",
            failures,
        )
    if args.expect_cloud_evidence:
        _check(evidence.get("enabled") is True, "Google Cloud evidence is disabled", failures)
        _check(evidence.get("failed") == 0, "a Google Cloud evidence write failed", failures)
        _check(
            {"pubsub", "storage", "bigquery"}.issubset(set(evidence.get("targets", []))),
            "not all Pub/Sub, Cloud Storage, and BigQuery targets were exercised",
            failures,
        )
    if args.expect_telemetry:
        _check(
            telemetry.get("configured") is True,
            "Grafana telemetry export is disabled",
            failures,
        )
        _check(
            telemetry.get("connected") is True,
            "Grafana telemetry exporter is disconnected",
            failures,
        )
        _check(
            telemetry.get("healthy") is True,
            "Grafana telemetry exporter reported an error",
            failures,
        )

    raw_digest = hashlib.sha256(result_response.content).hexdigest()
    report = {
        "schema": "raccord.cloud-smoke.v1",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "service_url": base,
        "passed": not failures,
        "failures": failures,
        "ready": ready_data,
        "scenario": {
            "fault_id": result.get("fault_id"),
            "ground_truth": result.get("ground_truth"),
            "detected": result.get("detected"),
            "diagnosis_correct": result.get("diagnosis_correct"),
            "recovered": result.get("recovered"),
            "assertions": assertions,
            "unsafe_action": result.get("unsafe_action"),
            "mcp_calls": result.get("mcp_calls"),
            "audit_chain_valid": result.get("audit_chain_valid"),
            "response_sha256": raw_digest,
        },
        "execution": execution,
        "cloud_evidence": evidence,
        "telemetry_export": telemetry,
        "cloud_status": cloud,
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test a deployed Raccord service")
    parser.add_argument("base_url")
    parser.add_argument("--fault", default="cap.progressive_drift")
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--expect-gemini", action="store_true")
    parser.add_argument("--expect-agent-engine", action="store_true")
    parser.add_argument("--expect-agent-observability", action="store_true")
    parser.add_argument("--expect-cloud-evidence", action="store_true")
    parser.add_argument("--expect-telemetry", action="store_true")
    args = parser.parse_args()

    report = run(args)
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
