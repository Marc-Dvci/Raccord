"""Every alignment backend must produce the reference's exact answer.

Not "an equally optimal alignment" - the *same* alignment. Two paths through a
Needleman-Wunsch matrix can share a cost and disagree about which caption token
matched which spoken token, and drift is read off those matched pairs. If the
backends disagreed, swapping one under a live probe would move a published
metric with no change to the measurement it claims to make.

So the property under test is bit-identity of pairs, unmatched sets and cost,
including on inputs chosen to produce ties.
"""

from __future__ import annotations

import random

import numpy as np
import pytest

from accesspulse.probes.accelerated import (
    align_tokens_accelerated,
    align_tokens_reference,
    align_tokens_wavefront,
    available_backends,
    get_backend,
    substitution_matrix,
)
from accesspulse.probes.align import _token_cost, monotonic_dtw_path

BACKENDS = [name for name, ok in available_backends().items() if ok and name != "reference"]


def _same(a, b) -> bool:
    return (a.pairs == b.pairs
            and a.unmatched_reference == b.unmatched_reference
            and a.unmatched_hypothesis == b.unmatched_hypothesis
            and abs(a.cost - b.cost) < 1e-4)


def _corpus(rng: random.Random, n: int, m: int, vocab: list[str]):
    return ([rng.choice(vocab) for _ in range(n)],
            [rng.choice(vocab) for _ in range(m)])


@pytest.mark.parametrize("backend", BACKENDS)
def test_backend_matches_the_reference_on_random_input(backend):
    fn = get_backend(backend)
    rng = random.Random(20260803)
    vocab = [f"word{i}" for i in range(40)] + ["a", "of", "the", "be"]
    for _ in range(40):
        reference, hypothesis = _corpus(rng, rng.randint(0, 90), rng.randint(0, 90), vocab)
        assert _same(align_tokens_reference(reference, hypothesis),
                     fn(reference, hypothesis))


@pytest.mark.parametrize("backend", BACKENDS)
def test_backend_matches_the_reference_when_every_move_ties(backend):
    """The adversarial case: one repeated token, so diagonal, up and left all
    cost the same and only the tie-breaking order decides the path."""
    fn = get_backend(backend)
    for n, m in ((5, 5), (8, 3), (3, 8), (16, 16), (40, 37)):
        reference = ["same"] * n
        hypothesis = ["same"] * m
        assert _same(align_tokens_reference(reference, hypothesis),
                     fn(reference, hypothesis))


@pytest.mark.parametrize("backend", BACKENDS)
def test_backend_handles_empty_and_degenerate_input(backend):
    fn = get_backend(backend)
    for reference, hypothesis in (([], []), (["a"], []), ([], ["a"]),
                                  (["one"], ["one"]), (["one"], ["two"])):
        assert _same(align_tokens_reference(reference, hypothesis),
                     fn(reference, hypothesis))


@pytest.mark.parametrize("backend", BACKENDS)
def test_backend_matches_on_near_matches(backend):
    """NEAR_COST applies only when both tokens are at least four characters and
    share a four-character prefix - the boundary the vectorised cost matrix has
    to reproduce exactly."""
    fn = get_backend(backend)
    reference = ["projector", "project", "pro", "proj", "archive", "arch"]
    hypothesis = ["projection", "proj", "pro", "project", "archivist", "arch"]
    assert _same(align_tokens_reference(reference, hypothesis),
                 fn(reference, hypothesis))


def test_substitution_matrix_matches_the_scalar_cost_function():
    rng = random.Random(11)
    vocab = ["a", "be", "the", "proj", "project", "projector", "archive", "archivist"]
    reference, hypothesis = _corpus(rng, 30, 25, vocab)
    expected = np.array([[_token_cost(r, h) for h in hypothesis] for r in reference],
                        dtype=np.float32)
    assert np.array_equal(expected, substitution_matrix(reference, hypothesis))


def test_dispatcher_short_circuits_small_inputs_to_the_reference():
    """Small windows are the common case; the dispatcher must not make them slower."""
    rng = random.Random(5)
    vocab = [f"w{i}" for i in range(20)]
    reference, hypothesis = _corpus(rng, 25, 25, vocab)   # 625 cells, below crossover
    assert _same(align_tokens_reference(reference, hypothesis),
                 align_tokens_accelerated(reference, hypothesis))

    reference, hypothesis = _corpus(rng, 120, 120, vocab)  # above crossover
    assert _same(align_tokens_reference(reference, hypothesis),
                 align_tokens_accelerated(reference, hypothesis))


def test_wavefront_agrees_with_the_classic_dtw_path_on_a_monotonic_cost():
    """A sanity check against the other reference in align.py: on a cost matrix
    whose optimum is the main diagonal, both recover it."""
    size = 12
    cost = np.ones((size, size), dtype=np.float64)
    np.fill_diagonal(cost, 0.0)
    assert monotonic_dtw_path(cost) == [(i, i) for i in range(size)]

    tokens = [f"t{i}" for i in range(size)]
    alignment = align_tokens_wavefront(tokens, list(tokens))
    assert alignment.pairs == [(i, i) for i in range(size)]
    assert alignment.cost == pytest.approx(0.0)
