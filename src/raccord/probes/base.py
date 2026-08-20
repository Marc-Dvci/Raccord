"""Common probe plumbing."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from ..contracts import ModelFinding


@dataclass
class ProbeReport:
    probe: str
    probe_version: str
    slice_key: str
    findings: list[ModelFinding] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def by_metric(self, metric: str) -> ModelFinding | None:
        for f in self.findings:
            if f.metric == metric:
                return f
        return None

    def value(self, metric: str, default: float = 0.0) -> float:
        f = self.by_metric(metric)
        return default if f is None or f.abstained else f.score


def finding(
    probe: str,
    version: str,
    metric: str,
    score: float,
    *,
    unit: str = "",
    confidence: float = 0.9,
    ci: tuple[float, float] | None = None,
    interval: tuple[float, float] | None = None,
    abstained: bool = False,
    data_quality: str = "ok",
    limitations: tuple[str, ...] = (),
    detail: dict[str, Any] | None = None,
) -> ModelFinding:
    return ModelFinding(
        finding_id=f"mf-{uuid.uuid4().hex[:10]}",
        probe=probe,
        model_version=version,
        metric=metric,
        score=float(score),
        unit=unit,
        confidence=float(confidence),
        confidence_interval=ci,
        evidence_interval=interval,
        abstained=abstained,
        data_quality=data_quality,  # type: ignore[arg-type]
        known_limitations=limitations,
        detail=detail or {},
    )
