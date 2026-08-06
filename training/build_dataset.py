"""Build QLoRA training shards from AccessPulse's own incident records.

The specialists are trained on what the platform already produces: incident
records with their scope, evidence and verification result, the communications
that were actually issued, and delivery-chain log lines with the component that
emitted them.

Two rules are enforced here rather than trusted:

1. **No audience data, ever.** Every example is checked against the field names
   and the value shapes that could carry it before it is written. A shard that
   fails the check is not written at all - see docs/PRIVACY.md.
2. **No ground truth.** The fault library is the benchmark's answer key. A
   specialist trained on it would be learning the answers rather than the task,
   and its evaluation would be meaningless.

    python training/build_dataset.py --scenarios 200 --out var/training

Running with no accelerator is fine: this script only generates data.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from accesspulse.faults import FAULT_LIBRARY  # noqa: E402
from accesspulse.runtime import AccessPulseRuntime  # noqa: E402

# Field names that would indicate audience data leaking into a training shard.
FORBIDDEN_KEYS = {
    "session_id", "user_id", "account", "account_id", "device_id", "ip", "ip_address",
    "cookie", "email", "subscriber", "viewer_id", "msisdn", "household",
}
# Value shapes that look like an identifier even under an innocent key name.
FORBIDDEN_VALUE_PATTERNS = (
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),                       # IPv4
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),    # email
    re.compile(r"\b[0-9a-f]{32,}\b"),                                 # long hex id
)
# Operator emails are legitimate accountability data in an approval, and are the
# one exception: they are staff, not viewers. They are still redacted from
# training text, because a specialist has no reason to learn them.
OPERATOR_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@studio\.example")


class PrivacyViolation(Exception):
    """Raised before anything is written to disk."""


def assert_no_audience_data(example: dict[str, Any]) -> None:
    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key.lower() in FORBIDDEN_KEYS:
                    raise PrivacyViolation(f"forbidden key {key!r} at {path}")
                walk(value, f"{path}.{key}")
        elif isinstance(node, (list, tuple)):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")
        elif isinstance(node, str):
            redacted = OPERATOR_EMAIL.sub("", node)
            for pattern in FORBIDDEN_VALUE_PATTERNS:
                if pattern.search(redacted):
                    raise PrivacyViolation(f"identifier-shaped value at {path}")

    walk(example, "$")


def redact(text: str) -> str:
    return OPERATOR_EMAIL.sub("[approver]", text)


# ---------------------------------------------------------------------------
# example builders
# ---------------------------------------------------------------------------


def _scope_summary(incident) -> dict[str, Any]:
    s = incident.scope
    if s is None:
        return {}
    return {
        "features": [f.value for f in s.features],
        "languages": list(s.languages),
        "territories": list(s.territories),
        "player_versions": list(s.player_versions),
        "components": list(s.components),
        "blast_class": s.blast_class,
        # The brief cites these figures, so they have to be in the input. An
        # example whose target contains a number the input does not is a lesson
        # in inventing numbers - which is what `no_invented_metrics` catches.
        "affected_sessions": s.affected_sessions,
        "protected_sessions": s.protected_sessions,
    }


def _evidence_summary(incident) -> list[dict[str, Any]]:
    return [
        {"source_tool": e.source_tool, "summary": redact(e.summary)}
        for e in incident.evidence[:12]
    ]


def status_writer_examples(incident) -> Iterable[dict[str, Any]]:
    for c in incident.communications:
        if c.audience != "public_status":
            continue
        yield {
            "input": {
                "feature": [f.value for f in (incident.scope.features if incident.scope else [])],
                "territories": list(incident.scope.territories) if incident.scope else [],
                "languages": list(incident.scope.languages) if incident.scope else [],
                "recovered": incident.state.value in ("RECOVERED", "COMMUNICATED", "REVIEWED"),
                "assertions_passing": sum(1 for a in incident.assertions
                                          if a.status.value == "passing"),
                "assertions_total": len(incident.assertions),
            },
            "output": redact(c.body),
        }


def operator_brief_examples(incident) -> Iterable[dict[str, Any]]:
    for c in incident.communications:
        # One audience per adapter. The operator, specialist and technical-
        # director briefs have genuinely different formats, and a shard that
        # mixed them would be teaching three tasks under one name - which is
        # exactly what a *specialist* adapter is not.
        if c.audience != "operator":
            continue
        yield {
            "input": {
                "scope": _scope_summary(incident),
                "evidence": _evidence_summary(incident),
                "hypotheses": [
                    {"cause": h.failure_class.value, "posterior": round(h.posterior, 3)}
                    for h in incident.hypotheses[:3]
                ],
                "action": (incident.proposed_action.action_type.value
                           if incident.proposed_action else None),
                "verification": [
                    {"name": a.name, "status": a.status.value, "scope": a.scope_note}
                    for a in incident.assertions
                ],
            },
            "output": redact(c.body),
        }


def log_labeller_examples(runtime: AccessPulseRuntime, rng: random.Random,
                          limit: int = 40) -> Iterable[dict[str, Any]]:
    """Log lines paired with the component that emitted them.

    The label is derived from the *emitting component and the line's own
    severity*, never from the fault library.
    """
    lines = runtime.telemetry.logs.query(limit=limit * 3)
    for line in rng.sample(lines, min(limit, len(lines))):
        labels = line.labels or {}
        component = labels.get("component", "")
        kind = ("encoder" if "capenc" in component or "encoder" in component
                else "packager" if "pack" in component
                else "cdn" if "cdn" in component or "origin" in component
                else "timing" if "clock" in component or "ptp" in component
                else "player" if component.startswith("pv-") else
                "auth" if "auth" in component else "unknown")
        severity = labels.get("level", "info")
        yield {
            "input": {"line": redact(line.line), "component": component},
            "output": {
                "component_kind": kind,
                "severity": severity,
                "failure_hint": None if severity == "info" else labels.get("hint"),
                "confidence": 0.9 if severity != "info" else 0.6,
            },
        }


# ---------------------------------------------------------------------------


async def build(scenarios: int, seed: int, out: Path) -> dict[str, int]:
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    fault_ids = sorted(FAULT_LIBRARY)

    shards: dict[str, list[dict[str, Any]]] = {
        "status_writer": [], "operator_brief": [], "log_labeller": [],
    }

    for i in range(scenarios):
        fault_id = fault_ids[i % len(fault_ids)]
        rt = AccessPulseRuntime(seed=seed + i, db_prefix=f"train_{seed}")
        await rt.connect()
        rt.tick(20)
        rt.inject(fault_id)
        for _ in range(4):
            rt.tick(25)
        result = await rt.run_incident(settle_seconds=20.0)
        incident = result.incident
        if incident is not None:
            shards["status_writer"].extend(status_writer_examples(incident))
            shards["operator_brief"].extend(operator_brief_examples(incident))
            try:
                shards["log_labeller"].extend(log_labeller_examples(rt, rng))
            except AttributeError:
                pass  # telemetry log surface is optional for this shard
        await rt.aclose()
        if (i + 1) % 10 == 0:
            print(f"  {i + 1}/{scenarios} scenarios", flush=True)

    counts: dict[str, int] = {}
    for name, examples in shards.items():
        for example in examples:
            assert_no_audience_data(example)
        path = out / f"{name}.jsonl"
        path.write_text("\n".join(json.dumps(e) for e in examples) + "\n", encoding="utf-8")
        counts[name] = len(examples)
        print(f"wrote {path} ({len(examples)} examples)")
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description="Build QLoRA training shards")
    ap.add_argument("--scenarios", type=int, default=60)
    ap.add_argument("--seed", type=int, default=20260803)
    ap.add_argument("--out", type=Path, default=Path("var/training"))
    args = ap.parse_args()
    counts = asyncio.run(build(args.scenarios, args.seed, args.out))
    print(json.dumps(counts, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
