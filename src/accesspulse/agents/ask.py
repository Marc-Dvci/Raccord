"""Operator question answering over an open incident.

An operator looking at a ranked diagnosis wants to interrogate it: *why did you
rule out a fixed clock offset? what changed just before this started? how do you
know it is actually fixed?* Those questions are answerable from the typed
incident record, and answering them well is what turns a dashboard into a
colleague.

Two planes, the same contract:

* **Gemini (`AP_REASONING_MODE=gemini`)** answers with the Grafana MCP toolset in
  hand, so a question the current evidence cannot settle causes it to *go and
  fetch more* — through the same audited MCP path as the investigation itself.
  Every call it makes lands in the same call log the Agent & MCP view renders.
* **Offline (the default)** answers deterministically from the incident record by
  resolving the question to an intent and citing the evidence that bears on it.

Both return an `Answer` carrying the evidence it rests on, so the operator can
follow a claim back to the Grafana query that produced it. Neither plane can
change anything: this is a read path, and the Gemini toolset is the same
read-only surface described in `adk.py`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..contracts import AssertionStatus, Evidence, Incident

# Intent patterns, most specific first. Deliberately small and readable: this is
# a fallback that must be honest about what it is, not an NLU system pretending
# to be a model.
# Note the deliberate absence of a trailing \b: these are word *stems*, so
# "chang" has to match "changed" and "approv" has to match "approved". A closing
# boundary silently breaks every inflected form, which is the kind of bug that
# looks like a bad model rather than a bad regex.
_INTENTS: list[tuple[str, str]] = [
    ("ruled_out",
     r"\b(rule[sd]?\s*out|why not|discount|dismiss|reject|too low|rank(ed)? low)"),
    ("approval",
     r"\b(approv|authoris|authoriz|who signed|sign-?off|permission|token)"),
    ("verified",
     r"\b(verif|how do (you|we) know it|is it (really )?fixed|assertion|proof|prove)"),
    ("change",
     r"\b(chang|deploy|releas|trigger|regress|what caused"
     r"|why did (this|it) (start|happen))"),
    ("scope",
     r"\b(scope|affect|impact|territor|blast|who is|how many"
     r"|which (device|build|language))"),
    ("unknown",
     r"\b(unknown|uncertain|confiden|not sure|abstain|gap|risk)"),
    ("evidence",
     r"\b(evidence|how do you know|what makes|support|basis"
     r"|why do you (think|believe))"),
]


@dataclass
class Answer:
    """One answered operator question."""

    question: str
    text: str
    plane: str  # "gemini" | "offline"
    evidence: list[dict] = field(default_factory=list)
    mcp_calls_made: int = 0
    model: str | None = None
    intent: str | None = None

    def as_dict(self) -> dict:
        return {
            "question": self.question,
            "answer": self.text,
            "plane": self.plane,
            "evidence": self.evidence,
            "mcp_calls_made": self.mcp_calls_made,
            "model": self.model,
            "intent": self.intent,
        }


def classify(question: str) -> str:
    q = question.lower()
    for name, pattern in _INTENTS:
        if re.search(pattern, q):
            return name
    return "summary"


def _cite(evidence: list[Evidence], ids: tuple[str, ...] | list[str]) -> list[dict]:
    wanted = set(ids)
    return [
        {"evidence_id": e.evidence_id, "tool": e.source_tool, "summary": e.summary,
         "deep_link": e.deep_link}
        for e in evidence if e.evidence_id in wanted
    ]


def _all_cites(evidence: list[Evidence], limit: int = 4) -> list[dict]:
    return [
        {"evidence_id": e.evidence_id, "tool": e.source_tool, "summary": e.summary,
         "deep_link": e.deep_link}
        for e in evidence[:limit]
    ]


def _named_hypothesis(incident: Incident, question: str):
    """Find the hypothesis the operator is asking about, if they named one."""
    q = question.lower()
    best = None
    best_hits = 0
    for h in incident.hypotheses:
        text = f"{h.failure_class.value} {h.statement}".lower()
        words = {w for w in re.split(r"[^a-z]+", text) if len(w) > 4}
        hits = sum(1 for w in words if w in q)
        if hits > best_hits:
            best, best_hits = h, hits
    return best if best_hits >= 1 else None


def answer_offline(incident: Incident, question: str, causal: list | None = None) -> Answer:
    """Answer from the typed record. No network, no model, no guessing."""
    intent = classify(question)
    evidence = list(incident.evidence)
    causal = causal or []
    top = incident.hypotheses[0] if incident.hypotheses else None
    scope = incident.scope

    def done(text: str, cites: list[dict]) -> Answer:
        return Answer(question=question, text=text, plane="offline",
                      evidence=cites, intent=intent)

    if intent == "ruled_out":
        target = _named_hypothesis(incident, question)
        if target is None:
            ranked = ", ".join(
                f"{h.failure_class.value} ({h.posterior:.2f})" for h in incident.hypotheses[1:5]
            )
            return done(
                "I did not rule anything out categorically — the diagnosis is a ranked "
                f"posterior. Below the leading hypothesis: {ranked or 'no further candidates'}. "
                "Name one and I will give you the evidence that argues against it.",
                _all_cites(evidence, 2),
            )
        cites = _cite(evidence, target.contradicting_evidence_ids or target.supporting_evidence_ids)
        note = target.uncertainty_note or "no explicit contradiction was recorded"
        return done(
            f"{target.failure_class.value} is ranked #{target.rank} at posterior "
            f"{target.posterior:.2f}, not excluded. {target.statement} "
            f"Evidence against it: {note}. "
            f"It sits below {top.failure_class.value} ({top.posterior:.2f}) because that "
            "hypothesis matches both the breached SLO signature and the change correlation."
            if top else f"{target.failure_class.value}: posterior {target.posterior:.2f}.",
            cites,
        )

    if intent == "verified":
        passing = [a for a in incident.assertions if a.status is AssertionStatus.PASSING]
        failed = [a for a in incident.assertions if a.status is AssertionStatus.FAILING]
        adjacent = [a for a in incident.assertions if "adjacent" in a.scope_note.lower()]
        return done(
            f"{len(passing)} of {len(incident.assertions)} assertions passed after the action, "
            "each one a fresh measurement rather than a restated expectation. "
            f"{len(adjacent)} of them cover adjacent scope — the languages, features and "
            "devices that were *not* broken, checked to prove the repair did not regress them. "
            + (f"Failing: {', '.join(a.name for a in failed)}."
               if failed else "Nothing failed, so the incident was allowed to close."),
            _cite(evidence, [e.evidence_id for e in evidence
                             if "recovery" in (e.summary or "").lower()
                             or "post-action" in (e.summary or "").lower()][:3]),
        )

    if intent == "approval":
        action = incident.proposed_action
        decision = incident.policy_decision
        if not decision:
            return done("No policy decision has been recorded on this incident yet.", [])
        roles = ", ".join(r.value for r in decision.required_roles) or "none"
        why = "; ".join(decision.rationale) or "no rationale recorded"
        return done(
            f"Policy classed this as {decision.classification.value}, because {why}. "
            f"Required authority: {roles}. "
            + (f"The action is {action.action_type.value} on {action.target}, and the executor "
               "will not run it without a signed, single-use token bound to that exact action "
               "hash and the evidence hash behind it."
               if action else ""),
            [],
        )

    if intent == "change":
        # Prefer the *ranked* candidates: they carry the resolved component, the
        # correlation score and the reasons for and against. The raw change list
        # often reports "unknown" for a component the ranking has since resolved.
        if causal:
            lines = []
            for c in causal[:4]:
                why = "; ".join(c.supporting) or "no supporting signal"
                lines.append(
                    f"- {c.score:.2f} · {c.change.component} · {c.change.kind} · "
                    f"{c.change.description} — {why}"
                )
            body = "\n".join(lines)
        elif incident.changes:
            body = "\n".join(
                f"- {c.component} · {c.kind} · {c.description} "
                f"({c.at.isoformat(timespec='seconds')})"
                for c in incident.changes[:4]
            )
        else:
            return done("No deployment or configuration change was correlated to this onset.", [])
        return done(
            "Changes correlated to the symptom onset, strongest first:\n" + body
            + "\n\nCorrelation is scored on temporal proximity to onset and on whether the "
              "component sits upstream of the affected delivery path. It is evidence, not "
              "proof of causation, which is why the ranked diagnosis carries it as one input "
              "among several.",
            _cite(evidence, [e.evidence_id for e in evidence
                             if "annotation" in e.source_tool][:3]),
        )

    if intent == "scope":
        if not scope:
            return done("Scope has not been established on this incident yet.", [])
        return done(
            f"{scope.affected_sessions:,} accessibility-enabled sessions across "
            f"{'/'.join(scope.territories) or 'all territories'}, on "
            f"{'/'.join(scope.player_versions) or 'all builds'}, in "
            f"{'/'.join(scope.languages) or 'all languages'}. Blast class {scope.blast_class}. "
            f"{scope.protected_sessions:,} sessions were unaffected and stayed that way — "
            "that number is the point of the adjacent-scope assertions.",
            _all_cites(evidence, 2),
        )

    if intent == "unknown":
        abstained = [f for f in incident.findings if f.abstained]
        degraded = [f for f in incident.findings if f.data_quality != "ok"]
        parts = []
        if top:
            parts.append(
                f"The leading hypothesis carries posterior {top.posterior:.2f}, so "
                f"{1 - top.posterior:.0%} of the probability mass is elsewhere."
            )
        parts.append(
            f"{len(abstained)} probe finding(s) abstained rather than reporting a value, and "
            f"{len(degraded)} were collected under degraded data quality."
        )
        if top and top.uncertainty_note:
            parts.append(f"Recorded caveat: {top.uncertainty_note}")
        return done(" ".join(parts), [])

    if intent == "evidence" and top:
        return done(
            f"{top.statement} Posterior {top.posterior:.2f}, ranked #{top.rank} of "
            f"{len(incident.hypotheses)}. It rests on "
            f"{len(top.supporting_evidence_ids)} piece(s) of evidence, every one retrieved "
            "through the Grafana MCP server and listed below with the tool that produced it.",
            _cite(evidence, top.supporting_evidence_ids),
        )

    state = incident.state.value
    return done(
        f"Incident {incident.incident_id} is in state {state}. "
        + (f"Leading hypothesis: {top.statement} (posterior {top.posterior:.2f}). "
           if top else "No hypothesis has been ranked yet. ")
        + f"{len(evidence)} pieces of evidence, all through Grafana MCP. "
        "Ask about the evidence, what changed, who is affected, what is still uncertain, "
        "why a hypothesis was ranked low, who approved the action, or how recovery was verified.",
        _all_cites(evidence, 3),
    )


async def answer(incident: Incident, question: str, mcp=None,
                 causal: list | None = None) -> Answer:
    """Answer an operator question, using Gemini when the reasoning plane is on.

    `mcp` is the Grafana MCP client, passed so the number of calls the answer
    caused can be reported back to the operator — a follow-up question that
    pulls fresh evidence should be visible as such.
    """
    question = question.strip()
    if not question:
        return Answer(question=question, text="Ask a question about this incident.",
                      plane="offline", intent="summary")

    from . import adk

    if not adk.available():
        return answer_offline(incident, question, causal)

    before = len(mcp.call_log) if mcp is not None else 0
    try:
        result = await adk.ask(incident, question)
    except Exception as exc:  # the operator gets an answer either way
        fallback = answer_offline(incident, question, causal)
        fallback.text = (
            f"The Gemini reasoning plane could not be reached ({type(exc).__name__}); "
            "answering from the incident record instead.\n\n" + fallback.text
        )
        return fallback

    after = len(mcp.call_log) if mcp is not None else 0
    return Answer(
        question=question,
        text=result["answer"],
        plane="gemini",
        evidence=_cite(list(incident.evidence), result.get("evidence_ids", []))
        or _all_cites(list(incident.evidence), 3),
        mcp_calls_made=max(0, after - before),
        model=result.get("model"),
        intent=classify(question),
    )
