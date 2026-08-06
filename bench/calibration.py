"""Probe calibration study.

The benchmark in `harness.py` scores the *system*: did it detect, scope, diagnose,
act and verify. This study scores the *measurement models* underneath it, which
is a different and more basic question: when the probe says "captions are 4.2
seconds late", how wrong is that number?

Every claim in docs/model_card.md comes from here. The ground truth is the fault
parameter that was injected; the probe never sees it.

    python -m bench.calibration                    # table
    python -m bench.calibration --json --out bench/results/calibration.json

Four studies:

  drift        injected caption offset in {0.5 .. 12}s -> reported cap.drift
               error, bias, p95, and whether the reported 95% interval covers
               the truth
  omission     injected token-drop probability -> reported cap.omission_rate
  language     the four programme languages, cue-length text -> identify_language
  abstention   windows with nothing to measure -> does the probe say so, or does
               it invent a confident zero
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from accesspulse import media  # noqa: E402
from accesspulse.contracts import FailureClass, FeatureType  # noqa: E402
from accesspulse.faults import FAULT_LIBRARY, FaultSpec, _scope  # noqa: E402
from accesspulse.probes import caption, text  # noqa: E402
from accesspulse.simulator import MediaSimulator  # noqa: E402

DRIFT_OFFSETS = (0.5, 1.0, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0)
DROP_RATES = (0.05, 0.1, 0.2, 0.3, 0.45)
SEEDS = (11, 23, 37, 41, 53)
SLICES = (("en", "FR", "ctv", "ctv-9.4.0"), ("fr", "FR", "ctv", "ctv-9.3.1"),
          ("de", "DE", "web", "web-4.12.0"), ("es", "GB", "ctv", "ctv-9.4.0"))
WINDOW_S = 30.0


def _calibration_fault(fault_id: str, params: dict[str, Any]) -> FaultSpec:
    """A step fault carrying exactly one known parameter.

    Registered into the library for the duration of the study only. The probes
    cannot read the library, so this is still a blind measurement.
    """
    spec = FaultSpec(
        fault_id, FailureClass.CAPTION_CLOCK_OFFSET, FeatureType.CAPTIONS,
        "calibration", "synthetic fault used only by the calibration study",
        "capenc-pool-a", onset="step", params=params,
        default_scope=_scope(), difficulty=0.5,
    )
    FAULT_LIBRARY[fault_id] = spec
    return spec


def _observe_with(params: dict[str, Any], seed: int,
                  slice_: tuple[str, str, str, str]) -> Any:
    language, territory, platform, player_version = slice_
    fault_id = f"calib.{'_'.join(f'{k}={v}' for k, v in sorted(params.items()))}"
    _calibration_fault(fault_id, params)
    sim = MediaSimulator(seed=seed)
    sim.advance(60.0)
    sim.inject(fault_id, emit_causal_change=False)
    sim.advance(60.0)
    return sim.observe(language, territory, platform, player_version, window_s=WINDOW_S)


# ---------------------------------------------------------------------------
# studies
# ---------------------------------------------------------------------------


@dataclass
class DriftPoint:
    injected: float
    reported: float
    confidence: float
    abstained: bool
    covered: bool


def study_drift() -> dict[str, Any]:
    points: list[DriftPoint] = []
    per_offset: dict[float, list[float]] = {}
    for offset in DRIFT_OFFSETS:
        errors: list[float] = []
        for seed in SEEDS:
            for slice_ in SLICES:
                obs = _observe_with({"drift_seconds": offset}, seed, slice_)
                report = caption.run(obs, slice_[0])
                f = report.by_metric("cap.drift")
                if f is None:
                    continue
                lo, hi = (f.confidence_interval or (0.0, 0.0))
                points.append(DriftPoint(
                    injected=offset, reported=f.score, confidence=f.confidence,
                    abstained=f.abstained,
                    covered=min(lo, hi) - 0.35 <= offset <= max(lo, hi) + 0.35,
                ))
                if not f.abstained:
                    errors.append(f.score - offset)
        per_offset[offset] = errors

    measured = [p for p in points if not p.abstained]
    errors = [p.reported - p.injected for p in measured]
    abs_errors = [abs(e) for e in errors]
    return {
        "samples": len(points),
        "measured": len(measured),
        "abstained": len(points) - len(measured),
        "mean_absolute_error_s": round(statistics.fmean(abs_errors), 3) if abs_errors else None,
        "median_absolute_error_s": round(statistics.median(abs_errors), 3)
        if abs_errors else None,
        "bias_s": round(statistics.fmean(errors), 3) if errors else None,
        "p95_absolute_error_s": round(sorted(abs_errors)[int(0.95 * (len(abs_errors) - 1))], 3)
        if abs_errors else None,
        "interval_coverage": round(sum(p.covered for p in measured) / max(1, len(measured)), 3),
        "mean_confidence": round(statistics.fmean([p.confidence for p in measured]), 3)
        if measured else None,
        "by_injected_offset": {
            str(offset): {
                "n": len(errs),
                "mean_absolute_error_s": round(statistics.fmean([abs(e) for e in errs]), 3)
                if errs else None,
                "bias_s": round(statistics.fmean(errs), 3) if errs else None,
            }
            for offset, errs in per_offset.items()
        },
    }


def study_omission() -> dict[str, Any]:
    rows = []
    for rate in DROP_RATES:
        errors = []
        for seed in SEEDS:
            for slice_ in SLICES:
                obs = _observe_with({"drop_probability": rate}, seed, slice_)
                report = caption.run(obs, slice_[0])
                f = report.by_metric("cap.omission_rate")
                if f is None or f.abstained:
                    continue
                errors.append(f.score - rate)
        rows.append({
            "injected": rate,
            "n": len(errors),
            "mean_absolute_error": round(statistics.fmean([abs(e) for e in errors]), 4)
            if errors else None,
            "bias": round(statistics.fmean(errors), 4) if errors else None,
        })
    all_errors = [abs(r["bias"]) for r in rows if r["bias"] is not None]
    return {
        "by_injected_drop_rate": rows,
        "mean_absolute_bias": round(statistics.fmean(all_errors), 4) if all_errors else None,
    }


def study_language() -> dict[str, Any]:
    """Cue-length text, which is the hard case: short strings, four languages."""
    correct = 0
    total = 0
    confidences: list[float] = []
    confusions: dict[str, dict[str, int]] = {}
    for line in media.SCRIPT:
        for lang in media.SUPPORTED_LANGUAGES:
            sentence = line.text.get(lang)
            if not sentence:
                continue
            detected, confidence = text.identify_language(sentence)
            total += 1
            correct += int(detected == lang)
            confidences.append(confidence)
            confusions.setdefault(lang, {}).setdefault(detected, 0)
            confusions[lang][detected] += 1
    return {
        "samples": total,
        "accuracy": round(correct / max(1, total), 4),
        "mean_confidence": round(statistics.fmean(confidences), 3) if confidences else None,
        "confusions": {src: {d: c for d, c in sorted(row.items()) if d != src}
                       for src, row in confusions.items()},
        "note": "Single dialogue lines. The probe identifies over a whole window of "
                "cues, which is a much longer string than this.",
    }


def study_abstention() -> dict[str, Any]:
    """A probe that cannot measure must say so rather than report a confident zero."""
    results = {"no_cues": [], "no_dialogue": []}
    for seed in SEEDS:
        for slice_ in SLICES:
            # cue_rate_multiplier=0 -> the caption track emits nothing at all
            obs = _observe_with({"cue_rate_multiplier": 0.0}, seed, slice_)
            report = caption.run(obs, slice_[0])
            f = report.by_metric("cap.drift")
            results["no_cues"].append({
                "abstained": bool(f and f.abstained),
                "confidence": float(f.confidence) if f else None,
                "data_quality": f.data_quality if f else None,
            })

    no_cues = results["no_cues"]
    return {
        "windows_with_no_caption_content": len(no_cues),
        "abstention_rate": round(sum(r["abstained"] for r in no_cues) / max(1, len(no_cues)), 3),
        "confident_zero_rate": round(
            sum(1 for r in no_cues if not r["abstained"] and (r["confidence"] or 0) > 0.5)
            / max(1, len(no_cues)), 3),
        "note": "confident_zero_rate is the number that matters: a probe reporting "
                "'0.0 seconds of drift, high confidence' for a window with no captions "
                "at all would hide a total loss of service.",
    }


# ---------------------------------------------------------------------------


def run() -> dict[str, Any]:
    return {
        "probe_versions": {
            "caption": caption.VERSION,
        },
        "window_seconds": WINDOW_S,
        "slices": ["/".join(s) for s in SLICES],
        "seeds": list(SEEDS),
        "drift": study_drift(),
        "omission": study_omission(),
        "language_identification": study_language(),
        "abstention": study_abstention(),
    }


def _print(report: dict[str, Any]) -> None:
    d = report["drift"]
    print("=" * 66)
    print(f"caption drift estimator ({report['drift']['measured']} measured samples, "
          f"{d['abstained']} abstentions)")
    print(f"  mean absolute error      {d['mean_absolute_error_s']} s")
    print(f"  median absolute error    {d['median_absolute_error_s']} s")
    print(f"  bias                     {d['bias_s']} s")
    print(f"  p95 absolute error       {d['p95_absolute_error_s']} s")
    print(f"  95% interval coverage    {d['interval_coverage']}")
    print(f"  mean reported confidence {d['mean_confidence']}")
    print("\n  injected   n   MAE      bias")
    for offset, row in d["by_injected_offset"].items():
        print(f"  {offset:>7} s {row['n']:>3}   {row['mean_absolute_error_s']:<8} "
              f"{row['bias_s']}")

    print("\n" + "=" * 66)
    print("caption omission estimator")
    for row in report["omission"]["by_injected_drop_rate"]:
        print(f"  drop {row['injected']:<5} n={row['n']:<3} "
              f"MAE {row['mean_absolute_error']} bias {row['bias']}")

    lang = report["language_identification"]
    print("\n" + "=" * 66)
    print(f"language identification: {lang['accuracy']:.3f} over {lang['samples']} lines "
          f"(mean confidence {lang['mean_confidence']})")
    for src, row in lang["confusions"].items():
        if row:
            print(f"  {src} confused with {row}")

    ab = report["abstention"]
    print("\n" + "=" * 66)
    print(f"abstention on empty windows: {ab['abstention_rate']:.3f}")
    print(f"confident-zero rate:         {ab['confident_zero_rate']:.3f}")
    print("=" * 66)


def main() -> int:
    ap = argparse.ArgumentParser(description="AccessPulse probe calibration study")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    report = run()
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print(report)
        if args.out:
            print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
