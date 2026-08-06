"""Fused Triton kernels for the alignment inner loop.

Two kernels, matching the two phases of `wavefront.py`:

`substitution_kernel`
    Builds the n x m token-cost matrix in one pass. Tokens arrive already
    integer-coded (identity id, 4-character-prefix id, and a length>=4 flag), so
    the kernel is integer comparison and select - no string handling on device.

`wavefront_kernel`
    One launch per anti-diagonal. Every cell on anti-diagonal k = i + j depends
    only on k-1 and k-2, so the whole diagonal is computed by one grid of
    threads. The kernel writes both the accumulated cost and the traceback
    pointer; the traceback itself stays on the host, where it is O(n + m) and
    inherently serial.

Tie-breaking is strict-less-than in the order diagonal, up, left - the same
order as the reference - so the pointer matrix, and therefore the alignment, is
identical rather than merely equally optimal.

Triton is an optional dependency (`pip install triton`, Linux + NVIDIA). When it
is absent this module imports cleanly and `available()` returns False; the
dispatcher in `__init__.py` then selects the NumPy wavefront implementation.
Nothing in AccessPulse requires an accelerator.
"""

from __future__ import annotations

import numpy as np

try:  # pragma: no cover - exercised only on accelerator hosts
    import torch
    import triton
    import triton.language as tl

    _HAVE_TRITON = torch.cuda.is_available()
except Exception:  # noqa: BLE001 - any import failure means "no accelerator"
    torch = None  # type: ignore[assignment]
    triton = None  # type: ignore[assignment]
    tl = None  # type: ignore[assignment]
    _HAVE_TRITON = False


def available() -> bool:
    return bool(_HAVE_TRITON)


if _HAVE_TRITON:  # pragma: no cover - requires an NVIDIA GPU

    @triton.jit
    def substitution_kernel(
        ref_id_ptr, hyp_id_ptr,          # int32[n], int32[m]  token identity
        ref_pre_ptr, hyp_pre_ptr,        # int32[n], int32[m]  4-char prefix id
        ref_long_ptr, hyp_long_ptr,      # int8[n],  int8[m]   len >= 4
        out_ptr,                         # float32[n, m]
        n, m,
        SUB_COST: tl.constexpr, NEAR_COST: tl.constexpr,
        BLOCK_N: tl.constexpr, BLOCK_M: tl.constexpr,
    ):
        pid_n = tl.program_id(0)
        pid_m = tl.program_id(1)
        offs_n = pid_n * BLOCK_N + tl.arange(0, BLOCK_N)
        offs_m = pid_m * BLOCK_M + tl.arange(0, BLOCK_M)
        mask_n = offs_n < n
        mask_m = offs_m < m

        ref_id = tl.load(ref_id_ptr + offs_n, mask=mask_n, other=-1)
        hyp_id = tl.load(hyp_id_ptr + offs_m, mask=mask_m, other=-2)
        ref_pre = tl.load(ref_pre_ptr + offs_n, mask=mask_n, other=-1)
        hyp_pre = tl.load(hyp_pre_ptr + offs_m, mask=mask_m, other=-2)
        ref_long = tl.load(ref_long_ptr + offs_n, mask=mask_n, other=0)
        hyp_long = tl.load(hyp_long_ptr + offs_m, mask=mask_m, other=0)

        exact = ref_id[:, None] == hyp_id[None, :]
        near = (ref_pre[:, None] == hyp_pre[None, :]) & \
               (ref_long[:, None] != 0) & (hyp_long[None, :] != 0)

        cost = tl.where(exact, 0.0, tl.where(near, NEAR_COST, SUB_COST))
        out = out_ptr + offs_n[:, None] * m + offs_m[None, :]
        tl.store(out, cost, mask=mask_n[:, None] & mask_m[None, :])

    @triton.jit
    def wavefront_kernel(
        d_ptr,                           # float32[(n+1) * (m+1)]
        ptr_ptr,                         # int8[(n+1) * (m+1)]
        sub_ptr,                         # float32[n * m]
        k, i_lo, count,                  # this anti-diagonal
        n, m,
        GAP_COST: tl.constexpr,
        BLOCK: tl.constexpr,
    ):
        offs = tl.program_id(0) * BLOCK + tl.arange(0, BLOCK)
        mask = offs < count
        i = i_lo + offs
        j = k - i
        stride = m + 1

        diag = tl.load(d_ptr + (i - 1) * stride + (j - 1), mask=mask, other=0.0) + \
            tl.load(sub_ptr + (i - 1) * m + (j - 1), mask=mask, other=0.0)
        up = tl.load(d_ptr + (i - 1) * stride + j, mask=mask, other=0.0) + GAP_COST
        left = tl.load(d_ptr + i * stride + (j - 1), mask=mask, other=0.0) + GAP_COST

        best = diag
        move = tl.zeros(best.shape, dtype=tl.int8)
        take = up < best
        best = tl.where(take, up, best)
        move = tl.where(take, 1, move)
        take = left < best
        best = tl.where(take, left, best)
        move = tl.where(take, 2, move)

        tl.store(d_ptr + i * stride + j, best, mask=mask)
        tl.store(ptr_ptr + i * stride + j, move, mask=mask)


def _codes(reference: list[str], hypothesis: list[str]) -> tuple[np.ndarray, ...]:
    """Integer-code both sequences against one shared vocabulary.

    The kernel compares ids, so reference and hypothesis must be coded together -
    coding them separately would make every cross-sequence comparison meaningless.
    """
    ids: dict[str, int] = {}
    pres: dict[str, int] = {}

    def code(tokens: list[str]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        return (
            np.fromiter((ids.setdefault(t, len(ids)) for t in tokens),
                        dtype=np.int32, count=len(tokens)),
            np.fromiter((pres.setdefault(t[:4], len(pres)) for t in tokens),
                        dtype=np.int32, count=len(tokens)),
            np.fromiter((len(t) >= 4 for t in tokens), dtype=np.int8, count=len(tokens)),
        )

    return (*code(reference), *code(hypothesis))


def align_tokens_triton(reference: list[str], hypothesis: list[str]):  # pragma: no cover
    """Same contract as `align.align_tokens`, computed on device.

    The traceback runs on the host: it is O(n + m), strictly serial, and copying
    the pointer matrix back costs less than a device-side serial walk.
    """
    if not _HAVE_TRITON:
        raise RuntimeError("Triton or CUDA is not available on this host")

    from ..align import GAP_COST, NEAR_COST, SUB_COST, Alignment
    from .wavefront import _traceback

    n, m = len(reference), len(hypothesis)
    if n == 0 or m == 0:
        return Alignment([], list(range(n)), list(range(m)), float(max(n, m)) * GAP_COST)

    dev = "cuda"
    ref_id, ref_pre, ref_long, hyp_id, hyp_pre, hyp_long = _codes(reference, hypothesis)

    def to(a: np.ndarray):
        return torch.from_numpy(a).to(dev)

    sub = torch.empty((n, m), dtype=torch.float32, device=dev)
    grid = (triton.cdiv(n, 64), triton.cdiv(m, 64))
    substitution_kernel[grid](
        to(ref_id), to(hyp_id), to(ref_pre), to(hyp_pre), to(ref_long), to(hyp_long),
        sub, n, m, SUB_COST=SUB_COST, NEAR_COST=NEAR_COST, BLOCK_N=64, BLOCK_M=64,
    )

    d = torch.full((n + 1, m + 1), float("inf"), dtype=torch.float32, device=dev)
    ptr = torch.zeros((n + 1, m + 1), dtype=torch.int8, device=dev)
    d[0, 0] = 0.0
    d[1:, 0] = torch.from_numpy(np.arange(1, n + 1) * GAP_COST).to(dev, torch.float32)
    d[0, 1:] = torch.from_numpy(np.arange(1, m + 1) * GAP_COST).to(dev, torch.float32)
    ptr[1:, 0] = 1
    ptr[0, 1:] = 2

    for k in range(2, n + m + 1):
        i_lo = max(1, k - m)
        count = min(n, k - 1) - i_lo + 1
        if count <= 0:
            continue
        wavefront_kernel[(triton.cdiv(count, 128),)](
            d, ptr, sub, k, i_lo, count, n, m, GAP_COST=GAP_COST, BLOCK=128,
        )

    return _traceback(sub.cpu().numpy(), ptr.cpu().numpy(), d.cpu().numpy(), n, m)
