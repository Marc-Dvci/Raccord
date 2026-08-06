# Architecture decision records

Each record states the decision, the alternatives that were seriously considered, and what the
decision costs — because a decision recorded without its cost is marketing, not architecture.

| # | Decision | Status |
|---|---|---|
| [0001](0001-deterministic-core-decides.md) | The deterministic core decides; the model explains | Accepted |
| [0002](0002-grafana-mcp-is-the-only-route-to-truth.md) | Grafana MCP is the agent's only route to operational truth | Accepted |
| [0003](0003-twelve-state-incident-machine.md) | A twelve-state incident machine with machine-checkable preconditions | Accepted |
| [0004](0004-policy-as-code-and-an-action-catalog.md) | Policy as code over an allow-listed action catalog | Accepted |
| [0005](0005-approval-tokens-bound-to-hashes.md) | Approval tokens bound to an action hash and an evidence hash | Accepted |
| [0006](0006-verification-re-measures-including-adjacent-scope.md) | Verification re-measures, including adjacent scope | Accepted |
| [0007](0007-probes-abstain-rather-than-guess.md) | Probes abstain rather than guess | Accepted |
| [0008](0008-twin-as-timed-event-list.md) | The twin models the programme as a timed event list | Accepted |
| [0009](0009-never-infer-disability.md) | Never infer disability; measure delivery, aggregate with suppression | Accepted |
| [0010](0010-dependency-free-ui.md) | A dependency-free user interface | Accepted |
| [0011](0011-offline-reasoning-is-the-default.md) | Offline reasoning is the default | Accepted |
| [0012](0012-ablations-share-a-subset.md) | Ablations are compared against the same subset | Accepted |

Format: context · decision · alternatives considered · consequences. Records are immutable once
accepted; a changed decision gets a new record that supersedes the old one.
