# ADR 0007 — Probes abstain rather than guess

**Status:** Accepted

## Context

A probe measures a 10–30 second window of a live stream. Sometimes there is nothing to measure:
no dialogue in the window, no caption cues, a feature not advertised in the manifest, too few
matched tokens to align against.

The convenient behaviour is to return zero. Zero drift, zero omission — the metric is
well-formed, the dashboard is green, the alert does not fire. It is also the most dangerous
possible output, because "no captions at all" and "captions perfectly on time" produce the same
number.

## Decision

Every `ModelFinding` carries a **confidence** and an **abstention flag**. A probe that cannot
measure returns `abstained=True, data_quality="insufficient"`, its known limitations, and the
exact evidence interval the claim would have been about.

Downstream, an abstained finding:

- never breaches an SLO,
- never supports a hypothesis,
- never satisfies a verification assertion,
- is displayed in the UI in words — *"abstained: evidence insufficient"* — not as a gap.

Thresholds are explicit and few: fewer than 12 matched tokens abstains on drift; text under 12
characters returns `unknown` from the language identifier; a window with no cues abstains on
every quality metric while still reporting availability, which *is* measurable.

## Alternatives considered

**Return zero with low confidence.** Rejected: consumers ignore confidence. Absence has to be
structurally different from a measurement, not a weaker one.

**Interpolate from the previous window.** Rejected: it invents data during exactly the
conditions where the truth is least knowable, and it would mask a total loss of service for as
long as the interpolation held.

**Widen the window until measurement is possible.** Considered, and partly done (the operational
SLOs evaluate over windows rather than instants). Rejected as a general answer because an
unbounded window destroys the time resolution an incident needs.

## Consequences

**Good.** The calibration study measures it directly: over windows containing no caption content
at all, abstention rate is **1.000** and the confident-zero rate is **0.000**. That is the number
this ADR exists to protect.

**Costly.** Every consumer has to handle abstention. `ProbeReport.value()` returns the default
for an abstained finding, SLO evaluation filters them, verification treats an abstained
assertion as not-passing. That is more code than trusting a float.

**Measured trade-off.** The `no_probe_confidence` ablation re-runs the corpus treating every
finding as fully confident, so the cost of removing abstention is reported rather than asserted
— see [BENCHMARK.md](../BENCHMARK.md).
