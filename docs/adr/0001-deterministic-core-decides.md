# ADR 0001 — The deterministic core decides; the model explains

**Status:** Accepted

## Context

AccessPulse changes a live broadcast chain during a premiere. The consequences of a wrong change
are borne by the audience that already has the worst experience. At the same time, the raw
material of an accessibility incident is genuinely multimodal — metrics, logs, traces, probe
findings across four features, change events — and synthesising that into an explanation a human
can act on is exactly what a large language model is good at.

The temptation in an agentic system is to let the model do both: reason *and* decide. That is
the design most agent frameworks encourage, and it is the design that makes "the agent broke
production" a sentence people say.

## Decision

Split by kind of work, not by convenience:

- **The deterministic core owns every fact and every decision.** Detection, scope computation,
  evidence retrieval, hypothesis ranking arithmetic, the policy classification, the verification
  result, and the incident state transitions are computed by typed, tested Python.
- **The model owns synthesis and language.** Explanation of the multimodal picture; explicit
  statement of what is uncertain and what evidence would resolve it; selection *among enumerated
  options*; six audience-specific communications.
- **The model cannot act.** It has no path to the executor, no ability to mint an approval, and
  no tool outside the governed MCP surface.

Every boundary crossing is a typed contract (`contracts.py`). Model output is prose plus a
choice from a closed set — never a target, a query, a command or a fact.

## Alternatives considered

**Let the model drive the loop and constrain it with prompting.** Rejected: prompt constraints
are not enforcement. The failure is silent and the blast radius is production.

**Let the model decide, then have a validator reject bad decisions.** Rejected as the primary
mechanism. A validator strong enough to catch every bad decision is the deterministic core, at
which point the model is decorative; a weaker validator is a false sense of safety. We do use
re-checking at the executor, but as defence in depth, not as the decision-maker.

**No model at all.** Genuinely tempting, and it is why offline mode exists (ADR 0011). Rejected
as the only mode because the explanation, the uncertainty statement and the six communications
are real work that a human otherwise does under time pressure during a live event.

## Consequences

**Good.** The safety properties are testable, and they are tests
(`tests/test_policy.py`, `tests/test_executor.py`, `tests/test_state_machine.py`). The system
degrades to fully functional without the model. Judges — and operators — can be told precisely
what the model can and cannot do, and the answer does not depend on a prompt.

**Costly.** Considerably more code than "give the model tools and a good prompt". Ranking,
scoping and policy logic all had to be written and tested. Adding a capability means writing
deterministic code for it rather than describing it in a prompt.

**Accepted limitation.** The model cannot surprise us usefully. If the true cause is outside the
hypothesis space the ranker knows about, the model cannot rescue the diagnosis — it can only say
the evidence is inconsistent, which it does. The benchmark's top-1 accuracy on the hard subset
(0.277) is partly this limitation, honestly measured.
