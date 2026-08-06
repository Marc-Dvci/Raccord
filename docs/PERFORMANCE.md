# Performance — the alignment kernels

Caption drift is not "compare timestamps". The caption stream and the spoken stream are
different token sequences with insertions, deletions and substitutions, so the correspondence
has to be recovered by a monotonic alignment before any timing number means anything. That
alignment is the dominant cost of the probe fleet: the fleet runs it for every language ×
territory × platform × player build, every sweep, for the whole event.

This document measures what the accelerated backends are worth, and how the dispatcher selects
the right one for each input size.

Reproduce: `python -m accesspulse.probes.accelerated.benchmark --out bench/results/kernels.json`

---

## 1. The restructuring

The Needleman-Wunsch recurrence looks sequential. It is not. Cell (i, j) depends on (i−1, j−1),
(i−1, j) and (i, j−1) — all on the two preceding **anti-diagonals** k−1 and k−2, where
k = i + j. Every cell on anti-diagonal k is therefore independent of every other cell on it.

```
      j →                     anti-diagonal k = i+j
   . 1 2 3 4 5                 all cells on one k
 i 1 ╲ ╲ ╲ ╲ ╲                 are computed together
 ↓ 2 ╲ ╲ ╲ ╲ ╲                 from k-1 and k-2
   3 ╲ ╲ ╲ ╲ ╲
```

That single observation is what turns the inner loop from O(n·m) sequential steps into O(n+m)
parallel steps, and it is the same decomposition in all three backends:

| Backend | Implementation | Requires |
|---|---|---|
| `reference` | Cell-by-cell Python walk (`probes/align.py`) | nothing |
| `wavefront` | One vectorised NumPy operation per anti-diagonal (`accelerated/wavefront.py`) | numpy |
| `triton` | One kernel launch per anti-diagonal, plus a fused token-cost kernel (`accelerated/triton_kernels.py`) | Triton + NVIDIA GPU |
| *(CUDA source)* | The same two phases plus a banded variant (`accelerated/cuda/align_wavefront.cu`) | nvcc + NVIDIA GPU |

---

## 2. Measured results

Host: Windows 11, Python 3.12, 8 logical CPUs, **no accelerator**. Median of 5 runs after one
warm-up. Sequences are a reference and a degraded hypothesis with realistic substitution,
deletion and insertion rates — not two random strings, which would collapse to an all-gap path
and understate the work.

| Tokens | Cells | `reference` | `wavefront` | Speed-up |
|---:|---:|---:|---:|---:|
| 32 | 1,056 | 3.6 ms | 6.6 ms | **0.5×** |
| 64 | 3,776 | 12.7 ms | 12.7 ms | 1.0× |
| 128 | 16,000 | 56.9 ms | 29.9 ms | 1.9× |
| 256 | 65,536 | 248.4 ms | 63.4 ms | 3.9× |
| 512 | 253,952 | 888.3 ms | 135.2 ms | 6.6× |
| 1024 | 1,014,784 | 3,704.0 ms | 375.6 ms | **9.9×** |

**The first row is the interesting one.** At 32 tokens the vectorised backend is *twice as
slow*: building the substitution matrix and issuing an operation per anti-diagonal costs more
than the work it saves. Break-even is near 64 × 64.

A 10-second probe window is roughly 30 tokens. The short-input case is the common case, so the
dispatcher short-circuits it:

```python
CROSSOVER_CELLS = 4096

def align_tokens_accelerated(reference, hypothesis):
    if len(reference) * len(hypothesis) < CROSSOVER_CELLS:
        return align_tokens_reference(reference, hypothesis)
    return get_backend()(reference, hypothesis)
```

The crossover is measured rather than guessed, and
`test_dispatcher_short_circuits_small_inputs_to_the_reference` pins it, so the fast path is
genuinely fast at every window size the fleet actually sees.

**Where the speed-up matters:** long windows during dense multi-speaker dialogue, backfill of a
whole event for post-incident review, and the 1,000-scenario benchmark, which alone runs the
alignment hundreds of thousands of times.

The Triton and CUDA backends implement the same anti-diagonal decomposition and are
parity-tested against the reference. On an accelerator host,
`python -m accesspulse.probes.accelerated.benchmark` adds a `triton` column automatically; the
figures published here are the CPU measurements taken on this build.

---

## 3. Parity is the precondition

A faster kernel that computes a *different* alignment is not an optimisation, it is a silent
change to every drift number the product has ever published. Two paths through the matrix can
share a total cost and still disagree about which caption token matched which spoken token —
and drift is the median over those matched pairs.

So the requirement is **bit-identity**, not equivalence of cost:

- the same float32 accumulation, including the boundary row and column, which are accumulated
  in float64 and narrowed on assignment exactly as the reference does (computing them in
  float32 shifts the last bit of some cells, which is enough to flip a tie and produce a
  different — equally optimal — alignment),
- the same tie-breaking: strict less-than in the order diagonal, up, left.

`tests/test_kernels_parity.py` asserts it on random inputs, on the adversarial all-ties input
(one repeated token, where only tie-breaking decides the path), on empty and degenerate inputs,
and across the NEAR_COST prefix boundary. The benchmark re-checks parity at every size and exits
non-zero if any backend disagrees.

---

## 4. The banded variant

`align_wavefront.cu` also carries `wavefront_band_kernel`, which restricts the recurrence to a
Sakoe-Chiba band around the diagonal, turning O(n·m) into O((n+m)·band).

The justification is domain, not just arithmetic: a caption stream more than a few seconds out
of correspondence is not a harder alignment problem, it is a *different failure class* — source
loss, or wrong-language delivery — and both have their own detectors and their own metrics
(`cap.availability`, `cap.wrong_language`). Cells outside the band keep +∞ rather than being
approximated, so a path is never silently allowed to leave it: the cost reflects that the band
was too narrow and the host widens it and retries.

Not enabled by default. The band width is an operational parameter, and a wrong one turns a
measurement into an assumption.

---

## 5. Where the rest of the time goes

The alignment is the hot loop, not the whole cost. Per benchmark scenario (mean 15.7 s wall, 1
worker):

| Phase | Share | Note |
|---|---|---|
| Probe sweeps across the slice matrix | largest | alignment plus the other 30 metrics |
| Verification | large | re-runs the fleet on original, adjacent and dependent scope ([ADR 0006](adr/0006-verification-re-measures-including-adjacent-scope.md)) |
| Grafana MCP calls | ~17 per incident | in-process stub in the benchmark; a network round-trip in deployment |
| Ranking, policy, approval, executor | small | arithmetic over typed records |

The benchmark parallelises across processes (`--workers 7`), which is why 15,693 s of CPU time
completes in 2,654 s of wall time.
