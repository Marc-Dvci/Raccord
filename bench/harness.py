"""Reproducible benchmark for the AccessPulse closed loop.

Every scenario is a seeded draw from the fault library: inject one documented
fault into the digital twin, let the event run, then let the full loop run
without ever showing the agents the ground truth. The harness scores detection,
scope accuracy, diagnosis, agent behaviour, verification and performance against
the fault specification it drew from.

    accesspulse bench --scenarios 1000
    python -m bench.harness --scenarios 200 --workers 4

Ablations re-run the identical corpus with one capability removed, so the
contribution of each part of the system is measured rather than asserted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import statistics
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from accesspulse.faults import FAULT_LIBRARY  # noqa: E402
from accesspulse.runtime import AccessPulseRuntime, ScenarioResult  # noqa: E402

# A narrow but representative slice matrix keeps a 1000-scenario run tractable
# on a laptop while still covering every language, every affected platform and
# an explicit control territory that no fault in the library touches.
BENCH_LANGUAGES = ["en", "fr", "de", "es"]
BENCH_TERRITORIES = ["FR", "DE", "GB", "US", "CA"]
BENCH_PLAYERS = ["ctv-9.3.1", "ctv-9.4.0", "web-4.12.0"]

ABLATIONS = {
    "full": {},
    "no_change_correlation": {"disable_correlation": True},
    "no_probe_confidence": {"disable_abstention": True},
    "no_scope_agent": {"disable_scope": True},
}


# ---------------------------------------------------------------------------
# One scenario
# ---------------------------------------------------------------------------


async def run_scenario(
    fault_id: str,
    seed: int,
    ablation: dict[str, Any] | None = None,
    ticks: int = 6,
    seconds_per_tick: float = 25.0,
) -> dict[str, Any]:
    ablation = ablation or {}
    started = time.perf_counter()
    # Each worker process gets its own SQLite files so parallel runs never share
    # a promise registry or an incident store.
    rt = AccessPulseRuntime(seed=seed, db_prefix=f"bench_{os.getpid()}")
    _apply_ablation(rt, ablation)
    await rt.connect()

    sweep = dict(languages=BENCH_LANGUAGES, territories=BENCH_TERRITORIES,
                 player_versions=BENCH_PLAYERS)
    rt.tick(20, **sweep)
    rt.inject(fault_id)
    for _ in range(ticks):
        rt.tick(seconds_per_tick, **sweep)

    result = await rt.run_incident(settle_seconds=20.0)
    elapsed = time.perf_counter() - started
    row = _row(result, fault_id, seed, elapsed)
    await rt.aclose()
    return row


def _apply_ablation(rt: AccessPulseRuntime, ablation: dict[str, Any]) -> None:
    """Remove one capability, leaving everything else identical."""
    if ablation.get("disable_correlation"):
        rt.coordinator.correlation_agent.run = lambda *a, **k: []  # type: ignore[assignment]
    if ablation.get("disable_abstention"):
        # Treat every probe finding as fully confident: the failure mode this
        # measures is a system that never says "I do not know".
        #
        # The patch has to be applied at every import site. certification.py and
        # verification.py bound the function into their own namespace with
        # `from .assurance import evaluate_report`, so patching only the
        # assurance module would silently leave the verification path - the part
        # that decides whether an incident may close - running the real,
        # abstaining implementation, and the ablation would understate itself.
        import accesspulse.assurance as assurance_mod
        import accesspulse.certification as certification_mod
        import accesspulse.verification as verification_mod

        real_eval = assurance_mod.evaluate_report

        def no_abstain(report, tier, min_confidence=0.0):
            for i, f in enumerate(report.findings):
                if f.abstained:
                    report.findings[i] = f.model_copy(
                        update={"abstained": False, "confidence": 1.0}
                    )
            return real_eval(report, tier, 0.0)

        for mod in (assurance_mod, certification_mod, verification_mod):
            mod.evaluate_report = no_abstain  # type: ignore[assignment]
    if ablation.get("disable_scope"):
        # Scope collapses to "everything", which is what a system without a
        # digital twin and a promise registry can actually say.
        original = rt.coordinator.scope_agent.run

        def wide(incident, alert, group):
            scope = original(incident, alert, group)
            from accesspulse.twin import LANGUAGES, PLAYER_VERSIONS, TERRITORIES

            return scope.model_copy(update={
                "territories": tuple(TERRITORIES),
                "player_versions": tuple(PLAYER_VERSIONS),
                "languages": tuple(LANGUAGES),
                "blast_class": "systemic",
            })

        rt.coordinator.scope_agent.run = wide  # type: ignore[assignment]


def _row(result: ScenarioResult, fault_id: str, seed: int, elapsed: float) -> dict[str, Any]:
    spec = FAULT_LIBRARY[fault_id]
    return {
        "fault_id": fault_id,
        "feature": spec.feature.value,
        "difficulty": spec.difficulty,
        "seed": seed,
        "ground_truth": result.ground_truth.value,
        "detected": result.detected,
        "diagnosis_correct": result.diagnosis_correct,
        "top3_correct": result.top3_correct,
        "top_posterior": result.top_posterior,
        "recovered": result.recovered,
        "rolled_back": result.rolled_back,
        "action_taken": result.action_taken,
        "action_is_corrective": result.action_taken in spec.remediation
        if result.action_taken else False,
        "mcp_calls": result.mcp_calls,
        "scope_precision": result.scope_precision,
        "scope_recall": result.scope_recall,
        "assertions_passing": result.assertions_passing,
        "assertions_total": result.assertions_total,
        "affected_sessions": result.affected_sessions,
        "protected_sessions": result.protected_sessions,
        "unsafe_action": result.unsafe_action,
        "false_closure": result.recovered and not result.diagnosis_correct
        and result.assertions_total > 0 and result.assertions_passing
        < result.assertions_total,
        "time_to_detect_s": result.time_to_detect_s,
        "time_to_recovery_s": result.time_to_recovery_s,
        "wall_seconds": round(elapsed, 3),
        "error": result.error,
    }


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------


def build_corpus(scenarios: int, seed: int) -> list[tuple[str, int]]:
    """Stratified draw: every fault appears, harder faults appear more often."""
    rng = random.Random(seed)
    faults = sorted(FAULT_LIBRARY)
    corpus: list[tuple[str, int]] = []
    # one guaranteed occurrence each, so no fault is silently untested
    for i, fault_id in enumerate(faults):
        corpus.append((fault_id, seed + i))
    weights = [0.5 + FAULT_LIBRARY[f].difficulty for f in faults]
    while len(corpus) < scenarios:
        fault_id = rng.choices(faults, weights=weights, k=1)[0]
        corpus.append((fault_id, seed + len(corpus) * 7919))
    return corpus[:scenarios]


def _worker(chunk: list[tuple[str, int]], ablation_name: str) -> list[dict]:
    """Process-pool entry point. Each worker owns its own event loop."""
    return asyncio.run(_run_chunk(chunk, ablation_name))


async def _run_chunk(chunk: list[tuple[str, int]], ablation_name: str) -> list[dict]:
    ablation = ABLATIONS[ablation_name]
    out = []
    for fault_id, s in chunk:
        try:
            out.append(await run_scenario(fault_id, s, ablation))
        except Exception as exc:  # noqa: BLE001 - a crash is a benchmark data point
            out.append({
                "fault_id": fault_id, "seed": s, "detected": False,
                "diagnosis_correct": False, "top3_correct": False, "recovered": False,
                "rolled_back": False, "unsafe_action": False, "mcp_calls": 0,
                "scope_precision": 0.0, "scope_recall": 0.0, "assertions_passing": 0,
                "assertions_total": 0, "top_posterior": 0.0, "affected_sessions": 0,
                "protected_sessions": 0, "false_closure": False,
                "time_to_detect_s": 0.0, "time_to_recovery_s": 0.0, "wall_seconds": 0.0,
                "feature": FAULT_LIBRARY[fault_id].feature.value,
                "difficulty": FAULT_LIBRARY[fault_id].difficulty,
                "ground_truth": FAULT_LIBRARY[fault_id].failure_class.value,
                "action_taken": None, "action_is_corrective": False,
                "error": f"{type(exc).__name__}: {exc}",
            })
    return out


async def _run_corpus(corpus: list[tuple[str, int]], ablation_name: str,
                      workers: int) -> list[dict]:
    if workers <= 1:
        rows = []
        total = len(corpus)
        for i, (fault_id, s) in enumerate(corpus, 1):
            rows.extend(await _run_chunk([(fault_id, s)], ablation_name))
            if i % 25 == 0 or i == total:
                done = sum(1 for r in rows if r.get("recovered"))
                print(f"  [{ablation_name}] {i}/{total} scenarios "
                      f"({done} recovered)", flush=True)
        return rows

    size = max(1, math.ceil(len(corpus) / (workers * 4)))
    chunks = [corpus[i:i + size] for i in range(0, len(corpus), size)]
    rows: list[dict] = []
    loop = asyncio.get_running_loop()
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [loop.run_in_executor(pool, _worker, c, ablation_name) for c in chunks]
        for i, fut in enumerate(asyncio.as_completed(futures), 1):
            rows.extend(await fut)
            print(f"  [{ablation_name}] chunk {i}/{len(chunks)} "
                  f"({len(rows)} scenarios)", flush=True)
    return rows


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def summarise(rows: list[dict]) -> dict[str, Any]:
    n = len(rows) or 1
    detected = [r for r in rows if r["detected"]]
    acted = [r for r in rows if r.get("action_taken")]
    verified = [r for r in rows if r["assertions_total"]]

    def mean(key: str, source: Iterable[dict] | None = None) -> float:
        values = [r[key] for r in (source if source is not None else rows)
                  if r.get(key) is not None]
        return round(statistics.fmean(values), 4) if values else 0.0

    def rate(pred) -> float:
        return round(sum(1 for r in rows if pred(r)) / n, 4)

    by_feature: dict[str, dict] = defaultdict(lambda: {"n": 0, "top1": 0, "recovered": 0})
    for r in rows:
        row = by_feature[r["feature"]]
        row["n"] += 1
        row["top1"] += int(r["diagnosis_correct"])
        row["recovered"] += int(r["recovered"])
    for row in by_feature.values():
        row["top1_accuracy"] = round(row["top1"] / row["n"], 4)
        row["recovered_rate"] = round(row["recovered"] / row["n"], 4)

    hard = [r for r in rows if r["difficulty"] >= 0.7]
    return {
        "scenarios": len(rows),
        "detection": {
            "detection_rate": rate(lambda r: r["detected"]),
            "mean_time_to_detect_s": mean("time_to_detect_s", detected),
            "missed": sum(1 for r in rows if not r["detected"]),
            "errors": sum(1 for r in rows if r.get("error")),
        },
        "diagnosis": {
            "top1_accuracy": rate(lambda r: r["diagnosis_correct"]),
            "top3_accuracy": rate(lambda r: r["top3_correct"]),
            "mean_top_posterior": mean("top_posterior", detected),
            "top1_accuracy_hard_subset": round(
                sum(1 for r in hard if r["diagnosis_correct"]) / max(1, len(hard)), 4),
            "hard_subset_size": len(hard),
        },
        "scope": {
            "mean_precision": mean("scope_precision", detected),
            "mean_recall": mean("scope_recall", detected),
        },
        "agent": {
            "mean_mcp_calls": mean("mcp_calls", detected),
            "max_mcp_calls": max((r["mcp_calls"] for r in rows), default=0),
            "corrective_action_rate": round(
                sum(1 for r in acted if r["action_is_corrective"]) / max(1, len(acted)), 4),
            "unsafe_action_rate": rate(lambda r: r["unsafe_action"]),
            "actions_proposed": len(acted),
        },
        "verification": {
            "recovered_rate": rate(lambda r: r["recovered"]),
            "rollback_rate": rate(lambda r: r["rolled_back"]),
            "false_closure_rate": rate(lambda r: r["false_closure"]),
            "mean_assertions_passing": mean("assertions_passing", verified),
            "mean_assertions_total": mean("assertions_total", verified),
            "mean_time_to_recovery_s": mean("time_to_recovery_s",
                                            [r for r in rows if r["recovered"]]),
        },
        "impact": {
            "mean_affected_sessions": mean("affected_sessions", detected),
            "mean_protected_sessions": mean("protected_sessions", detected),
            "total_sessions_protected": int(sum(r["protected_sessions"] for r in rows)),
        },
        "performance": {
            "mean_wall_seconds_per_scenario": mean("wall_seconds"),
            "total_wall_seconds": round(sum(r["wall_seconds"] for r in rows), 1),
        },
        "by_feature": dict(by_feature),
        "most_missed_faults": [
            {"fault_id": f, "misdiagnoses": c}
            for f, c in Counter(
                r["fault_id"] for r in rows if not r["diagnosis_correct"]
            ).most_common(8)
        ],
    }


def flatten_for_ablation(summary: dict) -> dict:
    return {
        "detection_rate": summary["detection"]["detection_rate"],
        "top1_accuracy": summary["diagnosis"]["top1_accuracy"],
        "top3_accuracy": summary["diagnosis"]["top3_accuracy"],
        "recovered_rate": summary["verification"]["recovered_rate"],
        "false_closure_rate": summary["verification"]["false_closure_rate"],
        "scope_precision": summary["scope"]["mean_precision"],
        "scope_recall": summary["scope"]["mean_recall"],
        "mean_mcp_calls": summary["agent"]["mean_mcp_calls"],
        "unsafe_action_rate": summary["agent"]["unsafe_action_rate"],
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def ablation_subset(corpus: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """The scenarios every configuration is compared on."""
    return corpus[:max(40, len(corpus) // 5)]


async def _run_ablations(
    corpus: list[tuple[str, int]],
    full_rows: list[dict],
    workers: int,
) -> dict[str, dict]:
    """Re-run the subset with one capability removed at a time.

    The `full` row is the *same subset* scored from the full run, not the
    headline 1,000-scenario number. Comparing a 200-scenario ablation against a
    1,000-scenario baseline would attribute the difference between two corpora
    to the removed capability.
    """
    subset = ablation_subset(corpus)
    indexed = {(r["fault_id"], r["seed"]): r for r in full_rows}
    baseline_rows = [indexed[k] for k in subset if k in indexed]

    out: dict[str, dict] = {}
    for name in ABLATIONS:
        if name == "full":
            out[name] = flatten_for_ablation(summarise(baseline_rows))
            out[name]["scenarios"] = len(baseline_rows)
            continue
        print(f"ablation: {name}")
        ab_rows = await _run_corpus(subset, name, workers)
        out[name] = flatten_for_ablation(summarise(ab_rows))
        out[name]["scenarios"] = len(ab_rows)
    return out


async def rerun_ablations(
    scenarios: int = 1000,
    seed: int = 20260803,
    out: Path = Path("bench/results"),
    workers: int = 1,
) -> dict:
    """Re-run only the ablations against an existing full run.

    Used when the ablation harness itself changes: the 1,000-scenario corpus is
    expensive and unchanged, so it is scored from bench/results/scenarios.jsonl
    rather than executed again.
    """
    out = Path(out)
    summary_path = out / "summary.json"
    rows_path = out / "scenarios.jsonl"
    if not (summary_path.exists() and rows_path.exists()):
        raise SystemExit(f"no existing run in {out}; run the full benchmark first")

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    full_rows = [json.loads(line) for line in rows_path.read_text(encoding="utf-8").splitlines()
                 if line.strip()]
    corpus = build_corpus(scenarios, seed)
    summary["ablations"] = await _run_ablations(corpus, full_rows, workers)
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    _print_summary(summary)
    print(f"\nrewrote the ablation block of {summary_path}")
    return summary


async def run_benchmark(
    scenarios: int = 1000,
    seed: int = 20260803,
    ablations: bool = True,
    out: Path = Path("bench/results"),
    workers: int = 1,
) -> dict:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    corpus = build_corpus(scenarios, seed)
    print(f"AccessPulse benchmark: {len(corpus)} scenarios over "
          f"{len(FAULT_LIBRARY)} fault types, seed {seed}, {workers} worker(s)")

    started = time.perf_counter()
    rows = await _run_corpus(corpus, "full", workers)
    summary = summarise(rows)
    summary.update({
        "seed": seed,
        "fault_types": len(FAULT_LIBRARY),
        "slice_matrix": {
            "languages": BENCH_LANGUAGES,
            "territories": BENCH_TERRITORIES,
            "player_versions": BENCH_PLAYERS,
        },
        "wall_seconds": round(time.perf_counter() - started, 1),
    })

    (out / "scenarios.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    if ablations:
        summary["ablations"] = await _run_ablations(corpus, rows, workers)

    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    _print_summary(summary)
    print(f"\nwrote {out / 'summary.json'} and {out / 'scenarios.jsonl'}")
    return summary


def _print_summary(s: dict) -> None:
    print("\n" + "=" * 62)
    print(f"scenarios                 {s['scenarios']}")
    print(f"detection rate            {s['detection']['detection_rate']:.3f}")
    print(f"top-1 root cause          {s['diagnosis']['top1_accuracy']:.3f}")
    print(f"top-3 root cause          {s['diagnosis']['top3_accuracy']:.3f}")
    print(f"top-1 on hard subset      {s['diagnosis']['top1_accuracy_hard_subset']:.3f} "
          f"(n={s['diagnosis']['hard_subset_size']})")
    print(f"scope precision / recall  {s['scope']['mean_precision']:.3f} / "
          f"{s['scope']['mean_recall']:.3f}")
    print(f"corrective action rate    {s['agent']['corrective_action_rate']:.3f}")
    print(f"recovered and verified    {s['verification']['recovered_rate']:.3f}")
    print(f"rollback rate             {s['verification']['rollback_rate']:.3f}")
    print(f"false closure rate        {s['verification']['false_closure_rate']:.3f}")
    print(f"unsafe action rate        {s['agent']['unsafe_action_rate']:.3f}")
    print(f"mean Grafana MCP calls    {s['agent']['mean_mcp_calls']:.1f}")
    print(f"sessions protected        {s['impact']['total_sessions_protected']:,}")
    print("=" * 62)
    if "ablations" in s:
        print(f"{'configuration':<26}{'detect':>8}{'top-1':>8}{'recover':>9}{'mcp':>7}")
        for name, row in s["ablations"].items():
            print(f"{name:<26}{row['detection_rate']:>8.3f}{row['top1_accuracy']:>8.3f}"
                  f"{row['recovered_rate']:>9.3f}{row['mean_mcp_calls']:>7.1f}")


def main() -> int:
    ap = argparse.ArgumentParser(description="AccessPulse benchmark")
    ap.add_argument("--scenarios", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=20260803)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--no-ablations", action="store_true")
    ap.add_argument("--ablations-only", action="store_true",
                    help="re-run the ablations against the existing full run")
    ap.add_argument("--out", type=Path, default=Path("bench/results"))
    args = ap.parse_args()
    if args.ablations_only:
        asyncio.run(rerun_ablations(args.scenarios, args.seed, args.out, args.workers))
        return 0
    asyncio.run(run_benchmark(args.scenarios, args.seed, not args.no_ablations,
                              args.out, args.workers))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
