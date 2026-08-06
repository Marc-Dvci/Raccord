# ADR 0004 — Policy as code over an allow-listed action catalog

**Status:** Accepted

## Context

Something has to decide whether a proposed remediation may run automatically, needs a human, or
must not happen at all. In most operational tooling that decision lives in three places at once:
a runbook nobody reads, a Slack norm, and whatever the on-call engineer believes at 02:00 during
a premiere.

For an agentic system the question is sharper, because the proposer is a model. "May this run?"
has to be answerable without consulting the proposer.

## Decision

Two artefacts, both code, both versioned.

**An action catalog** — 12 `ActionSpec` entries, one per `ActionType`. Each declares the
features it can affect, its preconditions, its **allow-listed targets**, the default role
required, the metric change it is expected to produce, the verification suite that will judge
it, its rollback behaviour, and the widest blast radius it may be used on.

**Policy rules** — 12 predicates over typed records (`P-001`…`P-020`), each a plain Python
function an auditor can read:

- `P-001` only catalogued actions may be proposed
- `P-002` the target must be on the catalog allow-list
- `P-003` the action must be able to affect a feature in the incident scope
- `P-004` a change freeze blocks everything except audience communication
- `P-005` no two conflicting actions in flight
- `P-006` an action may not be used on a blast radius wider than it is rated for
- `P-010` live tier-0 needs the event technical director
- `P-011` audience-visible communication needs the viewer-support lead
- `P-012` multi-territory actions need approval even outside tier 0
- `P-013` sign-pipeline restarts involve the accessibility specialist
- `P-014` provider constraints can force approval
- `P-020` narrow, reversible, single-region recoveries may run automatically

`evaluate()` runs every rule and combines: prohibited wins, then approval-required, then
automatic. `POLICY_VERSION` is stamped on every decision, so an incident can be re-evaluated
under the policy that was in force when it happened.

The executor **re-checks the catalog and the target itself** rather than trusting the decision
it was handed.

## Alternatives considered

**Natural-language policy in the prompt.** Rejected: unenforceable, untestable, and it puts the
constraint inside the thing being constrained.

**An external policy engine (OPA/Rego).** Seriously considered and it is the right answer for a
large estate. Rejected here because Rego over these typed records buys no expressiveness we
need, and it adds a runtime the judge-runnable demo cannot assume. The rules are already data
over types; porting them later is mechanical.

**Free-form remediation with a deny-list.** Rejected. A deny-list enumerates what you thought
of. An allow-list enumerates what you have verified, and the 12 entries are exactly the actions
with a verification suite attached (a test enforces that pairing).

## Consequences

**Good.** "What can this system do to production?" has a complete, readable answer: twelve
actions, each with allow-listed targets. Policy changes are reviewed as code and tested. The
classification does not depend on who is asking or how the request is phrased.

**Costly.** Adding a remediation is a small project: catalog entry, verification suite, tests,
and a fault in the library that it genuinely fixes. That friction is deliberate, but it is real.

**Consequence we accept.** A novel incident that would be fixed by an uncatalogued action gets
no automatic remediation. The system says so and escalates to a human, which is the correct
failure.
