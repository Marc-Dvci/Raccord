# Architecture

## The one-sentence version

A deterministic control plane owns the facts and the authority; Gemini owns synthesis and
language; Grafana MCP is the only route to operational truth; and nothing reaches production
without a signed, single-use approval bound to an exact action.

## Why it is built this way

The obvious design for "an AI agent for observability" is a language model with tools, a prompt
that says *be careful*, and a human in the loop as a rubber stamp. That design fails on the two
things that matter in live media operations:

1. **It cannot be trusted to act.** A model that can call `switch_encoder_pool` can be argued
   into calling it. Prompt injection reaches it through the very logs it is reading.
2. **It cannot be measured.** If the model decides what "broken" means, there is no ground truth
   to benchmark against, and every regression is invisible.

So the responsibilities are split by *what can be verified*:

| Layer | Owns | Verifiable by |
|---|---|---|
| Probes + SLO engine | whether the experience meets the promise | unit tests against known signals |
| Digital twin + registry | who is affected and what was promised | scope precision/recall against fault specs |
| Evidence agent | the facts, all via Grafana MCP | the state machine's preconditions |
| Diagnosis agent | ranked hypotheses over a formal taxonomy | top-1/top-3 accuracy on 1,000 scenarios |
| Policy engine | whether an action *may* run | rule-by-rule unit tests |
| Approvals + executor | whether an action *does* run | replay/expiry/hash-binding tests |
| Verification | whether it actually worked | false-closure rate |
| **Gemini** | explanation, uncertainty, audience language | human review; cannot affect the above |

## Data flow

```
                       ┌──────────────────────────────────────────┐
                       │        DIGITAL TWIN ENVIRONMENT          │
   fault injection ───▶│  sources → encoder pools → packager →    │
                       │  manifest → origin → CDN regions →       │
                       │  player builds → sessions                │
                       └───────────────┬──────────────────────────┘
                                       │ observe(language, territory,
                                       │         platform, build)
                                       ▼
   ┌───────────────────────────────────────────────────────────────┐
   │ PROBE FLEET   caption · audio-description · sign · player      │
   │ token alignment (DTW) · language ID · semantic similarity ·    │
   │ loudness · frame continuity · a11y-tree journeys              │
   │ every finding: score + confidence + interval + abstention     │
   └───────────────┬───────────────────────────────────────────────┘
                   │ ModelFinding[]
                   ▼
   ┌───────────────────────────────────────────────────────────────┐
   │ LIVE ASSURANCE  evaluate against the promise in force at T,   │
   │ burn error budget, raise structured alerts                    │
   └───────────────┬───────────────────────────────────────────────┘
                   │                     ▲
       Prometheus  │ Loki   Tempo        │ Grafana MCP
       Pyroscope   ▼ annotations         │ (the ONLY read path)
   ┌───────────────────────────┐   ┌─────┴──────────────────────────┐
   │      GRAFANA STACK        │◀──│  GRAFANA EVIDENCE AGENT        │
   │  5 dashboards, 31 rules   │──▶│  14-call mandatory chain       │
   └───────────────────────────┘   └─────┬──────────────────────────┘
                                          │ Evidence[]
                                          ▼
   ┌───────────────────────────────────────────────────────────────┐
   │ INCIDENT COORDINATOR — 12-state machine, preconditions,       │
   │ hash-chained audit                                            │
   │   scope → quality → correlation → diagnosis → policy          │
   └───────────────┬───────────────────────────────────────────────┘
                   │ ProposedAction + PolicyDecision
                   ▼
   ┌───────────────────────────────────────────────────────────────┐
   │ APPROVAL  signed · single-use · expiring · bound to the       │
   │           action hash AND the evidence hash                   │
   └───────────────┬───────────────────────────────────────────────┘
                   ▼
   ┌───────────────────────────────────────────────────────────────┐
   │ EXECUTOR  12 allow-listed actions. No shell. No dynamic       │
   │           targets. Idempotent. Records before/after state.    │
   └───────────────┬───────────────────────────────────────────────┘
                   │ mutates the twin
                   ▼
   ┌───────────────────────────────────────────────────────────────┐
   │ VERIFICATION  re-measure: original scope, adjacent slices,    │
   │ dependent features. Mandatory failure ⇒ rollback, not close.  │
   └───────────────────────────────────────────────────────────────┘
                   │
                   ▼ communications (6 audiences) → review → benchmark corpus
```

`GEMINI (ADK)` sits alongside the coordinator from `EVIDENCE_COMPLETE` onwards: it reads the
typed record and produces narrative, uncertainty and audience language. It has read-only MCP
tools. It has no path to the executor.

## Components

### Promise registry (`registry.py`)

Every accessibility experience is a **versioned operational contract**: feature, language,
territories, platforms, device classes, builds, delivery path, provider, thresholds, approved
fallback, SLO tier, three owners, communication policy, retention.

Amendments create a new version; the old version stays readable. Incident reasoning uses
`as_of(promise_id, incident_time)` — never `current()` — so an investigation is judged against
the promise that was in force when the symptom appeared. Validity intervals are strictly
ordered even within one clock tick, so the point-in-time read is never ambiguous.

### Digital twin (`twin.py`)

A versioned graph: 29 entity kinds, 14 edge kinds, point-in-time node and edge reads. Its job is
the **blast-radius query**: given a failing component, return every affected feature, language,
territory, platform, build, region, provider, violated promise, responsible owner, safe
remediation target and at-risk adjacent component — in one traversal.

Topology answers *what could be affected*; telemetry answers *what is affected*. The scope agent
intersects them, which is why scope precision is 1.00 rather than "the encoder feeds everything,
so everything is affected".

### The environment (`simulator.py`, `media.py`, `faults.py`)

A real, running delivery chain — not canned JSON. It advances a clock and produces timed caption
cues, described-audio windows, interpreter-feed frame statistics, synthetic player journeys,
manifests, transport counters and k-anonymised session aggregates, for any
(language, territory, platform, build) slice.

45 documented faults change what the audience actually receives. Approved actions change it
back — but only the actions that are genuinely corrective for that fault. A plausible-but-wrong
action changes state and leaves the symptom in place, which is what makes verification a real
test rather than a formality.

`faults.py` is the ground truth and is imported by nothing except the simulator and the
benchmark scorer. The probes, the diagnosis agent and the verification agent never see it.

### Probes (`probes/`)

Measure the **rendered audience experience**, not server health:

- **caption** — monotonic token alignment (Needleman-Wunsch with gap costs) recovers the
  correspondence between the caption stream and the spoken stream, then reads drift off the
  matched pairs. Plus omission, semantic preservation (hashed n-gram cosine), wrong-language
  (character-trigram identification fitted on the programme corpus), speaker attribution,
  duplicates, reading speed, flicker, and device render success.
- **audio_description** — declaration, audibility against a silence floor, language, loudness
  window, timeline drift, dialogue-gap placement, channel layout, selection success, and an
  explicitly *advisory* semantic-coverage flag.
- **sign** — technical quality only: continuity, frozen and black frames, frame rate, interpreter
  visibility, PiP overlap, A/V sync. It makes no claim about signing content (see model card).
- **player** — seven accessibility journeys across a browser/CTV/screen-reader/zoom matrix,
  including authentication and purchase.

Every finding carries score, confidence, confidence interval, evidence interval, model version,
data-quality state, known limitations, and can **abstain**. A window with too little dialogue
produces `data_quality="insufficient"`, not a confident zero.

### SLOs and assurance (`slo.py`, `assurance.py`)

31 SLOs with per-tier objectives and error budgets. This module is the single source of truth for
the probes, the generated Grafana alert rules, the preflight gate and post-incident accounting.
`tools/generate_grafana_assets.py --check` runs in CI, so a dashboard threshold cannot drift
from the objective.

### Incident machine (`incident.py`)

```
DETECTED → QUALIFIED → SCOPED → EVIDENCE_COMPLETE → DIAGNOSED → POLICY_EVALUATED
        → AWAITING_APPROVAL → ACTION_EXECUTING → VERIFYING → RECOVERED
        → COMMUNICATED → REVIEWED
                                    ↘ REJECTED    ↘ ROLLED_BACK
```

Each transition has machine-checkable preconditions and emits a hash-chained audit event.
Notable ones:

- `EVIDENCE_COMPLETE` requires MCP evidence for alerts, metrics, logs, traces and dashboards.
- `DIAGNOSED` requires hypotheses that cite evidence ids that actually exist.
- `ACTION_EXECUTING` requires, when policy demanded approval, a token whose action hash **and**
  evidence hash still match and which has not expired.
- `RECOVERED` requires every mandatory assertion to be `passing` — `inconclusive` is not `passing`.
- `COMMUNICATED` requires operator, executive and public-status messages, and refuses if the
  public message is flagged as containing internal detail.

An **abstained diagnosis is a legitimate diagnosis** — it records that the evidence does not
support a conclusion. What it blocks is the *action*: the policy stage refuses to propose one and
escalates to a human with all evidence intact.

### Policy, approvals, executor

20 rules over typed records, versioned (`raccord-policy-2026.08.1`) and stamped onto every
decision so an incident can be re-evaluated under the policy in force. 12 catalogued actions,
each with preconditions, an allow-listed target set, a required role, an expected metric change,
a verification suite and rollback behaviour.

The executor re-checks the policy itself rather than trusting the caller, redeems the token,
enforces idempotency, and records before/after state. It has no shell, no dynamic target
construction, and no way to widen a scope after approval.

### Verification (`verification.py`)

Six suites. Each re-runs the probe fleet in three scopes:

- **original** — the slices that breached must now be inside objective;
- **adjacent** — languages, territories and builds that were healthy must still be healthy;
- **dependent** — features sharing the repaired component must not have regressed.

The suite is chosen by the feature that actually broke, not by the action's default — a clock
change serves captions, described audio and the interpreter feed, and the assertions that must
pass are the ones for the broken feature.

## Google Cloud footprint

| Plane | Services |
|---|---|
| Reasoning | Gemini on Vertex AI; ADK agents and MCP toolset; Agent Engine managed runtime |
| Application | Cloud Run (app, API, deterministic control plane and probe simulation), one instance while live state is SQLite |
| Event | Pub/Sub receives de-identified event-level probe and SLO summaries |
| Evidence | Create-only, versioned Cloud Storage incident bundles; BigQuery aggregate incident outcomes |
| Observability | Grafana Cloud OTLP plus Agent Observability; official Grafana MCP sidecar behind a token-authenticated Cloud Run gateway; Prometheus, Loki, Tempo and Pyroscope locally |
| Security | Separate app/reasoning service accounts, least-privilege IAM, Secret Manager write-only secret provisioning; internal ingress in production mode |

The local demonstration runs the reasoning, application and state planes on a laptop with SQLite
and in-process stores. `tools/deploy_agent_engine.py` deploys the reasoning plane to Agent
Engine; `Dockerfile` and `infra/terraform/` carry the Cloud Run path. The exact deployment and
credential split are documented in [CLOUD_DEPLOYMENT.md](CLOUD_DEPLOYMENT.md).

## Trust boundaries

1. **Model output → operational fact.** Crossed only by typed `ModelFinding`s with confidence and
   abstention. A low-confidence finding cannot alone justify a change.
2. **Agent proposal → permitted action.** Crossed only by the policy engine.
3. **Permitted action → executed action.** Crossed only by a redeemed approval token.
4. **Internal record → public statement.** Crossed only by the communication agent, and the
   state machine refuses to close if the public message is flagged as carrying internal detail.
5. **Untrusted text → agent reasoning.** Log lines and caption content are data, never
   instructions. See [THREAT_MODEL.md](THREAT_MODEL.md).

## What remains outside the demonstrated scope

The local system is complete and runs end to end. `docs/cloud_smoke_run.json` is generated only
after a live deployment passes every cloud integration assertion; its absence means the live path
has not yet been claimed. SQLite live state intentionally limits Cloud Run to one instance; durable shared
workflow state is the next production step. Optional GPU/TPU inference pools, custom alignment
kernels, and eBPF delivery telemetry are documented in [PERFORMANCE.md](PERFORMANCE.md) with what exists,
what it would replace, and what has *not* been measured. Nothing in the benchmark results
depends on them.
