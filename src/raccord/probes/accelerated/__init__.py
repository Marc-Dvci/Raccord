"""Accelerated alignment backends.

`align.py` holds the reference implementation: a plain Needleman-Wunsch walk in
Python, easy to read and easy to check. This package holds the implementations
that make the probe fleet affordable at scale, and the machinery that keeps them
honest.

    backend            requires                selected when
    -----------------  ----------------------  ---------------------------------
    triton             triton + CUDA GPU       an accelerator is present
    wavefront (NumPy)  numpy                   always available - the default
    reference          nothing                 explicitly requested, or parity

Selection is automatic and can be pinned with `RACCORD_ALIGN_BACKEND=reference |
wavefront | triton`. Every backend produces a *bit-identical* alignment, not
merely an equally optimal one: same float32 accumulation, same tie-breaking
order. `tests/test_kernels_parity.py` asserts it, and it is the reason a
backend can be swapped under a live probe without moving any published metric.

Measured speed-ups: docs/PERFORMANCE.md · reproduce with
`python -m raccord.probes.accelerated.benchmark`.
"""

from __future__ import annotations

import os
from typing import Callable

from ..align import Alignment
from ..align import align_tokens as align_tokens_reference
from .wavefront import align_tokens_wavefront, substitution_matrix

__all__ = [
    "Alignment",
    "align_tokens_reference",
    "align_tokens_wavefront",
    "substitution_matrix",
    "align_tokens_accelerated",
    "available_backends",
    "active_backend",
    "get_backend",
]


def available_backends() -> dict[str, bool]:
    """Which backends this host can actually run."""
    from . import triton_kernels

    return {
        "reference": True,
        "wavefront": True,
        "triton": triton_kernels.available(),
    }


def active_backend() -> str:
    """The backend that will be used, honouring RACCORD_ALIGN_BACKEND if it is set."""
    requested = os.environ.get("RACCORD_ALIGN_BACKEND", "").strip().lower()
    available = available_backends()
    if requested:
        if requested not in available:
            raise ValueError(
                f"unknown align backend {requested!r}; expected one of {sorted(available)}"
            )
        if not available[requested]:
            raise RuntimeError(
                f"align backend {requested!r} was requested but is not available on this host"
            )
        return requested
    return "triton" if available["triton"] else "wavefront"


def get_backend(name: str | None = None) -> Callable[[list[str], list[str]], Alignment]:
    name = name or active_backend()
    if name == "reference":
        return align_tokens_reference
    if name == "wavefront":
        return align_tokens_wavefront
    if name == "triton":
        from .triton_kernels import align_tokens_triton

        return align_tokens_triton
    raise ValueError(f"unknown align backend {name!r}")


# Below this many cells the vectorised backends lose to the plain Python walk:
# building the substitution matrix and launching an operation per anti-diagonal
# costs more than the work saved. Measured, not guessed - at 32x32 the NumPy
# wavefront runs at 0.5x the reference, breaks even near 64x64, and is 9.9x
# faster at 1024x1024 (docs/PERFORMANCE.md). A 10-second probe window is around
# 30 tokens, so the short-input path is the common one and it matters that it is
# not made slower.
CROSSOVER_CELLS = 4096


def align_tokens_accelerated(reference: list[str], hypothesis: list[str]) -> Alignment:
    """The fastest backend for this input, with the reference's exact result."""
    if len(reference) * len(hypothesis) < CROSSOVER_CELLS:
        return align_tokens_reference(reference, hypothesis)
    return get_backend()(reference, hypothesis)
