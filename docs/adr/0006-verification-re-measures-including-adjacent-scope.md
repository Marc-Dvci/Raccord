# ADR 0006 — Verification re-measures, including adjacent scope

**Status:** Accepted

## Context

The worst outcome available to this system is not failing to fix an accessibility fault. It is
**declaring the fault fixed when it is not** — because that ends the incident, stops the pages,
publishes a status update saying service is restored, and leaves the affected audience with
nothing while everyone else believes the problem is over. An automated system that does this is
worse than no system.

The second-worst outcome is fixing the reported fault and breaking something adjacent. Switching
a caption encoder pool can regress French while fixing English; restarting a sign pipeline can
disturb the described-audio track that shares a component. A verification that only re-checks
the thing that was broken cannot see either.

## Decision

An incident reaches `RECOVERED` only when the probe fleet has re-measured the rendered
experience *after* the action and every mandatory assertion has passed. Each catalogued action
declares its verification suite, and every suite spans three scope kinds:

| Scope | What it asserts |
|---|---|
| `original` | the exact slices that breached are now inside objective |
| `adjacent` | neighbouring languages, territories, platforms and features that were healthy are **still** healthy |
| `dependent` | features sharing the repaired component are unaffected |

Assertions are evaluated against the same SLO definitions that raised the incident, so a panel
threshold can never disagree with a closure criterion. If a mandatory assertion fails and the
approved action declared automatic rollback, the verification agent rolls back and the incident
moves to `ROLLED_BACK`, not `RECOVERED`.

The model plays no part. `RECOVERED` is a precondition check over measured assertions.

## Alternatives considered

**Trust the metric that raised the alert to clear.** Rejected: it is one signal, it can clear
for the wrong reason, and it says nothing about adjacent damage.

**Ask the model whether it worked.** Rejected — this is precisely the failure this ADR exists to
prevent.

**Re-measure only the broken slices.** Rejected: cheaper, and it makes "we fixed English and
broke French" invisible. The hero incident's 9 assertions include 3 adjacent ones for exactly
this reason.

**Wait longer before verifying.** Considered; a settle period is configurable
(`settle_seconds`). Longer waits trade recovery time for confidence, and during a live premiere
recovery time is audience harm.

## Consequences

**Good.** The benchmark can report a `false_closure_rate` at all, and it is 0.001 over 1,000
scenarios. The rollback rate — 0.081 — is a *feature*: in 81 of 1,000 scenarios the system chose
the wrong action, found out by measuring, and undid it rather than closing.

**Costly.** Verification is the most expensive phase: it re-runs the probe fleet across the
original, adjacent and dependent slices. It is a large part of the ~15 s mean wall time per
benchmark scenario.

**Consequence we accept.** A fault that recovers slowly can fail verification and trigger a
rollback that was not needed. We prefer that to closure without evidence, and the rollback is
recorded so a human can see it happened.
