"""Approval integrity.

An approval is a short-lived, single-use, HMAC-signed capability bound to one
exact action hash, one evidence hash, one incident and one approver. It cannot be
replayed, cannot be widened after issue, and cannot be reused for a different
action even by the same approver.

The remediation executor refuses to run without a token that verifies against all
of those bindings. Every issue, redemption and rejection is an immutable audit
event.
"""

from __future__ import annotations

import base64
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256

from .config import get_settings
from .contracts import Approval, ProposedAction, Role, utcnow


class ApprovalError(Exception):
    """Raised when a token fails any binding check."""


@dataclass
class _Issued:
    token_id: str
    action_hash: str
    evidence_hash: str
    incident_id: str
    approver: str
    role: Role
    expires_at: datetime
    redeemed_at: datetime | None = None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


class ApprovalService:
    def __init__(self, key: bytes | None = None, ttl_seconds: int | None = None) -> None:
        settings = get_settings()
        self._key = key or settings.signing_key()
        self._ttl = ttl_seconds or settings.approval_ttl_seconds
        self._issued: dict[str, _Issued] = {}
        self.audit: list[dict] = []

    # -- issue -------------------------------------------------------------
    def issue(
        self,
        action: ProposedAction,
        evidence_hash: str,
        approver: str,
        role: Role,
        allowed_roles: tuple[Role, ...],
        ttl_seconds: int | None = None,
    ) -> Approval:
        if allowed_roles and role not in allowed_roles:
            self._log(
                "rejected",
                action.action_id,
                approver,
                reason=f"role {role.value} not in {[r.value for r in allowed_roles]}",
            )
            raise ApprovalError(
                f"{approver} holds role {role.value}; this action requires one of "
                f"{[r.value for r in allowed_roles]}"
            )
        now = utcnow()
        expires = now + timedelta(seconds=ttl_seconds or self._ttl)
        token_id = secrets.token_urlsafe(12)
        payload = {
            "jti": token_id,
            "inc": action.incident_id,
            "act": action.action_id,
            "ah": action.action_hash(),
            "eh": evidence_hash,
            "sub": approver,
            "role": role.value,
            "iat": int(now.timestamp()),
            "exp": int(expires.timestamp()),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        sig = hmac.new(self._key, raw, sha256).digest()
        token = f"{_b64(raw)}.{_b64(sig)}"

        self._issued[token_id] = _Issued(
            token_id=token_id,
            action_hash=payload["ah"],
            evidence_hash=evidence_hash,
            incident_id=action.incident_id,
            approver=approver,
            role=role,
            expires_at=expires,
        )
        self._log(
            "issued", action.action_id, approver, token_id=token_id, expires_at=expires.isoformat()
        )
        return Approval(
            approval_id=f"apr-{token_id[:8]}",
            incident_id=action.incident_id,
            action_id=action.action_id,
            approver=approver,
            approver_role=role,
            token=token,
            action_hash=payload["ah"],
            evidence_hash=evidence_hash,
            issued_at=now,
            expires_at=expires,
        )

    # -- redeem ------------------------------------------------------------
    def redeem(
        self,
        token: str,
        action: ProposedAction,
        evidence_hash: str,
        now: datetime | None = None,
    ) -> _Issued:
        now = now or utcnow()
        try:
            raw_b64, sig_b64 = token.split(".")
            raw = _unb64(raw_b64)
            sig = _unb64(sig_b64)
        except Exception as exc:  # noqa: BLE001 - malformed token is a single failure mode
            raise ApprovalError("malformed approval token") from exc

        expected = hmac.new(self._key, raw, sha256).digest()
        if not hmac.compare_digest(sig, expected):
            self._log("rejected", action.action_id, "unknown", reason="bad signature")
            raise ApprovalError("approval token signature is invalid")

        payload = json.loads(raw)
        issued = self._issued.get(payload["jti"])
        if issued is None:
            self._log(
                "rejected",
                action.action_id,
                payload.get("sub", "unknown"),
                reason="unknown token id",
            )
            raise ApprovalError("approval token was not issued by this service")
        if issued.redeemed_at is not None:
            self._log("rejected", action.action_id, issued.approver, reason="replay")
            raise ApprovalError("approval token has already been redeemed")
        if now.timestamp() > payload["exp"]:
            self._log("rejected", action.action_id, issued.approver, reason="expired")
            raise ApprovalError("approval token has expired")
        if payload["inc"] != action.incident_id:
            raise ApprovalError("approval token is bound to a different incident")
        if payload["act"] != action.action_id:
            raise ApprovalError("approval token is bound to a different action")
        if payload["ah"] != action.action_hash():
            self._log(
                "rejected",
                action.action_id,
                issued.approver,
                reason="action changed after approval",
            )
            raise ApprovalError("the action changed after it was approved (action hash mismatch)")
        if payload["eh"] != evidence_hash:
            self._log(
                "rejected",
                action.action_id,
                issued.approver,
                reason="evidence changed after approval",
            )
            raise ApprovalError("the evidence changed after approval (evidence hash mismatch)")

        issued.redeemed_at = now
        self._log("redeemed", action.action_id, issued.approver, token_id=issued.token_id)
        return issued

    # -- audit -------------------------------------------------------------
    def _log(self, event: str, action_id: str, approver: str, **detail) -> None:
        self.audit.append(
            {
                "at": utcnow().isoformat(),
                "event": event,
                "action_id": action_id,
                "approver": approver,
                **detail,
            }
        )

    def outstanding(self) -> list[_Issued]:
        now = utcnow()
        return [i for i in self._issued.values() if i.redeemed_at is None and i.expires_at > now]

    def reset(self) -> None:
        self._issued.clear()
        self.audit.clear()
