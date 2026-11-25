# 0000. Record architecture decisions

- **Status:** Accepted
- **Date:** 2026-11-24

## Context

Quorum makes several choices that are deliberately unusual, such as using PostgreSQL as the
only queue and broker, and promising at-least-once delivery rather than exactly-once. Without
the reasoning written down, a reader can't tell a deliberate trade-off from an oversight, and
future changes can quietly undo decisions that had good reasons behind them.

## Decision

Significant decisions are recorded as short Architecture Decision Records in `docs/adr/`,
numbered sequentially (`NNNN-kebab-title.md`). Each ADR has:

- **Status:** Proposed, Accepted, or Superseded by NNNN.
- **Context:** the forces at play and the constraints.
- **Decision:** what we do, stated plainly.
- **Consequences:** what gets easier, what gets harder, and when to revisit the decision.

An ADR is written in the same change that implements the decision, so the reasoning and the
code (and the tests that back its claims) are reviewed together. Accepted ADRs are not edited
in substance. A changed decision gets a new ADR that supersedes the old one.

## Consequences

- Reviewers and readers can find out why something is the way it is without digging through
  commit history.
- It adds a small amount of writing to each significant change.

Planned ADRs, written in the phases that implement them: PostgreSQL as the queue (Phase 1),
at-least-once delivery plus idempotency keys (Phase 1), leases and fencing tokens (Phase 1),
advisory-lock leader election (Phase 3), priority aging (Phase 3), SSE over WebSockets
(Phase 4), and no arbitrary code execution in the public demo (Phase 6).
