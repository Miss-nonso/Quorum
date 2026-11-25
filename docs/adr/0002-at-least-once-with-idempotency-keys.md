# 0002. At-least-once delivery with idempotency keys

- **Status:** Accepted
- **Date:** 2026-11-24

## Context

A worker can die at any instant: before starting a task, halfway through it, after its side
effect but before it reports completion, or while the completion is in flight. The system cannot
tell "crashed before the effect" from "crashed after the effect", because the information died
with the process.

That leaves two options:

- **At-most-once:** never retry an attempt whose fate is unknown. Work is silently lost.
- **At-least-once:** retry it. Work is never lost, but may be executed more than once.

"Exactly-once delivery" is not achievable in this failure model (it reduces to the two-generals
problem), and claiming it would be dishonest.

## Decision

- **Execution is at-least-once.** A task whose lease expires is retried. Lease expiry consumes an
  attempt, so a task that reliably crashes its worker is eventually dead-lettered instead of taking
  the pool down forever.
- **Every task carries an idempotency key.** By default it is `"{run_id}:{node_key}"`, which is
  stable across retries and replays. Callers may override it, for example with an order id, and
  `task_instances.idempotency_key` is unique.
- **Built-in tasks with side effects record the effect under that key, behind a unique
  constraint.** `notify` uses `INSERT … ON CONFLICT (idempotency_key) DO NOTHING`, so a
  re-execution observes the conflict, records `duplicate_suppressed: true` and succeeds without a
  second notification.
- The combination is described as **effectively-once effects**, never as "exactly-once
  delivery".

## Consequences

- **No lost work, and no duplicate effects** for any effect that can be keyed in a store with a
  uniqueness guarantee (our own tables, or an external API that accepts an idempotency key).
- **Task authors must think about idempotency.** The registry flags side-effecting types
  (`has_side_effects=True`), and each one must document how it deduplicates.
- **An effect without a dedupe mechanism is only at-least-once.** A webhook to a receiver that
  ignores idempotency keys can fire twice. `docs/semantics.md` says this explicitly.
- **The effect commits separately from task completion, in its own transaction.** That is
  deliberate: it models a real external effect, which can never join our transaction. Effects
  written to Conductor's own tables could share the completion transaction. We don't rely on
  that, so the guarantee is the same for internal and external effects.
- The chaos suite proves the claim: it kills a worker after its effect but before its completion
  and asserts that exactly one notification exists.
