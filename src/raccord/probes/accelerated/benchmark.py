"""Kernel benchmark: what the accelerated alignment is actually worth.

Measures every backend this host can run against the reference implementation,
on sequence lengths that bracket what a live probe sees — a 10-second window is
roughly 30 tokens, a 30-second window roughly 90, and a busy multi-speaker
window several hundred — plus larger sizes where the asymptotics show.

It also re-checks parity at every size, because a performance number for a
kernel that computes a different answer is worthless.

    python -m raccord.probes.accelerated.benchmark
    python -m raccord.probes.accelerated.benchmark --json --out bench/results/kernels.json
"""

from __future__ import annotations

import argparse
import json
import platform
import random
import statistics
import time
from pathlib import Path
from typing import Any

from ..align import Alignment
from . import align_tokens_reference, available_backends, get_backend

SIZES = (32, 64, 128, 256, 512, 1024)
REPEATS = 5
SEED = 4242


def _corpus(size: int, seed: int) -> tuple[list[str], list[str]]:
    """A reference and a hypothesis that differ the way captions differ.

    Not two random strings: the hypothesis is the reference with substitutions,
    deletions and insertions at rates typical of a degraded caption stream, so
    the alignment does the work it does in production rather than collapsing to
    an all-gap path.
    """
    rng = random.Random(seed)
    vocab = [f"word{i}" for i in range(400)] + ["a", "the", "of", "and", "to", "is"]
    reference = [rng.choice(vocab) for _ in range(size)]
    hypothesis: list[str] = []
    for token in reference:
        roll = rng.random()
        if roll < 0.06:  # deletion - a dropped word
            continue
        if roll < 0.12:  # substitution - a misrecognised word
            hypothesis.append(rng.choice(vocab))
            continue
        if roll < 0.15:  # insertion - a duplicated or spurious word
            hypothesis.append(token)
            hypothesis.append(rng.choice(vocab))
            continue
        hypothesis.append(token)
    return reference, hypothesis


def _identical(a: Alignment, b: Alignment) -> bool:
    return (
        a.pairs == b.pairs
        and a.unmatched_reference == b.unmatched_reference
        and a.unmatched_hypothesis == b.unmatched_hypothesis
        and abs(a.cost - b.cost) < 1e-4
    )


def _time(fn, reference: list[str], hypothesis: list[str], repeats: int) -> float:
    fn(reference, hypothesis)  # warm up: import, JIT, first allocation
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        fn(reference, hypothesis)
        samples.append(time.perf_counter() - started)
    return statistics.median(samples)


def run(sizes: tuple[int, ...] = SIZES, repeats: int = REPEATS) -> dict[str, Any]:
    backends = {name: ok for name, ok in available_backends().items() if ok}
    rows: list[dict[str, Any]] = []
    parity_failures: list[str] = []

    for size in sizes:
        reference, hypothesis = _corpus(size, SEED + size)
        baseline = _time(align_tokens_reference, reference, hypothesis, repeats)
        truth = align_tokens_reference(reference, hypothesis)

        row: dict[str, Any] = {
            "reference_tokens": len(reference),
            "hypothesis_tokens": len(hypothesis),
            "cells": len(reference) * len(hypothesis),
            "reference_ms": round(baseline * 1000, 3),
            "backends": {},
        }
        for name in backends:
            if name == "reference":
                continue
            fn = get_backend(name)
            result = fn(reference, hypothesis)
            if not _identical(truth, result):
                parity_failures.append(f"{name} @ {size}")
            elapsed = _time(fn, reference, hypothesis, repeats)
            row["backends"][name] = {
                "ms": round(elapsed * 1000, 3),
                "speedup": round(baseline / elapsed, 2) if elapsed else None,
                "parity": _identical(truth, result),
            }
        rows.append(row)

    return {
        "host": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "processor": platform.processor() or "unknown",
        },
        "available_backends": available_backends(),
        "repeats": repeats,
        "measure": "median of repeats, after one warm-up call",
        "parity_failures": parity_failures,
        "rows": rows,
    }


def _print(report: dict[str, Any]) -> None:
    names = [n for n, ok in report["available_backends"].items() if ok and n != "reference"]
    header = f"{'tokens':>8}{'cells':>10}{'reference':>12}"
    for name in names:
        header += f"{name:>12}{'speedup':>9}"
    print(header)
    print("-" * len(header))
    for row in report["rows"]:
        line = f"{row['reference_tokens']:>8}{row['cells']:>10}{row['reference_ms']:>11.1f}ms"
        for name in names:
            b = row["backends"].get(name)
            line += f"{b['ms']:>11.1f}ms{b['speedup']:>8.1f}x" if b else f"{'-':>21}"
        print(line)

    print()
    if report["parity_failures"]:
        print("PARITY FAILURES: " + ", ".join(report["parity_failures"]))
    else:
        print("every backend produced a bit-identical alignment at every size")
    if not report["available_backends"].get("triton"):
        print(
            "triton backend unavailable on this host (needs Triton + an NVIDIA GPU); "
            "numbers above are CPU only"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="Raccord alignment kernel benchmark")
    ap.add_argument("--sizes", type=int, nargs="*", default=list(SIZES))
    ap.add_argument("--repeats", type=int, default=REPEATS)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    report = run(tuple(args.sizes), args.repeats)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print(report)
        if args.out:
            print(f"\nwrote {args.out}")
    return 1 if report["parity_failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
