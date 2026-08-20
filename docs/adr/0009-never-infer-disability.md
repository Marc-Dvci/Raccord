# ADR 0009 — Never infer disability; measure delivery, aggregate with suppression

**Status:** Accepted

## Context

The most useful-sounding signal available to an accessibility monitoring system is also the one
it must never collect. Knowing which viewers turn on captions, described audio, sign-language
video or a screen reader would let you measure impact precisely, alert on the affected cohort,
and personalise recovery.

It would also be a disability register. Accessibility-feature use is data revealing health under
GDPR Article 9; membership in the cohort is itself the sensitive fact, not merely an attribute
of it. No reliability benefit justifies building it, and a project whose entire premise is
respect for these audiences building it would be self-refuting.

## Decision

**Raccord never infers, records or acts on a person's disability, impairment,
assistive-technology use, or identity.**

Operationally:

1. **Evidence comes from synthetic probes.** Detection, scope, diagnosis, the policy decision
   and verification are all driven by probes the platform runs against itself and by machine
   telemetry. The closed loop reaches a verified recovery **without reading a single real-user
   record**.
2. **Real-user data exists only as `SessionAggregate`** — counts per slice, with no identifier
   field in the type, territory as the finest geography, and no per-session record anywhere.
3. **k-anonymity at k = 50**, applied at the source. Below threshold the counts are zeroed and
   `suppressed=True` is set, so a suppressed slice is suppressed for every consumer including
   Grafana, the UI and the MCP surface. Small slices are **suppressed, never sharpened**.
4. **No user, session, account or device label** exists in the Prometheus metric surface, so no
   query, dashboard variable, alert rule or MCP call can produce one.
5. **The model never sees audience data.**

## Alternatives considered

**Collect enablement per session, anonymise later.** Rejected: "anonymised" per-session data
about a minority cohort is re-identifiable, and the collection is the harm.

**k = 5, as for general telemetry.** Rejected: accessibility-enabled sessions are a minority in
every slice, and here membership is the sensitive fact. k = 50 costs us resolution on small
territories and is worth it.

**Differential privacy with noise instead of suppression.** Considered seriously. Rejected for
this use because the aggregates are read at incident timescales for impact sizing, not as a
longitudinal series, and noise on a small count is easier to misread operationally than an
explicit "suppressed". Suppression is legible: the interface says the slice is suppressed rather
than quietly reporting a wrong number.

**Infer the affected cohort from probe results and session counts.** This is what we do —
*at slice level*. The distinction is that a slice is a delivery configuration, not a person.

## Consequences

**Good.** Article 9 is not engaged by the accessibility measurement path, because the data is
not processed. There is no per-session store to leak, subpoena or misuse. Impact figures remain
useful: "8,053 sessions affected, 140,295 protected" is computed from suppressed aggregates.

**Costly.** Impact numbers are coarser than they could be. We cannot tell an operator *which*
viewers are still broken after a partial recovery — only which slices. Suppressed slices report
zero, which occasionally hides a genuinely small affected population; the interface marks it
rather than pretending the data is complete.

**Consequence we accept and would not trade.** Several attractive features — targeted recovery
messaging, per-cohort SLOs, personalised fallbacks — are permanently out of scope. See
[PRIVACY.md](../PRIVACY.md) §9 for the full list of things we would refuse to build.
