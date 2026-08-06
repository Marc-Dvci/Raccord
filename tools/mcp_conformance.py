"""Check a Grafana MCP server against the capability contract AccessPulse needs.

    python tools/mcp_conformance.py --transport http   --out docs/mcp_conformance.json
    python tools/mcp_conformance.py --transport stdio  --out docs/mcp_conformance.json
    python tools/mcp_conformance.py --transport stub

AccessPulse never hard-codes a Grafana MCP tool name into an agent. It asks for a
*capability* ("read the firing alert") and `grafana_mcp.client` resolves that
against whatever the connected server actually advertises (ADR 0002). This tool
runs only that resolution step and writes down the answer, because the answer is
not stable: the official grafana/mcp-grafana server has renamed, consolidated and
removed tools across releases, and the hosted Grafana Cloud endpoint does not
expose exactly the same surface as the open-source one.

The output is a record of what one specific server, on one specific day, could
and could not do — which is the only honest form this claim can take. It exits
non-zero if a required capability is unresolvable, so it can gate a deployment
against a server that cannot support an investigation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from accesspulse.config import get_settings  # noqa: E402
from accesspulse.grafana_mcp.client import (  # noqa: E402
    CAPABILITIES,
    MCPUnavailable,
    build_client,
)
from accesspulse.telemetry import TelemetryPlane  # noqa: E402


async def probe(transport: str) -> dict:
    settings = get_settings()
    telemetry = TelemetryPlane()
    client = build_client(telemetry, sim=None, transport=transport)

    unresolved: list[dict] = []
    try:
        await client.connect()
        connected = True
        detail = ""
    except MCPUnavailable as exc:
        # A missing *required* capability raises, but the tool list has already
        # been fetched by then and is the most useful thing in this report.
        connected = False
        detail = str(exc)
    finally:
        pass

    tools = client.tool_names
    resolution = []
    for cap in CAPABILITIES:
        resolved = client._resolved.get(cap.key)
        row = {
            "capability": cap.key,
            "required": cap.required,
            "candidates": list(cap.candidates),
            "resolved_tool": resolved,
            "purpose": cap.purpose,
        }
        resolution.append(row)
        if resolved is None:
            unresolved.append(row)

    await client.aclose()

    return {
        "generated": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "transport": transport,
        "grafana_url": settings.grafana_url if transport != "stub" else "n/a (in-process)",
        "endpoint": settings.mcp_http_url if transport == "http" else (
            " ".join([settings.mcp_stdio_command, *settings.mcp_stdio_argv])
            if transport == "stdio" else "in-process"
        ),
        "connected": connected,
        "detail": detail,
        "server_tool_count": len(tools),
        "server_tools": tools,
        "capabilities_total": len(CAPABILITIES),
        "capabilities_resolved": len(CAPABILITIES) - len(unresolved),
        "required_unresolved": [r["capability"] for r in unresolved if r["required"]],
        "optional_unresolved": [r["capability"] for r in unresolved if not r["required"]],
        "resolution": resolution,
    }


def render(report: dict) -> None:
    print(f"transport            {report['transport']}")
    print(f"endpoint             {report['endpoint']}")
    print(f"server tools         {report['server_tool_count']}")
    print(f"capabilities         {report['capabilities_resolved']}"
          f"/{report['capabilities_total']} resolved")
    print()
    width = max(len(r["capability"]) for r in report["resolution"])
    for r in report["resolution"]:
        mark = "ok  " if r["resolved_tool"] else ("MISS" if r["required"] else "--  ")
        target = r["resolved_tool"] or f"(none of: {', '.join(r['candidates'])})"
        req = "required" if r["required"] else "optional"
        print(f"  {mark} {r['capability']:<{width}}  {req:<8}  {target}")
    if report["required_unresolved"]:
        print()
        print("REQUIRED capabilities this server cannot provide: "
              + ", ".join(report["required_unresolved"]))
        print("An investigation cannot leave SCOPED against this server "
              "(src/accesspulse/incident.py).")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--transport", default=None, help="stub | stdio | http")
    ap.add_argument("--out", type=Path, default=None, help="write the JSON report here")
    args = ap.parse_args()

    transport = args.transport or get_settings().mcp_transport
    report = asyncio.run(probe(transport))
    render(report)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {args.out}")

    return 1 if report["required_unresolved"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
