"""Wavefront (anti-diagonal) monotonic alignment.

The reference implementation in `align.py` walks the Needleman-Wunsch matrix one
cell at a time in Python. That is the dominant cost of the probe fleet: a
30-second window at a live event is a few hundred reference tokens against a few
hundred caption tokens, and the fleet runs that across the whole slice matrix,
every sweep, for every language.

The recurrence looks sequential, but it is not. Cell (i, j) depends on
(i-1, j-1), (i-1, j) and (i, j-1) - all of which lie on the two preceding
anti-diagonals k-1 and k-2, where k = i + j. So every cell on anti-diagonal k
can be computed at the same time.

That restructuring is what makes the alignment a GPU problem at all, and it is
the same decomposition the Triton and CUDA kernels in this package use. Here it
collapses the inner loop from O(n*m) Python iterations to O(n+m) vectorised
NumPy operations, which is the speed-up measured in docs/PERFORMANCE.md on a
machine with no accelerator.

Numerically identical to the reference, to the bit: same float32 accumulation,
same tie-breaking order (diagonal, then up, then left). `tests/
test_kernels_parity.py` asserts it on random and adversarial inputs.
"""

from __future__ import annotations

import numpy as np

from ..align import GAP_COST, NEAR_COST, SUB_COST, Alignment

__all__ = ["align_tokens_wavefront", "substitution_matrix"]


def substitution_matrix(reference: list[str], hypothesis: list[str]) -> np.ndarray:
    """The pairwise token cost, without the Python double loop.

    Mirrors `align._token_cost`: 0 for an exact match, NEAR_COST when the first
    four characters agree and both tokens are at least four characters long,
    SUB_COST otherwise.
    """
    n, m = len(reference), len(hypothesis)
    if n == 0 or m == 0:
        return np.zeros((n, m), dtype=np.float32)

    # Integer-code the tokens so equality is an integer comparison rather than a
    # string comparison repeated n*m times.
    vocab: dict[str, int] = {}
    ref_ids = np.fromiter(
        (vocab.setdefault(t, len(vocab)) for t in reference), dtype=np.int32, count=n
    )
    hyp_ids = np.fromiter(
        (vocab.setdefault(t, len(vocab)) for t in hypothesis), dtype=np.int32, count=m
    )

    prefixes: dict[str, int] = {}
    ref_pref = np.fromiter(
        (prefixes.setdefault(t[:4], len(prefixes)) for t in reference), dtype=np.int32, count=n
    )
    hyp_pref = np.fromiter(
        (prefixes.setdefault(t[:4], len(prefixes)) for t in hypothesis), dtype=np.int32, count=m
    )
    ref_long = np.fromiter((len(t) >= 4 for t in reference), dtype=bool, count=n)
    hyp_long = np.fromiter((len(t) >= 4 for t in hypothesis), dtype=bool, count=m)

    exact = ref_ids[:, None] == hyp_ids[None, :]
    near = (ref_pref[:, None] == hyp_pref[None, :]) & ref_long[:, None] & hyp_long[None, :]

    sub = np.full((n, m), SUB_COST, dtype=np.float32)
    np.putmask(sub, near, np.float32(NEAR_COST))
    np.putmask(sub, exact, np.float32(0.0))
    return sub


def align_tokens_wavefront(reference: list[str], hypothesis: list[str]) -> Alignment:
    """Same contract as `align.align_tokens`, computed anti-diagonal by anti-diagonal."""
    n, m = len(reference), len(hypothesis)
    if n == 0 or m == 0:
        return Alignment([], list(range(n)), list(range(m)), float(max(n, m)) * GAP_COST)

    sub = substitution_matrix(reference, hypothesis)

    d = np.full((n + 1, m + 1), np.inf, dtype=np.float32)
    ptr = np.zeros((n + 1, m + 1), dtype=np.int8)  # 0 diag, 1 up (del), 2 left (ins)
    d[0, 0] = 0.0
    # The boundary is accumulated in float64 and narrowed on assignment, exactly
    # as the reference does. Computing it in float32 instead shifts the last bit
    # of some boundary cells, which is enough to flip a tie in the traceback and
    # produce a different - equally optimal, but not identical - alignment.
    d[1:, 0] = np.arange(1, n + 1) * GAP_COST
    ptr[1:, 0] = 1
    d[0, 1:] = np.arange(1, m + 1) * GAP_COST
    ptr[0, 1:] = 2

    gap = np.float32(GAP_COST)
    for k in range(2, n + m + 1):
        i = np.arange(max(1, k - m), min(n, k - 1) + 1, dtype=np.intp)
        if i.size == 0:
            continue
        j = k - i

        diag = d[i - 1, j - 1] + sub[i - 1, j - 1]
        up = d[i - 1, j] + gap
        left = d[i, j - 1] + gap

        # Tie-breaking must match the reference exactly: a move is only taken if
        # it is strictly better than the one before it, in the order diag, up,
        # left. Strict comparison is what keeps the traceback identical.
        best = diag
        move = np.zeros(i.size, dtype=np.int8)
        take_up = up < best
        best = np.where(take_up, up, best)
        move = np.where(take_up, np.int8(1), move)
        take_left = left < best
        best = np.where(take_left, left, best)
        move = np.where(take_left, np.int8(2), move)

        d[i, j] = best
        ptr[i, j] = move

    return _traceback(sub, ptr, d, n, m)


def _traceback(sub: np.ndarray, ptr: np.ndarray, d: np.ndarray, n: int, m: int) -> Alignment:
    """Identical walk to the reference implementation."""
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
