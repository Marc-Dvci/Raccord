"""The remediation executor.

The only component in AccessPulse permitted to change the production
environment. It is deliberately small and boring:

* it accepts a typed ProposedAction and nothing else - no shell, no free-form
  command, no dynamic target construction;
* it re-checks the policy classification itself rather than trusting the caller;
* it redeems the approval token, which binds the action hash and evidence hash;
* it enforces idempotency, so a retried or duplicated agent call cannot execute
  the same change twice;
* it records before/after state for the audit trail and for verification.

An agent holding this object still cannot broaden a scope, invent a target or
grant itself a permission.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .approvals import ApprovalError, ApprovalService
from .contracts import (
    ActionResult,
    Incident,
    PolicyClass,
    ProposedAction,
    utcnow,
)
from .policy import ACTION_CATALOG
from .simulator import MediaSimulator


class ExecutionRefused(Exception):
    """The executor declined to run the action. Never a partial execution."""


@dataclass
class RemediationExecutor:
    sim: MediaSimulator
    approvals: ApprovalService
    executed_keys: set[str] = field(default_factory=set)
    log: list[dict] = field(default_factory=list)

    def execute(self, incident: Incident, action: ProposedAction) -> ActionResult:
        decision = incident.policy_decision
        if decision is None:
            raise ExecutionRefused("no policy decision for this action")
        if decision.action_id != action.action_id:
            raise ExecutionRefused("policy decision does not match the action")
        if decision.classification is PolicyClass.PROHIBITED:
            raise ExecutionRefused(
                "policy prohibits this action: " + "; ".join(decision.rationale)
            )

        spec = ACTION_CATALOG.get(action.action_type)
        if spec is None:
            raise ExecutionRefused("action type is not in the catalog")
        if spec.allowed_targets and action.target not in spec.allowed_targets:
            raise ExecutionRefused(f"target '{action.target}' is not allow-listed")

        key = action.idempotency_key or action.action_hash()
        if key in self.executed_keys:
            self._log("duplicate_suppressed", action)
            return ActionResult(
                action_id=action.action_id,
                incident_id=incident.incident_id,
                executed=False,
                outcome="suppressed: an identical action has already been executed",
                error="duplicate_idempotency_key",
            )

        if decision.classification is PolicyClass.APPROVAL_REQUIRED:
            if incident.approval is None:
                raise ExecutionRefused("approval required but no token presented")
            try:
                self.approvals.redeem(
                    incident.approval.token, action, incident.evidence_hash()
                )
            except ApprovalError as exc:
                self._log("approval_rejected", action, error=str(exc))
                raise ExecutionRefused(str(exc)) from exc

        states = self.sim.apply_action(action.action_type, action.target, action.parameters)
        self.executed_keys.add(key)
        self._log("executed", action, target=action.target)

        return ActionResult(
            action_id=action.action_id,
            incident_id=incident.incident_id,
            executed=True,
            executed_at=utcnow(),
            outcome=f"{spec.title} applied to {action.target}",
            before_state=states["before"],
            after_state=states["after"],
        )

    def rollback(self, incident: Incident, action: ProposedAction) -> ActionResult:
        """Restore the pre-action state recorded by execute()."""
        if incident.action_result is None or not incident.action_result.executed:
            raise ExecutionRefused("nothing to roll back")
        before = incident.action_result.before_state
        self.sim.caption_encoder_pool = before.get("caption_encoder_pool",
                                                   self.sim.caption_encoder_pool)
        self.sim.clock_source = before.get("clock_source", self.sim.clock_source)
        self.sim.caption_path = before.get("caption_path", self.sim.caption_path)
        self.sim.pinned_player_versions = dict(before.get("pinned_player_versions", {}))
        self.sim.rerouted_regions = dict(before.get("rerouted_regions", {}))
        self.sim.disabled_feature_flags = set(before.get("disabled_feature_flags", []))
        self.sim.record_change(
            "config", action.target, f"rollback of {action.action_type.value}",
            actor="accesspulse.remediation",
        )
        self._log("rolled_back", action)
        return ActionResult(
            action_id=action.action_id,
            incident_id=incident.incident_id,
            executed=True,
            outcome=f"rolled back {action.action_type.value} on {action.target}",
            before_state=incident.action_result.after_state,
            after_state=self.sim.state_snapshot(),
        )

    def _log(self, event: str, action: ProposedAction, **detail) -> None:
        self.log.append({
            "at": utcnow().isoformat(),
            "event": event,
            "incident_id": action.incident_id,
            "action_id": action.action_id,
            "action_type": action.action_type.value,
            "action_hash": action.action_hash(),
            **detail,
        })
