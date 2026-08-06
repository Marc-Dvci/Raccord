"""Monotonic token alignment.

Caption drift is not "compare timestamps" - the caption stream and the spoken
stream are different token sequences with insertions, deletions and
substitutions. We recover the correspondence with a monotonic dynamic-time-warp
over token identity, then read the drift off the matched pairs.

The inner loop is the dominant cost of the probe fleet at scale. What lives here
is the *reference* implementation: a plain cell-by-cell walk, written to be read
and checked rather than to be fast. `accelerated/` carries the versions that make
the fleet affordable - an anti-diagonal NumPy wavefront, and fused Triton and
CUDA kernels for accelerator pools - together with the benchmark that compares
them and the parity tests that keep them bit-identical to this file
(docs/PERFORMANCE.md).

The caption probe calls the dispatcher in `accelerated/`, not this function
directly; it falls back here for short windows, where the vectorised path costs
more than it saves.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SUB_COST = 1.0
GAP_COST = 0.6
NEAR_COST = 0.35


@dataclass
class Alignment:
    pairs: list[tuple[int, int]]          # (reference index, hypothesis index)
    unmatched_reference: list[int]
    unmatched_hypothesis: list[int]
    cost: float

    @property
    def match_ratio(self) -> float:
        total = len(self.pairs) + len(self.unmatched_reference)
        return len(self.pairs) / total if total else 0.0


def _token_cost(a: str, b: str) -> float:
    if a == b:
        return 0.0
    if a[:4] == b[:4] and min(len(a), len(b)) >= 4:
        return NEAR_COST
    return SUB_COST


def align_tokens(reference: list[str], hypothesis: list[str]) -> Alignment:
    """Needleman-Wunsch with monotonicity - the alignment a forced aligner would
    produce, computed on token identity rather than acoustics."""
    n, m = len(reference), len(hypothesis)
    if n == 0 or m == 0:
        return Alignment([], list(range(n)), list(range(m)), float(max(n, m)) * GAP_COST)

    # Cost matrix, vectorised over the hypothesis axis.
    d = np.full((n + 1, m + 1), np.inf, dtype=np.float32)
    ptr = np.zeros((n + 1, m + 1), dtype=np.int8)  # 0 diag, 1 up (del), 2 left (ins)
    d[0, 0] = 0.0
    d[1:, 0] = np.arange(1, n + 1) * GAP_COST
    ptr[1:, 0] = 1
    d[0, 1:] = np.arange(1, m + 1) * GAP_COST
    ptr[0, 1:] = 2

    sub = np.empty((n, m), dtype=np.float32)
    for i, ref_tok in enumerate(reference):
        sub[i] = [_token_cost(ref_tok, h) for h in hypothesis]

    for i in range(1, n + 1):
        prev = d[i - 1]
        row = d[i]
        prow = ptr[i]
        srow = sub[i - 1]
        for j in range(1, m + 1):
            diag = prev[j - 1] + srow[j - 1]
            up = prev[j] + GAP_COST
            left = row[j - 1] + GAP_COST
            best = diag
            move = 0
            if up < best:
                best, move = up, 1
            if left < best:
                best, move = left, 2
            row[j] = best
            prow[j] = move

    pairs: list[tuple[int, int]] = []
    unmatched_ref: list[int] = []
    unmatched_hyp: list[int] = []
    i, j = n, m
    while i > 0 or j > 0:
        move = ptr[i, j]
        if i > 0 and j > 0 and move == 0:
            if sub[i - 1, j - 1] <= NEAR_COST:
                pairs.append((i - 1, j - 1))
            else:
                unmatched_ref.append(i - 1)
                unmatched_hyp.append(j - 1)
            i, j = i - 1, j - 1
        elif i > 0 and (move == 1 or j == 0):
            unmatched_ref.append(i - 1)
            i -= 1
        else:
            unmatched_hyp.append(j - 1)
            j -= 1

    pairs.reverse()
    unmatched_ref.reverse()
    unmatched_hyp.reverse()
    return Alignment(pairs, unmatched_ref, unmatched_hyp, float(d[n, m]))


def drift_from_alignment(
    alignment: Alignment,
    reference_times: list[float],
    hypothesis_times: list[float],
) -> tuple[float, float, tuple[float, float]]:
    """Return (median drift seconds, dispersion, 95% interval).

    A positive value means captions arrive *after* the corresponding dialogue.
    The median is used rather than the mean because a handful of mis-aligned
    tokens must not move the operational number.
    """
    if not alignment.pairs:
        return 0.0, 0.0, (0.0, 0.0)
    deltas = np.array(
        [hypothesis_times[j] - reference_times[i] for i, j in alignment.pairs],
        dtype=np.float64,
    )
    median = float(np.median(deltas))
    mad = float(np.median(np.abs(deltas - median)))
    lo = float(np.percentile(deltas, 2.5))
    hi = float(np.percentile(deltas, 97.5))
    return median, mad, (lo, hi)


def monotonic_dtw_path(cost: np.ndarray) -> list[tuple[int, int]]:
    """Classic DTW path over a precomputed cost matrix.

    Retained as the reference implementation the accelerated kernels are
    validated against in tests/test_kernels_parity.py.
    """
    n, m = cost.shape
    acc = np.full((n + 1, m + 1), np.inf, dtype=np.float64)
    acc[0, 0] = 0.0
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            acc[i, j] = cost[i - 1, j - 1] + min(acc[i - 1, j - 1], acc[i - 1, j], acc[i, j - 1])
    path: list[tuple[int, int]] = []
    i, j = n, m
    while i > 0 and j > 0:
        path.append((i - 1, j - 1))
        step = int(np.argmin([acc[i - 1, j - 1], acc[i - 1, j], acc[i, j - 1]]))
        if step == 0:
            i, j = i - 1, j - 1
        elif step == 1:
            i -= 1
        else:
            j -= 1
    path.reverse()
    return path
