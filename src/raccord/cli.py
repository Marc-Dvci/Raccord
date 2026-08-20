"""Raccord command line.

raccord serve                 # run the web product on :8080
raccord hero                  # run the hero incident end to end in the terminal
raccord certify               # run preflight certification
raccord bench --scenarios 1000
raccord mcp                   # show the resolved Grafana MCP tool surface
raccord faults                # list the fault library
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .certification import ReleaseGate
from .certification import summarise as summarise_cert
from .config import get_settings
from .faults import FAULT_LIBRARY, HERO_FAULT_ID
from .runtime import RaccordRuntime

app = typer.Typer(add_completion=False, help="Raccord - Accessible Experience Reliability")
console = Console()


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(None, help="defaults to RACCORD_API_PORT"),
    reload: bool = typer.Option(False),
) -> None:
    """Run the Raccord web product and API."""
    import uvicorn

    settings = get_settings()
    console.print(
        Panel.fit(
            f"[bold]Raccord[/bold]\n"
            f"web      http://{host}:{port or settings.api_port}\n"
            f"api docs http://{host}:{port or settings.api_port}/docs\n"
            f"metrics  http://{host}:{port or settings.api_port}/metrics\n"
            f"status   http://{host}:{port or settings.api_port}/api/status-page\n"
            f"MCP transport: {settings.mcp_transport}   reasoning: {settings.reasoning_mode}",
            title="starting",
            border_style="blue",
        )
    )
    uvicorn.run(
        "raccord.api:app",
        host=host,
        port=port or settings.api_port,
        reload=reload,
        log_level="warning",
    )


@app.command()
def hero(
    fault: str = typer.Option(HERO_FAULT_ID, help="fault id from the library"),
    ticks: int = typer.Option(9),
    seconds: float = typer.Option(20.0),
    transport: str = typer.Option(None, help="stub | stdio | http"),
    json_out: Path = typer.Option(None, "--json", help="write the full record to a file"),
) -> None:
    """Run one incident end to end and print the closed loop."""
    asyncio.run(_hero(fault, ticks, seconds, transport, json_out))


async def _hero(
    fault: str, ticks: int, seconds: float, transport: str | None, json_out: Path | None
) -> None:
    rt = RaccordRuntime(mcp_transport=transport, db_prefix="cli")
    await rt.connect()
    console.print(
        f"[dim]Grafana MCP: {rt.mcp.transport}, {len(rt.mcp.tool_names)} tools advertised[/dim]"
    )

    for _ in range(3):
        rt.tick(15)
    console.print(f"[green]baseline healthy[/green]: {len(rt.assurance.breaches())} SLOs breaching")

    spec = FAULT_LIBRARY[fault]
    rt.inject(fault)
    console.print(
        Panel.fit(
            f"[bold]{spec.name}[/bold]\n{spec.description}\n\n"
            f"ground truth: [yellow]{spec.failure_class.value}[/yellow]\n"
            f"component: {spec.component}   onset: {spec.onset}",
            title="fault injected",
            border_style="yellow",
        )
    )
    for _ in range(ticks):
        rt.tick(seconds)
    console.print(f"breaching now: {sorted(rt.assurance.breaches())}")

    result = await rt.run_incident()
    inc = result.incident

    t = Table(title="Closed loop", show_header=False, box=None)
    t.add_row("detected", _yn(result.detected))
    t.add_row(
        "diagnosis", f"{_yn(result.diagnosis_correct)} ({result.top_posterior:.3f} posterior)"
    )
    if inc:
        t.add_row("state", inc.state.value)
        t.add_row(
            "scope",
            f"{'/'.join(inc.scope.territories)} · "
            f"{'/'.join(inc.scope.player_versions)} · "
            f"{'/'.join(inc.scope.languages)}"
            if inc.scope
            else "—",
        )
        t.add_row(
            "scope precision/recall", f"{result.scope_precision:.2f} / {result.scope_recall:.2f}"
        )
        t.add_row(
            "policy", inc.policy_decision.classification.value if inc.policy_decision else "—"
        )
        t.add_row(
            "approval",
            f"{inc.approval.approver} ({inc.approval.approver_role.value})"
            if inc.approval
            else "not required",
        )
    t.add_row("action", result.action_taken or "—")
    t.add_row(
        "verification", f"{result.assertions_passing}/{result.assertions_total} assertions passing"
    )
    t.add_row("recovered", _yn(result.recovered))
    t.add_row(
        "sessions affected / protected",
        f"{result.affected_sessions:,} / {result.protected_sessions:,}",
    )
    t.add_row("Grafana MCP calls", str(result.mcp_calls))
    t.add_row("unsafe action", _yn(result.unsafe_action))
    if inc:
        machine = rt.coordinator.machine(inc.incident_id)
        t.add_row("audit chain", _yn(machine.verify_audit_chain()))
    if result.error:
        t.add_row("error", f"[red]{result.error}[/red]")
    console.print(t)

    console.print("\n[bold]Grafana MCP call chain[/bold]")
    for i, call in enumerate(rt.mcp.call_log, 1):
        console.print(
            f"  {i:>2}. {call['tool']:<28} "
            f"{call['duration_ms']:>7.2f} ms  {call['result_bytes']:>6} B"
        )

    if inc:
        for c in inc.communications:
            if c.audience == "public_status":
                console.print(Panel(c.body, title="public status update", border_style="green"))

    if json_out:
        payload = {
            "result": {
                k: (v.value if hasattr(v, "value") else v)
                for k, v in result.__dict__.items()
                if k != "incident"
            },
            "incident": inc.model_dump(mode="json") if inc else None,
            "mcp_calls": rt.mcp.call_log,
            "review": (
                rt.coordinator.reviews[inc.incident_id].model_dump(mode="json")
                if inc and inc.incident_id in rt.coordinator.reviews
                else None
            ),
        }
        json_out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        console.print(f"[dim]wrote {json_out}[/dim]")
    await rt.aclose()


@app.command()
def certify(transport: str = typer.Option(None)) -> None:
    """Run the Accessibility Release Gate against the current environment."""
    asyncio.run(_certify(transport))


async def _certify(transport: str | None) -> None:
    rt = RaccordRuntime(mcp_transport=transport, db_prefix="cli")
    await rt.connect()
    rt.tick(30)
    record = await ReleaseGate(rt.sim, rt.registry, rt.event_id, rt.mcp).run()
    summary = summarise_cert(record)
    console.print(
        Panel.fit(
            f"[bold]{'CERTIFIED' if record.certified else 'BLOCKED'}[/bold]\n"
            f"{summary['assertions']} assertions across {len(summary['by_gate'])} gates\n"
            f"signature {record.signature[:24]}…",
            border_style="green" if record.certified else "red",
        )
    )
    t = Table("gate", "passing", "failing", "inconclusive", "hard")
    for gate, row in summary["by_gate"].items():
        t.add_row(
            gate,
            str(row["passing"]),
            str(row["failing"]),
            str(row["inconclusive"]),
            str(row["hard"]),
        )
    console.print(t)
    for blocker in record.blockers:
        console.print(f"  [red]blocker[/red] {blocker}")
    await rt.aclose()


@app.command()
def bench(
    scenarios: int = typer.Option(1000, help="number of scenarios"),
    seed: int = typer.Option(20260803),
    ablations: bool = typer.Option(True, help="also run the ablation configurations"),
    out: Path = typer.Option(Path("bench/results"), help="output directory"),
) -> None:
    """Run the reproducible benchmark corpus."""
    from bench.harness import run_benchmark  # noqa: PLC0415

    asyncio.run(run_benchmark(scenarios=scenarios, seed=seed, ablations=ablations, out=out))


@app.command()
def mcp(transport: str = typer.Option(None)) -> None:
    """Show the Grafana MCP tool surface and the capabilities Raccord resolves."""
    asyncio.run(_mcp(transport))


async def _mcp(transport: str | None) -> None:
    from .grafana_mcp.client import CAPABILITIES

    rt = RaccordRuntime(mcp_transport=transport, db_prefix="cli")
    await rt.connect()
    console.print(
        f"[bold]transport[/bold] {rt.mcp.transport}   [bold]tools[/bold] {len(rt.mcp.tool_names)}"
    )
    t = Table("capability", "required", "resolved tool", "purpose")
    for cap in CAPABILITIES:
        t.add_row(
            cap.key,
            "yes" if cap.required else "no",
            rt.mcp.tool_for(cap.key) if rt.mcp.has(cap.key) else "[red]missing[/red]",
            cap.purpose,
        )
    console.print(t)
    console.print("\n[dim]" + ", ".join(rt.mcp.tool_names) + "[/dim]")
    await rt.aclose()


@app.command()
def faults() -> None:
    """List the fault library."""
    t = Table("id", "feature", "name", "class", "difficulty")
    for f in FAULT_LIBRARY.values():
        t.add_row(f.fault_id, f.feature.value, f.name, f.failure_class.value, f"{f.difficulty:.2f}")
    console.print(t)
    console.print(f"[dim]{len(FAULT_LIBRARY)} faults[/dim]")


def _yn(value: bool) -> str:
    return "[green]yes[/green]" if value else "[red]no[/red]"


if __name__ == "__main__":
    app()
