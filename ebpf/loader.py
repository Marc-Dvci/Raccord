"""Load the delivery-path eBPF programs and export them to Prometheus.

    sudo python ebpf/loader.py --config ebpf/components.yaml
    python ebpf/loader.py --dry-run          # validate config, no kernel needed

The loader maps cgroup ids to delivery-chain component names, reads the BPF maps
on an interval, and exposes them on the same `/metrics` surface the probe fleet
uses - so infrastructure evidence and media symptoms land on one timeline in
Grafana and the change-correlation agent can use both.

Metrics exported (all labelled by `component` and nothing else):

    accesspulse_ebpf_offcpu_seconds            histogram  encoder descheduling
    accesspulse_ebpf_tcp_retransmits_total     counter    caption path loss
    accesspulse_ebpf_clock_adjustments_total   counter    timing reference steps
    accesspulse_ebpf_config_reopens_total      counter    configuration reloads
    accesspulse_ebpf_seconds_since_config_read gauge      staleness

There is no viewer, session, address or payload anywhere in that list, and the
BPF programs do not read any (docs/PRIVACY.md).

Requires Linux with BTF and a recent libbpf (`pip install bcc` or the bundled
libbpf-python). AccessPulse never requires it: with the loader absent the
diagnosis runs on media evidence alone, which is the configuration the published
benchmark measures.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
BPF_OBJECT = ROOT / "ebpf" / "caption_path.bpf.o"
DEFAULT_CONFIG = ROOT / "ebpf" / "components.yaml"

# Histogram slot i covers [2^i, 2^(i+1)) nanoseconds.
SLOTS = 27


@dataclass
class Component:
    name: str
    cgroup_path: str
    kind: str
    cgroup_id: int | None = None
    # A component that has not reopened its configuration since the last change
    # event is running stale config - the negative signal that no application
    # metric exposes.
    config_paths: list[str] = field(default_factory=list)


def load_components(path: Path) -> list[Component]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [Component(**entry) for entry in raw["components"]]


def resolve_cgroup_ids(components: list[Component]) -> None:
    """cgroup id is the inode of the cgroup directory."""
    import os

    for component in components:
        try:
            component.cgroup_id = os.stat(component.cgroup_path).st_ino
        except OSError as exc:
            raise SystemExit(
                f"cannot resolve cgroup for {component.name}: {exc}. "
                "Is the delivery component running, and is the path correct?"
            ) from exc


# ---------------------------------------------------------------------------


class Exporter:
    """Reads the BPF maps and renders Prometheus exposition text.

    Counters are exported as monotonic totals; the kernel maps are never reset,
    so a restart of the loader does not fabricate a spike.
    """

    def __init__(self, components: list[Component]) -> None:
        self.components = components
        self.by_cgroup = {c.cgroup_id: c for c in components if c.cgroup_id}
        self.last_config_read: dict[str, float] = {}

    def render(self, snapshot: dict[str, Any]) -> str:
        lines: list[str] = [
            "# HELP accesspulse_ebpf_offcpu_seconds Time a delivery component "
            "spent off-CPU, from sched_switch.",
            "# TYPE accesspulse_ebpf_offcpu_seconds histogram",
        ]
        for name, hist in snapshot.get("offcpu_ns", {}).items():
            cumulative = 0
            total = 0.0
            for slot, count in enumerate(hist):
                cumulative += count
                upper = (2 ** slot) / 1e9
                total += count * upper
                lines.append(
                    f'accesspulse_ebpf_offcpu_seconds_bucket{{component="{name}",'
                    f'le="{upper:.9f}"}} {cumulative}'
                )
            lines.append(
                f'accesspulse_ebpf_offcpu_seconds_bucket{{component="{name}",le="+Inf"}} '
                f"{cumulative}"
            )
            lines.append(f'accesspulse_ebpf_offcpu_seconds_sum{{component="{name}"}} {total:.6f}')
            lines.append(f'accesspulse_ebpf_offcpu_seconds_count{{component="{name}"}} '
                         f"{cumulative}")

        for metric, help_text in (
            ("tcp_retransmits_total", "TCP retransmits on the component's egress."),
            ("clock_adjustments_total", "clock_adjtime calls - timing reference steps."),
            ("config_reopens_total", "Configuration file reopens."),
        ):
            key = metric.replace("_total", "")
            lines.append(f"# HELP accesspulse_ebpf_{metric} {help_text}")
            lines.append(f"# TYPE accesspulse_ebpf_{metric} counter")
            for name, value in snapshot.get(key, {}).items():
                lines.append(f'accesspulse_ebpf_{metric}{{component="{name}"}} {value}')

        now = time.time()
        lines.append("# HELP accesspulse_ebpf_seconds_since_config_read Seconds since this "
                     "component last reopened its configuration.")
        lines.append("# TYPE accesspulse_ebpf_seconds_since_config_read gauge")
        for name, at in self.last_config_read.items():
            lines.append(f'accesspulse_ebpf_seconds_since_config_read{{component="{name}"}} '
                         f"{now - at:.1f}")
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------


def attach(components: list[Component]):  # pragma: no cover - requires a kernel
    try:
        from bcc import BPF
    except ImportError as exc:
        raise SystemExit(
            "eBPF support is optional and not installed.\n"
            "  Linux with BTF:  pip install bcc\n"
            "AccessPulse runs without it; infrastructure evidence is simply absent."
        ) from exc

    if not BPF_OBJECT.exists():
        raise SystemExit(
            f"{BPF_OBJECT} not built. Run:\n"
            "  clang -O2 -g -target bpf -c ebpf/caption_path.bpf.c -o ebpf/caption_path.bpf.o"
        )

    bpf = BPF(src_file=str(ROOT / "ebpf" / "caption_path.bpf.c"))
    bpf.attach_raw_tracepoint(tp="sched_switch", fn_name="on_sched_switch")
    bpf.attach_raw_tracepoint(tp="tcp_retransmit_skb", fn_name="on_tcp_retransmit")
    bpf.attach_tracepoint(tp="syscalls:sys_enter_clock_adjtime", fn_name="on_clock_adjtime")
    bpf.attach_tracepoint(tp="syscalls:sys_enter_openat", fn_name="on_openat")
    return bpf


def read_maps(bpf, components: list[Component]) -> dict[str, Any]:  # pragma: no cover
    by_cgroup = {c.cgroup_id: c.name for c in components}
    snapshot: dict[str, Any] = {"offcpu_ns": {}, "tcp_retransmits": {},
                                "clock_adjustments": {}, "config_reopens": {}}
    for cgroup, hist in bpf["offcpu_ns"].items():
        name = by_cgroup.get(cgroup.value)
        if name:
            snapshot["offcpu_ns"][name] = [hist.slots[i] for i in range(SLOTS)]
    for map_name, key in (("retransmits", "tcp_retransmits"),
                          ("clock_adjustments", "clock_adjustments"),
                          ("config_reopens", "config_reopens")):
        for cgroup, count in bpf[map_name].items():
            name = by_cgroup.get(cgroup.value)
            if name:
                snapshot[key][name] = count.value
    return snapshot


def main() -> int:
    ap = argparse.ArgumentParser(description="AccessPulse eBPF delivery telemetry")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--out", type=Path, default=Path("var/ebpf_metrics.prom"),
                    help="textfile-collector path Prometheus scrapes")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate the configuration without touching the kernel")
    args = ap.parse_args()

    components = load_components(args.config)
    print(f"{len(components)} delivery components configured:")
    for c in components:
        print(f"  {c.name:<22} {c.kind:<10} {c.cgroup_path}")

    if args.dry_run:
        exporter = Exporter(components)
        sample = {
            "offcpu_ns": {c.name: [0] * SLOTS for c in components},
            "tcp_retransmits": {c.name: 0 for c in components},
            "clock_adjustments": {c.name: 0 for c in components},
            "config_reopens": {c.name: 0 for c in components},
        }
        rendered = exporter.render(sample)
        print(f"\ndry run: exposition renders, {len(rendered.splitlines())} lines, "
              f"{len(components)} components")
        print("no kernel programs loaded")
        return 0

    if sys.platform != "linux":  # pragma: no cover
        raise SystemExit("eBPF requires Linux. Use --dry-run to validate the configuration.")

    resolve_cgroup_ids(components)
    bpf = attach(components)
    exporter = Exporter(components)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    print(f"\nattached; writing {args.out} every {args.interval}s")

    try:  # pragma: no cover
        while True:
            snapshot = read_maps(bpf, components)
            args.out.write_text(exporter.render(snapshot), encoding="utf-8")
            time.sleep(args.interval)
    except KeyboardInterrupt:  # pragma: no cover
        print("\ndetached")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
