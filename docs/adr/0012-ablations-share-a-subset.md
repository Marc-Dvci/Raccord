# ADR 0012 — Ablations are compared against the same subset

**Status:** Accepted

## Context

The benchmark publishes an ablation table: re-run the corpus with one capability removed, and
report what changes. The point is to *measure* each capability's contribution rather than assert
it.

The first implementation ran the headline configuration over all 1,000 scenarios and each
ablation over the first 200, then printed them in one table. Those numbers are not comparable.
The 1,000-scenario corpus is a stratified draw weighted towards harder faults; its first 200
entries are the guaranteed one-per-fault prefix plus a weighted tail, and they have a different
difficulty distribution. A capability could appear to *help* or *hurt* purely because of which
scenarios each column ran on.

A second, subtler problem: the abstention ablation monkeypatched `assurance.evaluate_report`,
but `certification.py` and `verification.py` had bound that function into their own namespaces
with `from .assurance import evaluate_report`. The patch therefore missed the verification
path — the part that decides whether an incident may close — so the ablation silently
understated itself.

## Decision

1. **Every configuration is scored on the identical scenario subset.** The `full` row in the
   ablation table is the same 200 scenarios, scored from the full run's own rows, not the
   headline 1,000-scenario number. `_run_ablations()` indexes the full run by
   `(fault_id, seed)` and selects the subset, so the baseline is a re-scoring, not a re-run.
2. **An ablation must be applied at every import site** it can reach. The abstention ablation
   patches `assurance`, `certification` and `verification`.
3. **`--ablations-only` re-runs the ablations against an existing full run**, because the
   1,000-scenario corpus is expensive and unchanged when only the ablation harness changes.

The headline 1,000-scenario numbers remain the published result for the system; the ablation
table is explicitly a 200-scenario like-for-like comparison and is labelled as such.

## Alternatives considered

**Run every ablation over the full 1,000 scenarios.** Correct, and roughly four times the
compute for each of three ablations. Rejected as the default; the subset is large enough to show
effects of the size we observe (a 13-point top-1 swing), and the code supports running the full
corpus for anyone who wants it.

**Report ablations as deltas without a baseline column.** Rejected: a delta against an unstated
baseline is the same error with the evidence removed.

**Bootstrap confidence intervals on the subset.** Worth doing and not done. The current table
reports point estimates on 200 scenarios; small differences (one or two points) should not be
read as real, and the benchmark documentation says so.

## Consequences

**Good.** The ablation table answers the question it claims to answer. The abstention ablation
now exercises the verification path, so its result reflects the capability rather than a partial
patch.

**Costly.** One more concept in the benchmark documentation: the `full` row of the ablation table
does not match the headline table, and that difference has to be explained every time it is
presented.

**Consequence we accept.** Point estimates on 200 scenarios are noisy. Differences of a couple of
points are not interpreted, and [BENCHMARK.md](../BENCHMARK.md) states which comparisons are
large enough to be meaningful.
