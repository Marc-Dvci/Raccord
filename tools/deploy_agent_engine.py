"""Deploy the Raccord reasoning plane to Vertex AI Agent Engine.

    python tools/deploy_agent_engine.py --check          # preflight only
    python tools/deploy_agent_engine.py --staging-bucket gs://my-bucket
    python tools/deploy_agent_engine.py --list
    python tools/deploy_agent_engine.py --delete <resource-name>

What is deployed is the *reasoning plane only* - the ADK reasoning dispatcher and its
Grafana MCP toolset. The deterministic core, the policy engine, the approval
service and the executor stay in the application, on Cloud Run, inside the
operator's own boundary. That split is not an implementation detail: an agent
runtime that could execute a remediation would defeat the whole safety argument
(ADR 0001, docs/THREAT_MODEL.md section 5).

Nothing here is required to run Raccord. The default reasoning mode is
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


def preflight(
    staging_bucket: str | None,
    mcp_url: str | None = None,
    mcp_token_secret: str | None = None,
    agento11y_token_secret: str | None = None,
    service_account: str | None = None,
) -> list[str]:
    """Everything that would fail the deployment, checked before it starts."""
    problems: list[str] = []

    for name in REQUIRED_ENV:
        if not os.environ.get(name):
            problems.append(f"{name} is not set")

    if not staging_bucket:
        problems.append("--staging-bucket is required (a gs:// bucket in the same project)")
    elif not staging_bucket.startswith("gs://"):
        problems.append(f"staging bucket {staging_bucket!r} must start with gs://")
    if not service_account:
        problems.append(
            "--service-account is required so reasoning runs under its least-privilege identity"
        )

    try:
        import google.adk  # noqa: F401
        import vertexai  # noqa: F401
    except ImportError:
        problems.append('cloud extras are not installed - run: pip install -e ".[cloud]"')

    from raccord.config import get_settings

    settings = get_settings()
    if settings.reasoning_mode != "gemini":
        problems.append(
            f"RACCORD_REASONING_MODE is {settings.reasoning_mode!r}; set it to 'gemini' so the "
            "deployed plane is the one this project actually uses"
        )
    if settings.mcp_transport != "http":
        problems.append(
            "RACCORD_MCP_TRANSPORT must be 'http'; Agent Engine cannot launch local Docker"
        )
    resolved_mcp_url = mcp_url or settings.mcp_http_url
    if not resolved_mcp_url.startswith("https://"):
        problems.append(
            "--mcp-url must be the public HTTPS URL of the authenticated Raccord MCP gateway"
        )
    if not settings.mcp_bearer_token and not mcp_token_secret:
        problems.append(
            "provide --mcp-token-secret (recommended) or set RACCORD_MCP_BEARER_TOKEN"
        )
    if os.environ.get("AGENTO11Y_ENDPOINT") and not (
        os.environ.get("AGENTO11Y_AUTH_TOKEN") or agento11y_token_secret
    ):
        problems.append(
            "AGENTO11Y_ENDPOINT is set; provide --agento11y-token-secret or "
            "AGENTO11Y_AUTH_TOKEN"
        )
    if settings.grafana_url.startswith("http://localhost"):
        problems.append(
            f"RACCORD_GRAFANA_URL is {settings.grafana_url!r}, which the Agent Engine runtime "
            "cannot reach. Point it at the Grafana Cloud stack."
        )
    return problems


def describe() -> None:
    """Print what will be deployed, and what deliberately will not be."""
    from raccord.agents import adk
    from raccord.config import get_settings

    settings = get_settings()
    print("deploying")
    print(f"  agent            raccord_reasoning ({settings.gemini_model})")
    print("  skills           synthesis, communications, operator Q&A, incident learning")
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
    ap = argparse.ArgumentParser(description="Deploy the Raccord reasoning plane")
    ap.add_argument("--staging-bucket", default=os.environ.get("RACCORD_STAGING_BUCKET"))
    ap.add_argument("--display-name", default="raccord-reasoning")
    ap.add_argument(
        "--service-account", default=os.environ.get("RACCORD_REASONING_SERVICE_ACCOUNT")
    )
    ap.add_argument(
        "--mcp-url",
        default=os.environ.get("RACCORD_MCP_HTTP_URL"),
        help="public HTTPS endpoint of the authenticated MCP gateway",
    )
    ap.add_argument(
        "--mcp-token-secret",
        default=os.environ.get("RACCORD_MCP_TOKEN_SECRET"),
        help="Secret Manager secret id/resource containing the gateway bearer token",
    )
    ap.add_argument(
        "--agento11y-token-secret",
        default=os.environ.get("RACCORD_AGENTO11Y_TOKEN_SECRET"),
        help="Secret Manager secret id/resource containing the Grafana Cloud ingest token",
    )
    ap.add_argument("--min-instances", type=int, default=0)
    ap.add_argument("--max-instances", type=int, default=1)
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
        from raccord.config import get_settings

        vertexai.init(
            project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
            location=get_settings().agent_engine_location,
        )
        if args.delete:
            agent_engines.get(args.delete).delete(force=True)
            print(f"deleted {args.delete}")
            return 0
        for engine in agent_engines.list():
            print(f"{engine.resource_name}  {engine.display_name}")
        return 0

    if args.min_instances < 0 or args.max_instances < 1 or args.min_instances > args.max_instances:
        raise SystemExit("instance bounds must satisfy 0 <= min-instances <= max-instances")
    problems = preflight(
        args.staging_bucket,
        args.mcp_url,
        args.mcp_token_secret,
        args.agento11y_token_secret,
        args.service_account,
    )
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

    from raccord.agents.adk import deploy_to_agent_engine

    engine = deploy_to_agent_engine(
        args.staging_bucket,
        args.display_name,
        service_account=args.service_account,
        mcp_url=args.mcp_url,
        mcp_token_secret=args.mcp_token_secret,
        agento11y_token_secret=args.agento11y_token_secret,
        min_instances=args.min_instances,
        max_instances=args.max_instances,
    )
    print(f"\ndeployed: {engine.resource_name}")
    print("set RACCORD_AGENT_ENGINE_RESOURCE to this value to route all Gemini skills through it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
