# ADR 0005 — Approval tokens bound to an action hash and an evidence hash

**Status:** Accepted

## Context

Policy says an action needs a human. The naive implementation is a boolean: the UI sets
`approved = True` and the executor checks it. That is worth nothing. It can be set by any code
path, including the agent's; it survives changes to the action after approval; it can be reused;
and it leaves no evidence of who took responsibility.

The property we actually want is: **a named human, holding an authorised role, looked at
*this exact action* supported by *this exact evidence*, and said yes, once, recently.**

## Decision

An approval is a capability, not a flag. `ApprovalService.issue()` mints an HMAC-SHA256-signed
token over a payload binding:

| Field | Binds |
|---|---|
| `jti` | token identity, for single use |
| `inc` | the incident |
| `act` | the action id |
| `ah` | `action.action_hash()` — type, target, parameters |
| `eh` | `incident.evidence_hash()` — the evidence set the approver saw |
| `sub` / `role` | the named approver and their role |
| `iat` / `exp` | issue time and a 300-second default TTL |

`redeem()` verifies the signature, that the service itself issued the token, that it has not
been redeemed, that it has not expired, and that all four bindings still match. Any change to
the action after approval changes `ah`; any change to the evidence changes `eh`; either fails
redemption with an explicit message. Role authorisation is checked at **issue** time — an
approver without the required role is refused and the refusal is audited.

The signing key lives in `Settings.signing_key()` (Secret Manager in deployment, a gitignored
local file in the demo). **No agent has a code path to `issue()`.**

## Alternatives considered

**A boolean or a database row.** Rejected, above.

**JWT with a standard library.** Effectively what this is, minus a dependency and minus the
temptation to accept tokens issued by anything else. The service keeps its own record of every
token it issued, so a well-formed token from elsewhere is still refused
(`test_token_from_another_service_is_refused`).

**Asymmetric signing.** Better for a multi-service deployment where the verifier should not hold
the signing key. Not adopted here because issuer and verifier are the same process; the change
is localised to `ApprovalService` if that stops being true.

**Longer TTL for operator convenience.** Rejected. Five minutes is roughly how long an approval
should be meaningful during a live event; a stale approval is an approval for a situation that
no longer exists.

## Consequences

**Good.** Time-of-check/time-of-use is closed: you cannot get a narrow action approved and then
widen it. Replay is closed. "Who approved this, when, and what did they see?" has an exact
answer, in the audit trail, for every action. Eight tests cover the failure modes.

**Costly.** The approval flow is more work to implement and to explain than a checkbox, and an
expired token during a demonstration is a real annoyance (the reset endpoint exists partly for
this).

**Consequence we accept.** An insider holding the approver role can still approve a bad action.
Policy makes it accountable, not impossible — the correct trade for an operational system.
