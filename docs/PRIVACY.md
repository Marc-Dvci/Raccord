# Privacy model

Raccord measures whether an accessibility feature *worked*. It does not measure, infer,
store or transmit anything about who needed it.

That distinction is the whole design. A system that watched individual viewers to find out
who turns captions on would be building a disability register, and no reliability benefit
justifies that. So Raccord gets its answers from synthetic probes against the delivery
chain, and uses real-user data only in the form of coarse, suppressed, aggregate counts.

---

## 1. The non-negotiable rule

> **Raccord never infers, records or acts on a person's disability, impairment,
> assistive-technology use, or identity.**

Concretely, the system does not:

- attach an identifier — account, device, session, IP, cookie or hash of any of these — to any
  accessibility signal,
- record that a *particular* viewer enabled captions, described audio, sign-language video or a
  screen reader,
- profile, segment, target or score viewers,
- retain raw player telemetry,
- send audience data to a language model.

Feature-enablement is only ever a **count within a slice**, never an attribute of a person.

---

## 2. Where the evidence actually comes from

| Source | Nature | Contains personal data? |
|---|---|---|
| Caption, audio-description, sign-feed and player probes | Synthetic sessions the platform runs against itself | No. There is no human on the other end of a probe. |
| Delivery-chain telemetry (encoder, packager, CDN, timing) | Machine state | No |
| Change events (deployments, config, flags) | Operational metadata | Operator identity only, in an operational context |
| Session aggregates | Counts per slice, k-anonymised | No — see §3 |

The overwhelming majority of Raccord's evidence — everything that drives detection, scope,
diagnosis, the policy decision and verification — comes from the first two rows. **The closed
loop reaches a verified recovery without reading a single real-user record.** Aggregates only
size the impact after the fact.

You can check that claim: `SessionAggregate` is consumed in exactly one place in the incident
path (impact estimation), and no probe, SLO evaluation, hypothesis or verification assertion
takes it as an input.

---

## 3. What a session aggregate is

`SessionAggregate` (`src/raccord/contracts.py`) is the only structure in the system derived
from real viewers, and it is deliberately impoverished:

```python
class SessionAggregate(Frozen):
    """Privacy-preserving. Never an individual, never an inferred trait."""
    slice_key: str                          # "FR/ctv/ctv-9.4.0"
    feature: FeatureType                    # captions | audio_description | sign_language
    language: str
    territory: str                          # country, never finer
    platform: Platform
    player_version: str
    sessions_with_feature_enabled: int      # a count, never a list
    selection_failures: int
    playback_errors_after_selection: int
    repeated_attempts: int
    support_contacts: int
    k_anonymity_threshold: int = 50
    suppressed: bool = False
```

Properties that follow from the shape of the type, not from policy documentation:

- **No identifier field exists.** There is nowhere to put one.
- **Territory is the finest geography.** No city, no region, no postcode, no coordinates, no
  network operator.
- **Every measure is a count.** There is no per-session record to join, leak or subpoena.
- **Small slices are suppressed, never sharpened.** Below `k_anonymity_threshold = 50` the
  counts are zeroed and `suppressed = True` is set (`Simulator.session_aggregates`). The
  interface tells the operator that a slice is suppressed; it never quietly reports a
  small-cell value.

### Why k = 50 and not k = 5

Accessibility-enabled sessions are a minority population in every slice. A k that is safe for
general telemetry is not safe here, because membership in the aggregate is itself the sensitive
fact. k = 50 is set per-query (`session_aggregates(..., k_threshold=50)`) and applied before any
value leaves the simulator, so a suppressed slice is suppressed for every downstream consumer,
including Grafana and the UI.

### Suppression is not the only protection

Because Raccord never needs a *trend at high resolution* for a minority slice, aggregates
are only ever read at incident timescales for impact sizing. There is no longitudinal per-slice
store to difference against, which is the usual way k-anonymity is defeated.

---

## 4. What reaches Grafana

Every accessibility metric Raccord writes to Prometheus is labelled with the operational
slice — feature, language, territory, platform, player version, event — and nothing else. There
is no user, session, account or device label anywhere in the metric surface, so no Grafana
query, dashboard variable, alert rule or MCP call can produce one. Loki lines are delivery-
component logs, not access logs. Tempo spans cover the media path and the agent's own
reasoning, not viewer requests.

The MCP call chain includes `query_prometheus` for accessibility-enabled session aggregates
(step 4). That query returns k-anonymised counts per slice — the same data described in §3, with
the same suppression already applied.

---

## 5. What reaches the model

When the reasoning plane runs on Gemini, the model receives the **typed incident record**: SLO
evaluations, probe findings, evidence items retrieved through Grafana MCP, change events, the
ranked hypotheses and the policy decision. Those structures are the ones documented above and
contain no personal data by construction.

The model is not given raw telemetry, not given aggregate rows below threshold, not given
free-text operator notes, and cannot issue its own queries outside the governed MCP tool
surface. It also cannot act — see [THREAT_MODEL.md](THREAT_MODEL.md).

---

## 6. Personal data that does exist

Two categories, both operational rather than audience data:

**Operator identity.** Approvals record who approved what: an email address, a role, a
timestamp, and the hash of the action they approved. This is accountability data — the point of
an approval is that a named human took responsibility — and it is retained with the incident
record. Approvers are staff, not viewers.

**Support contacts.** The aggregate carries a *count* of support contacts per slice. The
contacts themselves live in whatever support system an operator already runs; Raccord never
ingests their content.

---

## 7. Retention

| Data | Retention | Why |
|---|---|---|
| Probe findings and SLO evaluations | Metric retention of the Prometheus/Mimir deployment | Trend and error budget |
| Session aggregates | Same, already suppressed | Impact sizing |
| Incident records, evidence, approvals, audit chain | Retained for the audit period the operator sets | Accountability and post-incident review |
| Raw player telemetry | **Never stored** — aggregated at the edge of the system | Nothing to leak |
| Model prompts and responses | Traced as spans for observability; contain only the typed record | Debuggability |

In the local demonstration everything lives in SQLite under `var/` and is destroyed by
`raccord` judge reset (`POST /api/reset`).

---

## 8. Regulatory posture

This is a demonstration system, not legal advice, but the design maps cleanly onto the
obligations that would apply to a real deployment:

- **GDPR Art. 9 (special categories).** Data revealing health — which includes disability — is
  prohibited absent a specific legal basis. Raccord's answer is not to find a basis but to
  never process it: no individual-level accessibility signal is collected, so Art. 9 is not
  engaged by the accessibility measurement path.
- **Data minimisation (Art. 5(1)(c)).** The evidence that drives the loop is machine telemetry
  from synthetic probes. The audience data is counts with suppression.
- **EU Accessibility Act / EN 301 549 / WCAG 2.2.** These are the *reasons* the measurement
  exists; the promise registry encodes the obligations an operator has committed to, and the
  certification gate is where they are proved before an event.
- **Purpose limitation.** Aggregates exist to size the impact of a specific incident. They are
  not available for marketing, product analytics or personalisation, because the counts carry no
  key to join on.

---

## 9. What we would refuse to build

For the avoidance of doubt, these are out of scope by design and would not be added on request:

- per-viewer accessibility profiles, however "anonymised",
- inference of impairment from behaviour (caption toggling, seek patterns, screen-reader
  signatures, dwell time),
- cross-session or cross-device linkage of accessibility feature use,
- targeting, pricing, ranking or content decisions informed by accessibility signals,
- exporting slice-level enablement to any third party.

If an operator needs to know *whether the described-audio track worked in Germany on CTV
9.4.0*, Raccord answers that precisely, from probes, without knowing anything about a single
German viewer. That is the whole point.
