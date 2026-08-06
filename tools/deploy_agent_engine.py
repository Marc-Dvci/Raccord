"""Deploy the AccessPulse reasoning plane to Vertex AI Agent Engine.

    python tools/deploy_agent_engine.py --check          # preflight only
    python tools/deploy_agent_engine.py --staging-bucket gs://my-bucket
    python tools/deploy_agent_engine.py --list
    python tools/deploy_agent_engine.py --delete <resource-name>

What is deployed is the *reasoning plane only* - the ADK synthesis agent and its
Grafana MCP toolset. The deterministic core, the policy engine, the approval
service and the executor stay in the application, on Cloud Run, inside the
operator's own boundary. That split is not an implementation detail: an agent
runtime that could execute a remediation would defeat the whole safety argument
(ADR 0001, docs/THREAT_MODEL.md section 5).

Nothing here is required to run AccessPulse. The default reasoning mode is
offline, and the closed loop reaches a verified recovery with no cloud account
at all (ADR 0011).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

REQUIRED_ENV = ("GOOGLE_CLOUD_PROJECT",)
RECOMMENDED_ENV = ("GOOGLE_CLOUD_LOCATION", "GOOGLE_GENAI_USE_VERTEXAI")


def preflight(staging_bucket: str | None) -> list[str]:
    """Everything that would fail the deployment, checked before it starts."""
    problems: list[str] = []

    for name in REQUIRED_ENV:
        if not os.environ.get(name):
            problems.append(f"{name} is not set")

    if not staging_bucket:
        problems.append("--staging-bucket is required (a gs:// bucket in the same project)")
    elif not staging_bucket.startswith("gs://"):
        problems.append(f"staging bucket {staging_bucket!r} must start with gs://")

    try:
        import google.adk  # noqa: F401
        import vertexai  # noqa: F401
    except ImportError:
        problems.append('cloud extras are not installed - run: pip install -e ".[cloud]"')

    from accesspulse.config import get_settings

    settings = get_settings()
    if settings.reasoning_mode != "gemini":
        problems.append(
            f"AP_REASONING_MODE is {settings.reasoning_mode!r}; set it to 'gemini' so the "
            "deployed plane is the one this project actually uses"
        )
    if not settings.grafana_service_account_token:
        problems.append(
            "AP_GRAFANA_SERVICE_ACCOUNT_TOKEN is empty. The deployed agent reaches Grafana "
            "through the MCP server and cannot investigate without it."
        )
    if settings.grafana_url.startswith("http://localhost"):
        problems.append(
            f"AP_GRAFANA_URL is {settings.grafana_url!r}, which the Agent Engine runtime "
            "cannot reach. Point it at the Grafana Cloud stack."
        )
    return problems


def describe() -> None:
    """Print what will be deployed, and what deliberately will not be."""
    from accesspulse.agents import adk
    from accesspulse.config import get_settings

    settings = get_settings()
    print("deploying")
    print(f"  agent            accesspulse_synthesis ({settings.gemini_model})")
    print("  tools            Grafana MCP toolset, read-only capability filter:")
    print("                   list_datasources, list_alert_rules, get_alert_rule_by_uid,")
    print("                   query_prometheus, query_loki_logs, query_tempo_traces,")
    print("                   search_dashboards, get_dashboard_by_uid, generate_deeplink,")
    print("                   find_annotations")
    print("  tracing          enabled (spans land in Cloud Trace and Grafana Tempo)")
    print("\nnot deploying - these stay inside the operator's boundary")
    print("  policy engine    every consequential action is classified here")
    print("  approval service holds the HMAC signing key")
    print("  executor         the only component that can change the environment")
    print("  probe fleet      measures the rendered experience")
    print(f"\nreasoning plane available locally: {adk.available()}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Deploy the AccessPulse reasoning plane")
    ap.add_argument("--staging-bucket", default=os.environ.get("AP_STAGING_BUCKET"))
    ap.add_argument("--display-name", default="accesspulse-reasoning")
    ap.add_argument("--check", action="store_true", help="preflight only, deploy nothing")
    ap.add_argument("--list", action="store_true", help="list deployed agent engines")
    ap.add_argument("--delete", metavar="RESOURCE_NAME", help="delete a deployed agent engine")
    args = ap.parse_args()

    if args.list or args.delete:
        try:
            import vertexai  # type: ignore
            from vertexai import agent_engines  # type: ignore
        except ImportError:
            raise SystemExit('cloud extras are not installed - pip install -e ".[cloud]"')
        vertexai.init(project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
                      location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"))
        if args.delete:
            agent_engines.get(args.delete).delete(force=True)
            print(f"deleted {args.delete}")
            return 0
        for engine in agent_engines.list():
            print(f"{engine.resource_name}  {engine.display_name}")
        return 0

    problems = preflight(args.staging_bucket)
    describe()

    if problems:
        print("\npreflight failed:")
        for problem in problems:
            print(f"  - {problem}")
        print("\nnothing was deployed.")
        return 1
    print("\npreflight passed")

    if args.check:
        print("--check: nothing deployed")
        return 0

    from accesspulse.agents.adk import deploy_to_agent_engine

    engine = deploy_to_agent_engine(args.staging_bucket, args.display_name)
    print(f"\ndeployed: {engine.resource_name}")
    print("set AP_AGENT_ENGINE_RESOURCE to this value to route synthesis through it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
