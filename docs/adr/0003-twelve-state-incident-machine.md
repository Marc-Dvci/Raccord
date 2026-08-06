# ADR 0003 — A twelve-state incident machine with machine-checkable preconditions

**Status:** Accepted

## Context

The usual shape of an agent loop is: think, call a tool, think again, decide you are done. What
"done" means is whatever the model says. For an incident that ends in a change to a live
broadcast chain, "the model said it was finished" is not an acceptable definition of finished.

Incident response in media operations already has a shape — detect, qualify, scope, gather
evidence, diagnose, decide, approve, act, verify, communicate, review. Encoding that shape makes
the loop auditable and makes "the agent skipped verification" impossible rather than unlikely.

## Decision

A twelve-state machine with two terminal off-ramps (`contracts.py`, `incident.py`):

```
DETECTED → QUALIFIED → SCOPED → EVIDENCE_COMPLETE → DIAGNOSED → POLICY_EVALUATED
        → AWAITING_APPROVAL → ACTION_EXECUTING → VERIFYING → RECOVERED
        → COMMUNICATED → REVIEWED
                                        off-ramps: REJECTED · ROLLED_BACK
```

Two properties matter more than the states themselves:

1. **No transition may be skipped.** `INCIDENT_TRANSITIONS` is an explicit adjacency map; an
   illegal transition raises and is recorded as a rejected transition in the audit trail rather
   than silently ignored.
2. **Transitions carry machine-checkable preconditions.** `QUALIFIED` requires a firing alert.
   `EVIDENCE_COMPLETE` requires Grafana MCP evidence of all five kinds (ADR 0002). `RECOVERED`
   requires every mandatory verification assertion to pass. `ROLLED_BACK` requires a real
   verification failure.

The audit trail is a hash chain over the transitions, verifiable with
`machine.verify_audit_chain()`.

## Alternatives considered

**A free-running agent loop with a "done" tool.** Rejected: the completion criterion is the
model's opinion, and the most dangerous failure mode of this system — declaring an accessibility
feature restored when it is not — becomes a prompt-engineering problem.

**A workflow engine (Temporal, Step Functions).** Reasonable in production, rejected here: it
would add infrastructure the judge-runnable demo cannot assume, for the same guarantee. The
state machine is ~400 lines and has no dependencies.

**More granular states.** Considered; rejected as noise. Twelve states map onto what an
operations team already recognises, which matters more than modelling every substep.

## Consequences

**Good.** "The agent cannot close an incident without verification" is a property of the type
system and a test, not a claim. The UI can render the machine and show operators exactly where
an incident is. Post-incident review has an ordered, hash-chained record of what happened when.

**Costly.** Less flexible than a free loop. A genuinely novel incident shape has to be expressed
within these states or the machine has to change — and changing it is a deliberate act with
tests attached, which is the intended friction.

**Consequence we accept.** The benchmark's `false_closure_rate` is meaningful *because* closure
is gated. Without the machine, the metric would measure the model's mood.
