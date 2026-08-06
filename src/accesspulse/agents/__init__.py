"""Agent layer.

Eleven agents with narrow, typed responsibilities. The coordinator owns state;
every other agent reads typed records and returns typed records. The reasoning
plane (Gemini via ADK) sits on top for synthesis and role-specific language; it
cannot bypass the state machine, the policy engine or the executor.
"""

from __future__ import annotations

from .coordinator import (
    REMEDIATION_MAP,
    CoordinatorConfig,
    IncidentCoordinator,
    publish_metrics,
)
from .core import (
    TAXONOMY,
    CausalCandidate,
    ChangeCorrelationAgent,
    CommunicationAgent,
    DiagnosisAgent,
    MultimodalQualityAgent,
    ReliabilityLearningAgent,
    ScopeAgent,
    Signature,
)
from .evidence import REQUIRED_CHAIN, GrafanaEvidenceAgent

__all__ = [
    "REMEDIATION_MAP",
    "REQUIRED_CHAIN",
    "TAXONOMY",
    "CausalCandidate",
    "ChangeCorrelationAgent",
    "CommunicationAgent",
    "CoordinatorConfig",
    "DiagnosisAgent",
    "GrafanaEvidenceAgent",
    "IncidentCoordinator",
    "MultimodalQualityAgent",
    "ReliabilityLearningAgent",
    "ScopeAgent",
    "Signature",
    "publish_metrics",
]
