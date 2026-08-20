"""The deterministic incident state machine.

Twelve states, one legal path, machine-checkable preconditions on every
transition, and a hash-chained audit log. The coordinator agent cannot skip a
state, cannot reach ACTION_EXECUTING without a redeemed approval when policy
demanded one, and cannot reach RECOVERED with a failing mandatory assertion.

The language model proposes; this module decides whether the proposal is even
representable.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Callable

from .contracts import (
    INCIDENT_TRANSITIONS,
    AssertionStatus,
    AuditEvent,
    Incident,
    IncidentState,
    PolicyClass,
    can_transition,
    stable_hash,
    utcnow,
)


class IncidentStateError(Exception):
    """Raised when a transition is illegal or its preconditions are unmet."""


# ---------------------------------------------------------------------------
# Preconditions: each returns the list of unmet requirements (empty == ok)
# ---------------------------------------------------------------------------


def _p_qualified(inc: Incident) -> list[str]:
    unmet = []
    if inc.alert is None:
        unmet.append("no alert attached")
    elif inc.alert.state != "firing":
        unmet.append("alert is not firing")
    return unmet


def _p_scoped(inc: Incident) -> list[str]:
    unmet = []
    if inc.scope is None:
        unmet.append("scope not computed")
        return unmet
    if not inc.scope.features:
        unmet.append("scope names no affected feature")
    if not inc.scope.violated_promise_ids:
        unmet.append("scope names no violated promise")
    if inc.scope.affected_sessions < 0:
        unmet.append("affected session aggregate is negative")
    return unmet


REQUIRED_EVIDENCE_TOOLS = (
    "grafana.mcp:list_alert_rules",
    "grafana.mcp:query_prometheus",
    "grafana.mcp:query_loki_logs",
    "grafana.mcp:query_tempo_traces",
    "grafana.mcp:search_dashboards",
)


def _p_evidence_complete(inc: Incident) -> list[str]:
    unmet = []
    tools = {e.source_tool for e in inc.evidence}
    for required in REQUIRED_EVIDENCE_TOOLS:
        if required not in tools:
            unmet.append(f"missing evidence from {required}")
    if not inc.findings:
        unmet.append("no probe or model findings attached")
    return unmet


def _p_diagnosed(inc: Incident) -> list[str]:
    unmet = []
    if not inc.hypotheses:
        unmet.append("no hypotheses produced")
        return unmet
    top = inc.hypotheses[0]
    # An abstention is a legitimate diagnosis: it records that the evidence does
    # not support a conclusion. What it blocks is an *action* - the policy stage
    # refuses to propose one - not the transition into DIAGNOSED.
    if not top.supporting_evidence_ids:
        unmet.append("top hypothesis cites no evidence")
    known = {e.evidence_id for e in inc.evidence}
    dangling = [e for e in top.supporting_evidence_ids if e not in known]
    if dangling:
        unmet.append(f"hypothesis cites unknown evidence ids: {dangling}")
    return unmet


def _p_policy_evaluated(inc: Incident) -> list[str]:
    unmet = []
    if inc.proposed_action is None:
        unmet.append("no action proposed")
    if inc.policy_decision is None:
        unmet.append("policy not evaluated")
    elif inc.proposed_action and inc.policy_decision.action_id != inc.proposed_action.action_id:
        unmet.append("policy decision does not match the proposed action")
    return unmet


def _p_awaiting_approval(inc: Incident) -> list[str]:
    if inc.policy_decision is None:
        return ["policy not evaluated"]
    if inc.policy_decision.classification is not PolicyClass.APPROVAL_REQUIRED:
        return ["policy did not require approval"]
    return []


def _p_action_executing(inc: Incident) -> list[str]:
    unmet = []
    if inc.proposed_action is None:
        return ["no action proposed"]
    if inc.policy_decision is None:
        return ["policy not evaluated"]
    if inc.policy_decision.classification is PolicyClass.PROHIBITED:
        unmet.append("policy prohibits this action")
    if inc.policy_decision.classification is PolicyClass.APPROVAL_REQUIRED:
        if inc.approval is None:
            unmet.append("approval required but not present")
        else:
            if inc.approval.action_hash != inc.proposed_action.action_hash():
                unmet.append("approval is bound to a different action")
            if inc.approval.evidence_hash != inc.evidence_hash():
                unmet.append("evidence changed after approval")
            if inc.approval.expires_at <= utcnow():
                unmet.append("approval token expired")
    return unmet


def _p_verifying(inc: Incident) -> list[str]:
    if inc.action_result is None:
        return ["no action result"]
    if not inc.action_result.executed:
        return ["action did not execute"]
    return []


def _p_recovered(inc: Incident) -> list[str]:
    unmet = []
    if not inc.assertions:
        return ["verification did not run"]
    for a in inc.assertions:
        if a.mandatory and a.status is not AssertionStatus.PASSING:
            unmet.append(f"mandatory assertion '{a.name}' is {a.status.value}")
    return unmet


def _p_rolled_back(inc: Incident) -> list[str]:
    if not inc.assertions:
        return ["verification did not run"]
    if all(a.status is AssertionStatus.PASSING for a in inc.assertions if a.mandatory):
        return ["no mandatory assertion failed; rollback is not justified"]
    return []


def _p_communicated(inc: Incident) -> list[str]:
    audiences = {c.audience for c in inc.communications}
    unmet = []
    for required in ("operator", "public_status", "executive"):
        if required not in audiences:
            unmet.append(f"no communication generated for {required}")
    leaking = [
        c.communication_id
        for c in inc.communications
        if c.audience == "public_status" and c.contains_internal_detail
    ]
    if leaking:
        unmet.append(f"public status contains internal detail: {leaking}")
    return unmet


def _p_reviewed(inc: Incident) -> list[str]:
    if inc.state not in (IncidentState.COMMUNICATED, IncidentState.ROLLED_BACK):
        return ["review requires a communicated or rolled-back incident"]
    return []


PRECONDITIONS: dict[IncidentState, Callable[[Incident], list[str]]] = {
    IncidentState.QUALIFIED: _p_qualified,
    IncidentState.SCOPED: _p_scoped,
    IncidentState.EVIDENCE_COMPLETE: _p_evidence_complete,
    IncidentState.DIAGNOSED: _p_diagnosed,
    IncidentState.POLICY_EVALUATED: _p_policy_evaluated,
    IncidentState.AWAITING_APPROVAL: _p_awaiting_approval,
    IncidentState.ACTION_EXECUTING: _p_action_executing,
    IncidentState.VERIFYING: _p_verifying,
    IncidentState.RECOVERED: _p_recovered,
    IncidentState.ROLLED_BACK: _p_rolled_back,
    IncidentState.COMMUNICATED: _p_communicated,
    IncidentState.REVIEWED: _p_reviewed,
    IncidentState.REJECTED: lambda inc: [],
}


# ---------------------------------------------------------------------------
# Machine
# ---------------------------------------------------------------------------


class IncidentMachine:
    """Owns one incident's state. All mutation goes through here."""

    def __init__(self, incident: Incident, store: "IncidentStore | None" = None) -> None:
        self.incident = incident
        self.store = store
        if not incident.audit:
            self._append_audit(
                "incident.opened",
                None,
                IncidentState.DETECTED,
                actor="raccord.coordinator",
                detail={},
            )

    # -- audit -------------------------------------------------------------
    def _append_audit(
        self,
        event: str,
        from_state: IncidentState | None,
        to_state: IncidentState | None,
        actor: str,
        detail: dict,
    ) -> AuditEvent:
        prev_hash = self.incident.audit[-1].hash if self.incident.audit else ""
        seq = len(self.incident.audit) + 1
        body = {
            "seq": seq,
            "incident_id": self.incident.incident_id,
            "event": event,
            "from": from_state.value if from_state else None,
            "to": to_state.value if to_state else None,
            "actor": actor,
            "detail": detail,
            "prev_hash": prev_hash,
        }
        ev = AuditEvent(
            seq=seq,
            incident_id=self.incident.incident_id,
            at=utcnow(),
            actor=actor,
            event=event,
            from_state=from_state,
            to_state=to_state,
            detail=detail,
            prev_hash=prev_hash,
            hash=stable_hash(body),
        )
        self.incident.audit.append(ev)
        if self.store:
            self.store.append_audit(ev)
        return ev

    def note(self, event: str, actor: str, **detail) -> AuditEvent:
        return self._append_audit(event, self.incident.state, self.incident.state, actor, detail)

    # -- transitions -------------------------------------------------------
    def can(self, target: IncidentState) -> tuple[bool, list[str]]:
        if not can_transition(self.incident.state, target):
            return False, [
                f"{self.incident.state.value} -> {target.value} is not a legal transition; "
                f"legal: {[s.value for s in INCIDENT_TRANSITIONS[self.incident.state]]}"
            ]
        unmet = PRECONDITIONS.get(target, lambda _: [])(self.incident)
        return (not unmet), unmet

    def transition(
        self,
        target: IncidentState,
        actor: str = "raccord.coordinator",
        **detail,
    ) -> AuditEvent:
        ok, unmet = self.can(target)
        if not ok:
            self._append_audit(
                "transition.rejected",
                self.incident.state,
                target,
                actor,
                {"unmet": unmet, **detail},
            )
            raise IncidentStateError(
                f"cannot move incident {self.incident.incident_id} to {target.value}: "
                + "; ".join(unmet)
            )
        previous = self.incident.state
        self.incident.state = target
        if target in (IncidentState.REVIEWED, IncidentState.REJECTED):
            self.incident.closed_at = utcnow()
        ev = self._append_audit("transition", previous, target, actor, detail)
        if self.store:
            self.store.save(self.incident)
        return ev

    # -- integrity ---------------------------------------------------------
    def verify_audit_chain(self) -> bool:
        prev = ""
        for ev in self.incident.audit:
            body = {
                "seq": ev.seq,
                "incident_id": ev.incident_id,
                "event": ev.event,
                "from": ev.from_state.value if ev.from_state else None,
                "to": ev.to_state.value if ev.to_state else None,
                "actor": ev.actor,
                "detail": ev.detail,
                "prev_hash": prev,
            }
            if ev.prev_hash != prev or ev.hash != stable_hash(body):
                return False
            prev = ev.hash
        return True

    @property
    def state(self) -> IncidentState:
        return self.incident.state


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    event_id    TEXT NOT NULL,
    state       TEXT NOT NULL,
    severity    TEXT NOT NULL,
    opened_at   TEXT NOT NULL,
    closed_at   TEXT,
    body        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    incident_id TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    at          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    event       TEXT NOT NULL,
    from_state  TEXT,
    to_state    TEXT,
    detail      TEXT NOT NULL,
    prev_hash   TEXT NOT NULL,
    hash        TEXT NOT NULL,
    PRIMARY KEY (incident_id, seq)
);
"""


class IncidentStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def save(self, incident: Incident) -> None:
        self._conn.execute(
            "INSERT INTO incidents VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(incident_id) DO UPDATE SET state=excluded.state, "
            "severity=excluded.severity, closed_at=excluded.closed_at, body=excluded.body",
            (
                incident.incident_id,
                incident.event_id,
                incident.state.value,
                incident.severity.value,
                incident.opened_at.isoformat(),
                incident.closed_at.isoformat() if incident.closed_at else None,
                incident.model_dump_json(),
            ),
        )
        self._conn.commit()

    def append_audit(self, ev: AuditEvent) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO audit VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                ev.incident_id,
                ev.seq,
                ev.at.isoformat(),
                ev.actor,
                ev.event,
                ev.from_state.value if ev.from_state else None,
                ev.to_state.value if ev.to_state else None,
                json.dumps(ev.detail, default=str),
                ev.prev_hash,
                ev.hash,
            ),
        )
        self._conn.commit()

    def get(self, incident_id: str) -> Incident | None:
        row = self._conn.execute(
            "SELECT body FROM incidents WHERE incident_id = ?", (incident_id,)
        ).fetchone()
        return Incident.model_validate_json(row["body"]) if row else None

    def list(self, event_id: str | None = None, limit: int = 100) -> list[Incident]:
        if event_id:
            rows = self._conn.execute(
                "SELECT body FROM incidents WHERE event_id = ? ORDER BY opened_at DESC LIMIT ?",
                (event_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT body FROM incidents ORDER BY opened_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [Incident.model_validate_json(r["body"]) for r in rows]

    def audit_for(self, incident_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM audit WHERE incident_id = ? ORDER BY seq", (incident_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def reset(self) -> None:
        self._conn.executescript("DELETE FROM incidents; DELETE FROM audit;")
        self._conn.commit()
