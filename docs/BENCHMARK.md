# Benchmark — methodology, results, ablations, limitations

1,000 seeded scenarios over 45 documented faults. The agents never see the fault specification;
the harness scores against it.

```bash
accesspulse bench --scenarios 1000                      # the published run
python -m bench.harness --scenarios 200 --workers 7     # a faster subset
python -m bench.harness --ablations-only --workers 7    # re-run ablations only
python -m bench.calibration                             # the measurement models
```

Artefacts: [`bench/results/summary.json`](../bench/results/summary.json) ·
[`bench/results/scenarios.jsonl`](../bench/results/scenarios.jsonl) (one row per scenario) ·
[`bench/results/calibration.json`](../bench/results/calibration.json) ·
[`bench/results/kernels.json`](../bench/results/kernels.json)

---

## 1. What is being measured

A scenario is one complete incident: inject a documented fault into the digital twin, let the
event run, then run the whole closed loop — detect → scope → gather evidence through Grafana
MCP → diagnose → evaluate policy → obtain approval → execute one allow-listed action →
re-measure → communicate → review.

**The separation that makes it meaningful.** `faults.py` holds the ground truth and is imported
by exactly two things: the simulator, which applies the symptom, and the harness, which scores
the result. The probes, the scope agent, the ranking, the policy engine and the verification
suites never import it. Nothing under test can see the answer.

**The corpus.** `build_corpus(1000, seed=20260803)`: every one of the 45 faults appears at least
once, then the remainder is sampled with weight `0.5 + difficulty`, so harder faults appear more
often. Every scenario carries its own seed and can be replayed exactly.

**The slice matrix.** 4 languages (en, fr, de, es) × 5 territories (FR, DE, GB, US, CA) × 3
player builds (ctv-9.3.1, ctv-9.4.0, web-4.12.0). **US is a control territory that no fault in
the library touches** — without it, scope precision would be unmeasurable.

**Reasoning plane:** offline (deterministic), which is the default. These numbers characterise
the system, not Gemini — see [ADR 0011](adr/0011-offline-reasoning-is-the-default.md).

---

## 2. Headline results

1,000 scenarios · 45 fault types · seed 20260803

| Metric | Result | What it means |
|---|---|---|
| Detection rate | **1.000** | 0 missed, 0 harness errors |
| Scope precision / recall | **1.000 / 0.993** | against a fault spec the agents never see |
| Top-1 root cause | 0.652 | the correct failure class ranked first |
| Top-3 root cause | 0.897 | the correct failure class in the top three |
| Mean top posterior | 0.731 | confidence in the top hypothesis |
| Corrective action chosen | 0.895 | of 1,000 actions proposed, 895 genuinely fix the injected fault |
| Recovered **and verified** | 0.919 | every mandatory assertion re-measured and passing |
| Rolled back after failed verification | 0.081 | the system undoing its own wrong action |
| **False closure rate** | **0.001** | 1 in 1,000 |
| **Unsafe action rate** | **0.000** | no action outside policy, ever |
| Mean Grafana MCP calls per incident | 16.9 (max 18) | the cost of ADR 0002 |
| Mean assertions per verification | 6.5 of 6.7 passing | |
| Sessions protected (total) | 32,563,290 | modelled populations — see §7 |

**The two numbers that matter most are the last two zeroes**, and they are not the same kind of
claim as top-1 accuracy.

*False closure* — declaring an accessibility feature restored when it is not — is the failure
mode that makes an automated system worse than no system. It happens once in 1,000 scenarios
because closure is gated on re-measurement, not on a model's opinion
([ADR 0006](adr/0006-verification-re-measures-including-adjacent-scope.md)).

*Unsafe action rate* is 0.000 by construction, not by luck: the executor re-checks the catalog
and the target, and requires a signed approval bound to the action and evidence hashes. A
non-zero value here would mean a bug in the policy engine or the executor, and it is reported
precisely so that a regression would show.

**Rollback rate is a feature, not a defect.** In 81 of 1,000 scenarios the system picked the
wrong action, found out by re-measuring, and undid it rather than closing the incident.

---

## 3. Where it is weak

Accuracy is strongly stratified by difficulty. This table is the honest core of the benchmark.

| Difficulty band | n | Top-1 | Top-3 | Recovered |
|---|---:|---:|---:|---:|
| Easy (< 0.4) | 197 | 0.817 | **1.000** | 1.000 |
| Moderate (0.4–0.6) | 374 | 0.709 | 0.872 | 0.944 |
| Hard (0.6–0.75) | 282 | 0.723 | 0.918 | 1.000 |
| **Hardest (≥ 0.75)** | **147** | **0.150** | 0.782 | **0.592** |

On the hardest band, top-1 collapses to **0.150** and recovery to 0.592. On the harness's own
"hard subset" definition (difficulty ≥ 0.7, n = 242) top-1 is **0.277**.

**Why.** The hardest faults are infrastructure-level causes whose *symptoms are nearly
identical* to the media-level faults they induce: a stale configuration, packet loss, provider
degradation, an encoder CPU saturation and a clock-source change all present as caption drift
with omissions. The eight most-misdiagnosed faults:

| Fault | Misdiagnoses |
|---|---:|
| `infra.stale_config` | 40 |
| `infra.packet_loss` | 32 |
| `infra.provider_degradation` | 29 |
| `cap.word_drop` | 28 |
| `infra.encoder_cpu` | 27 |
| `player.caption_control` | 26 |
| `infra.clock_source_change` | 24 |
| `player.missing_name` | 23 |

Six of the eight are infrastructure or player faults being read as the media symptom they
produce. Top-3 stays at 0.782 on the hardest band, so the right answer is usually *present* and
mis-ranked rather than absent — which is why the product shows ranked hypotheses with their
supporting and contradicting evidence rather than a single verdict.

**What we would do next:** the evidence needed to separate these classes exists (eBPF-level
delivery telemetry, provider-side signals), and the ranking treats them as weaker features than
they deserve. That is a modelling gap, stated rather than hidden.

### By feature

| Feature | n | Top-1 | Recovered |
|---|---:|---:|---:|
| Sign language | 181 | 0.873 | 1.000 |
| Audio description | 146 | 0.856 | 0.856 |
| Accessible player | 173 | 0.717 | 1.000 |
| Accessible purchase | 20 | 1.000 | 1.000 |
| Accessible authentication | 19 | 1.000 | 1.000 |
| **Captions** | **448** | **0.460** | 0.866 |
| **Alternate audio** | **13** | **0.000** | 1.000 |

Captions carry 448 of 1,000 scenarios and 19 of the 45 faults, including every hard
infrastructure fault — so the headline top-1 of 0.652 is substantially a caption number.

**Alternate audio is 0.000 top-1 over 13 scenarios**, and it is worth being precise about why:
described audio and alternate-language audio ride the same adaptation set, the same manifest
entry and the same player audio menu, so the ranker consistently attributes an alternate-audio
fault to its audio-description sibling. It still *recovers* 13 of 13, because the remediation
for both is the same action. A diagnosis that is wrong in a way that does not change the
treatment is still wrong, and it is reported as wrong.

---

## 4. Ablations

Each configuration removes exactly one capability and re-runs the **same 200 scenarios**. The
`full` row is those same 200 scenarios scored from the full run — not the headline 1,000-scenario
number, which would make every comparison meaningless
([ADR 0012](adr/0012-ablations-share-a-subset.md)).

| Configuration | Top-1 | Top-3 | Recovered | Scope P / R | False closure |
|---|---:|---:|---:|---|---:|
| **full** (baseline, n=200) | **0.645** | 0.895 | **0.945** | 1.000 / 0.994 | 0.005 |
| no change correlation | 0.515 | 0.840 | 0.790 | 1.000 / 0.994 | 0.005 |
| no probe confidence | 0.650 | 0.895 | 0.945 | 1.000 / 0.994 | 0.005 |
| no scope agent | 0.640 | 0.895 | 0.940 | **0.494** / 1.000 | 0.000 |

**Change correlation is the load-bearing capability.** Removing it costs **13.0 points of top-1
accuracy** (0.645 → 0.515) and **15.5 points of verified recovery** (0.945 → 0.790). Symptoms
alone frequently cannot separate causes that present identically; the deployment or
configuration event 30 seconds before onset frequently can.

**The scope agent buys precision, not accuracy.** Removing it — scope collapses to "everything",
which is what a system without a digital twin and a promise registry can honestly say — leaves
diagnosis and recovery essentially unchanged, and halves scope precision to **0.494**. That is
the real cost: an operator told that a global premiere is systemically broken when four
territories and two builds are affected will make a different, worse decision. The metric that
moves is the metric that should move.

**Probe abstention shows no measurable effect at system level — a null result, reported as
one.** Treating every finding as fully confident changes top-1 by +0.5 points, which is noise on
200 scenarios, and changes nothing else. Two honest readings: the corpus rarely produces windows
where abstention is decisive, and the SLO evaluation is robust to over-confident findings
because it aggregates across the slice matrix. Where abstention *does* demonstrably matter is at
the measurement layer, and the calibration study measures it directly: over windows with no
caption content at all, the confident-zero rate is **0.000** (§6). We are not going to promote a
null system-level result into a claim it does not support.

**Statistical caution.** These are point estimates on 200 scenarios. Differences of a point or
two — `no_probe_confidence`'s +0.5, `no_scope_agent`'s −0.5 — are not interpretable. The
change-correlation result (13 and 15.5 points) and the scope-precision collapse (50 points) are
far outside that range. No bootstrap intervals are computed; that is a gap.

---

## 5. Performance

| | |
|---|---|
| Mean wall time per scenario | 15.7 s (1 worker) |
| Median / p95 | 14.8 s / 21.7 s |
| Total CPU time, 1,000 scenarios | 15,693 s |
| Wall time with `--workers 7` | 2,654 s (~44 min) |
| Mean Grafana MCP calls | 16.9 (min 16, max 18) |

Most of that time is the probe fleet: the sweep across the slice matrix, then verification
re-running it over original, adjacent and dependent scope. Alignment kernel performance is in
[PERFORMANCE.md](PERFORMANCE.md).

**Mean time to detect is 150.0 s for every scenario, and that is an artefact, not a
measurement.** Detection happens at the first assurance sweep after the fault's symptom
develops, and the harness advances the twin on a fixed tick schedule (6 ticks × 25 s), so every
scenario detects at the same simulated instant. It is reported because the harness computes it;
it should not be read as a latency result. Real detection latency is a function of probe cadence
and would have to be measured against a real chain.

---

## 6. Calibration of the measurement models

The system benchmark scores decisions. The calibration study scores the *numbers those decisions
rest on* — see [model_card.md](model_card.md) §3.1 for the full report.

| | |
|---|---|
| Caption drift estimator, mean absolute error | **0.030 s** (p95 0.148 s, bias +0.030 s) |
| Reported 95% interval covers the truth | 1.000 |
| Caption omission estimator, bias | ≤ 0.009 across injected drop rates 0.05–0.45 |
| Language identification | 1.000 — **in-sample**, on the programme corpus the profiles are fitted from |
| Abstention on windows with no caption content | 1.000 |
| **Confident-zero rate on those windows** | **0.000** |

---

## 7. Limitations

1. **A twin is not the world.** Symptoms are generated from parameterised models, not captured
   from production. This benchmark establishes that the system reasons correctly about *these*
   symptoms.
2. **The fault library is ours.** Its distribution reflects our judgement of what matters, not a
   measured frequency distribution from industry incident data. A fault class we did not think
   of is not in the benchmark.
3. **Difficulty weights are authored, not derived.** They stratify the corpus and define the
   "hard subset", so §3's headline weakness is measured against our own estimate of difficulty.
4. **Time-to-detect is a tick artefact** (§5).
5. **Impact figures are arithmetic over modelled populations.** "32,563,290 sessions protected"
   is internally consistent and useful for comparing scenarios. It is not a claim about any real
   audience, and it should not be quoted as one.
6. **Ablations are 200 scenarios with no confidence intervals** (§4).
7. **Language identification is evaluated in-sample** (§6).
8. **One reasoning plane.** The published numbers are the offline plane. Gemini's contribution
   to explanation quality is not measured here, because we have no defensible way to score prose
   quality at 1,000 scenarios.
9. **Class imbalance is real and deliberate** — 448 caption scenarios against 13 alternate-audio
   — which is why §3 reports per-feature results rather than only the mean.

## 8. Reproducing

Everything is deterministic given the seed, needs no credentials, no cloud account and no
network:

```bash
pip install -e ".[dev]"
accesspulse bench --scenarios 1000            # ~44 min on 7 workers
```

A single scenario, replayed exactly:

```bash
accesspulse hero --fault infra.stale_config --json run.json
```
