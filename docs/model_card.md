# Model card — Raccord measurement models

Raccord contains several models. This card covers all of them, because the honest
description of the system is that **the models measure and the deterministic core decides**,
and a card that only described the language model would misrepresent where the intelligence
lives.

| Model | Version | Role | Decides anything? |
|---|---|---|---|
| Caption quality probe | `caption-probe-1.4.0` | Measures the rendered caption experience | No — emits findings with confidence |
| Audio-description probe | `ad-probe-1.2.0` | Measures the described-audio experience | No |
| Sign-feed technical probe | `sign-probe-1.1.0` | Measures interpreter-feed technical quality | No |
| Player journey probe | `player-probe-1.3.0` | Measures accessible operation of the player | No |
| Monotonic token aligner | in `probes/align.py` | Recovers caption↔dialogue correspondence | No |
| Character-trigram language identifier | in `probes/text.py` | Flags wrong-language delivery | No |
| Hashed n-gram embedding | in `probes/text.py` | Semantic-preservation signal | No |
| Diagnosis ranker | in `agents/core.py` | Ranks hypotheses with posteriors | Ranks; the policy engine decides |
| Gemini reasoning plane | `gemini-3.7-flash` via Google Cloud | Synthesis, uncertainty, communications, operator Q&A, learning | **No** — see [THREAT_MODEL.md](THREAT_MODEL.md) §5 |

---

## 1. Intended use

**In scope.** Continuous measurement of whether a promised accessibility feature is being
delivered correctly to a defined audience slice (language × territory × platform × player
version), for the purpose of operating a live media service: detection, scoping, diagnosis,
verification of a remediation, and evidence for a post-incident review.

**Out of scope, and explicitly not supported.**

- **Judging human interpreters, describers or captioners.** The sign-language probe measures
  *technical delivery* — frame freeze, black frames, frame rate, crop, visibility, sync — and
  makes **no claim whatsoever about signing quality, grammar, register or comprehension**. A
  test asserts this (`test_sign_probe_makes_no_semantic_claim`). Signing quality is a judgement
  for Deaf viewers and qualified assessors, not for a monitoring system.
- **Certifying legal compliance.** Raccord measures what it measures. It does not certify
  EN 301 549 or WCAG conformance of a programme.
- **Anything about individual viewers.** See [PRIVACY.md](PRIVACY.md).
- **Replacing human review of described audio.** Semantic coverage is a *signal* that the
  description may have stopped covering the visual action; it is not an assessment of whether
  the description is good.

---

## 2. What each probe outputs

Every finding is a `ModelFinding` carrying: metric id, score, unit, **confidence**, optional
confidence interval, the **evidence interval** (the exact seconds of programme the claim is
about), an **abstention flag**, a data-quality label and its known limitations. Thirty-three
metric ids across the four probes.

The abstention flag is the most important field. A probe that cannot measure — no cues in the
window, no dialogue, a feature not advertised in the manifest — returns
`abstained=True, data_quality="insufficient"` rather than a confident zero. Downstream, an
abstained finding never breaches an SLO and never supports a hypothesis.

---

## 3. Evaluation

Two independent evaluations, both reproducible from this repository.

### 3.1 Calibration of the measurement models

`python -m bench.calibration` — injects a *known* parameter into the digital twin and compares
the probe's estimate against it. The probes cannot read the fault library, so this is a blind
measurement. Full output: [`bench/results/calibration.json`](../bench/results/calibration.json).

**Caption drift estimator** — 160 samples: 8 injected offsets (0.5–12 s) × 5 seeds × 4 slices
(en/FR/CTV, fr/FR/CTV, de/DE/web, es/GB/CTV), 30-second windows.

| | |
|---|---|
| Mean absolute error | **0.030 s** |
| Median absolute error | 0.006 s |
| Bias | +0.030 s (very slightly over-reports lateness) |
| p95 absolute error | 0.148 s |
| Reported 95% interval covers the truth | 1.000 |
| Mean reported confidence | 0.94 |
| Abstentions | 0 of 160 |

Error is flat at 0.018 s from 0.5 s to 6 s and rises to 0.068 s at 8 s and 12 s — at large
offsets the 30-second window contains proportionally less overlapping content to align, which
is the expected behaviour and the reason the operational SLO is evaluated over a window rather
than an instant.

**Caption omission estimator** — injected token-drop probability vs reported `cap.omission_rate`:

| Injected drop rate | MAE | Bias |
|---|---|---|
| 0.05 | 0.017 | −0.003 |
| 0.10 | 0.028 | −0.000 |
| 0.20 | 0.041 | +0.002 |
| 0.30 | 0.037 | +0.003 |
| 0.45 | 0.055 | +0.009 |

Essentially unbiased across the range; absolute error grows with the drop rate because heavier
dropping removes the anchors the alignment uses.

**Language identification** — 1.000 accuracy over the 48 single dialogue lines, mean confidence
1.0. **This is an in-sample result and must be read as one**: the trigram profiles are fitted on
the same programme corpus. It demonstrates that the identifier separates *these four languages
on this corpus*, which is exactly what it is deployed to do (catch a French track served on the
English slice) — and nothing more. It is not a general-purpose language identifier and would not
be presented as one.

**Abstention** — 20 windows containing no caption content at all: abstention rate 1.000,
**confident-zero rate 0.000**. This is the number that matters. A probe reporting "0.0 seconds
of drift, high confidence" for a window with no captions would hide a total loss of service.

### 3.2 End-to-end contribution

The 1,000-scenario system benchmark ([BENCHMARK.md](BENCHMARK.md)) measures what the findings
are worth once the rest of the system consumes them. The ablation that removes probe abstention
— treating every finding as fully confident — is reported there.

---

## 4. Limitations, stated plainly

1. **Calibration is against a digital twin, not against real acoustics.** The twin applies the
   offset exactly, so the study measures the *estimator's* recovery of a known offset, not
   forced alignment against real speech with accents, overlap and background music. Against
   real audio the dominant error term would be the ASR or forced-alignment front end, which is
   not part of this repository.
2. **The aligner is token-identity based.** Heavy paraphrase — legitimate for edited captions —
   reduces matched pairs and therefore confidence. The probe reports this in
   `known_limitations` on every drift finding rather than silently degrading.
3. **The hashed n-gram embedding is a lexical-overlap signal, not a semantic model.** It catches
   a description that has stopped describing; it does not understand meaning. The production
   profile swaps it for a multilingual encoder behind the same `embed`/`similarity` interface.
4. **The language identifier needs ≥ 12 characters** and returns `unknown` below that.
5. **Four languages.** en, fr, de, es — the programme's languages. Adding a language means
   fitting the profile on authorised text for it.
6. **Semantic coverage of audio description is a weak signal**, deliberately. It flags absence,
   not quality.
7. **The sign probe is technical only** (§1). This is a design decision, not a gap to be filled
   later.
8. **In-sample language identification** (§3.1).

---

## 5. The reasoning plane (Gemini)

**Model:** `gemini-3.7-flash` on Google Cloud's global endpoint, reached through the Agent Development Kit. The model uses its supported default `MEDIUM` thinking level; Raccord sends none of the sampling or candidate parameters deprecated or rejected by Gemini 3.7.
`gemini-3.6-flash` remains a configurable GA fallback. Optional —
`RACCORD_REASONING_MODE=offline` is the default and runs the whole loop deterministically.

**Given:** the typed incident record — SLO evaluations, probe findings with their confidence and
abstentions, evidence retrieved through Grafana MCP, change events, ranked hypotheses, the
policy decision.

**Asked for:** an explanation of the multimodal picture; an explicit statement of what is
uncertain and what evidence would resolve it; a choice among *enumerated* hypotheses and
*catalogued* actions; six audience-specific communications.

**Not given:** raw audience data, aggregates below the k-threshold, credentials, or any tool
outside the governed MCP toolset.

**Cannot:** decide that something is broken, compute scope or ranking, mint or bypass an
approval, execute anything, or close an incident.

**Failure modes we expect and how they are contained:** hallucinated causes (the ranking is
computed, not generated); over-confidence (uncertainty is a required field and the deterministic
posterior is what the UI shows); prompt injection through log or annotation text
([THREAT_MODEL.md](THREAT_MODEL.md) §4.2); unavailability (the offline plane runs the same loop).

---

## 6. Ethical considerations

**Who could be harmed by this system being wrong?** The audience that depends on the feature. A
false negative leaves them without service; a false closure tells everyone the problem is fixed
when it is not. That asymmetry is why the benchmark reports false-closure rate and rollback rate
as headline metrics, and why verification re-measures rather than trusting a model.

**Who could be harmed by this system being right in the wrong way?** Viewers, if enablement data
were used to identify them — addressed by not collecting it ([PRIVACY.md](PRIVACY.md)) — and
interpreters, describers and captioners, if technical metrics were used as performance
management. Raccord measures *delivery*, and the sign probe's refusal to make semantic
claims is the concrete expression of that boundary.

**Automation bias.** The interface separates evidence, hypothesis, policy and verified result
structurally, states abstentions in words, and requires a named human approval for anything
consequential during a live tier-0 event.

---

## 7. Maintenance

Probe versions are semantic and stamped onto every finding, so a metric's meaning can be traced
to the code that produced it. Changing a scoring rule requires a version bump; the calibration
study and the system benchmark are re-run and their results committed alongside.

---

*Card format follows Mitchell et al., "Model Cards for Model Reporting" (2019), adapted for a
system whose models measure rather than decide.*
