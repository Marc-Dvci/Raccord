# ADR 0011 — Offline reasoning is the default

**Status:** Accepted

## Context

The reasoning plane can run on Gemini via Vertex AI and the Agent Development Kit. That requires
a Google Cloud project, credentials, quota and a network. A judge cloning this repository has
none of those, and an operator during a live event may have a network problem that is precisely
correlated with the incident being investigated.

There is also a claim we want to be able to make without qualification: the language model has
no authority in this system (ADR 0001). The most convincing way to demonstrate that is to remove
the model and show the loop still closes.

## Decision

`AP_REASONING_MODE=offline` is the **default**. In offline mode a deterministic reasoning plane
produces the explanation, the uncertainty statement, the hypothesis selection among enumerated
options, and the six communications. The closed loop reaches a verified recovery with the
language model removed entirely.

Setting `AP_REASONING_MODE=gemini` swaps the plane. Nothing else changes: the same typed
contracts, the same state machine, the same policy, the same executor, the same MCP tool
surface. The 1,000-scenario benchmark runs in offline mode, which is why its numbers are exactly
reproducible.

The MCP transport is independent of this choice — `stub` (in-process) by default, `stdio` or
`http` against the official Grafana MCP server, with no agent code changes.

## Alternatives considered

**Require Gemini.** Rejected: the demonstration would not run for a judge without a cloud
account, and the benchmark would be neither reproducible nor cheap.

**Mock the model with canned responses.** Rejected: a mock proves nothing about whether the loop
works without the model — it proves the loop works with a fake model. The offline plane is a
real implementation of the same interface.

**Default to Gemini and fall back on error.** Rejected: silent fallback makes it impossible to
know which plane produced a given incident record. The mode is explicit and is reported in the
UI and in `/api/agent-observability`.

## Consequences

**Good.** `git clone && pip install && accesspulse hero` works, with no credentials, no cloud
account and no network. The benchmark is deterministic and free to run. The claim "the model
cannot decide anything" is demonstrable by deleting it.

**Costly.** Two reasoning implementations to keep behind one interface, and the offline plane's
prose is plainly worse than Gemini's — it is competent and structured, not fluent. The
qualitative difference between the two modes is the honest argument for the model's value, and
it is visible by flipping one environment variable.

**Consequence we accept.** The published benchmark numbers characterise the offline plane. They
should not be read as measuring Gemini's contribution; the ablation table measures capabilities
of the deterministic system, not of the model.
