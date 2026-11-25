# 0007. No arbitrary code execution

- **Status:** Accepted
- **Date:** 2026-11-25

## Context

Workflow engines usually run user code (Airflow DAG files, Celery tasks, a shell step). Quorum
has a public demo where anonymous visitors trigger runs, kill workers and flood queues. Running
their code means sandboxing untrusted code: containers per task, seccomp, network egress
control, resource quotas and constant vigilance. On a $5–10/month host, one escape compromises
the box, the database and the demo's reputation.

## Decision

- **A task is a registered type plus validated params. Nothing else executes.** The registry
  (`worker/registry.py`) is the complete list: `http_fetch`, `validate`, `transform`, `store`,
  `notify`, `report`, `sleep` and `flaky`.
- **Params are validated against the type's Pydantic model twice**: when a workflow is saved,
  and again when the task executes, in case a row was edited behind the API's back.
- **Data flows by reference, not by code.** A param may be
  `{"$from": "<upstream node>", "path": "a.b.0"}`. Save-time validation checks that the
  referenced node is upstream of the referencing one, so the data always exists.
- **Transformations are declarative**: filter by simple predicates, project, sort, limit.
  Validation uses a JSON Schema subset. Message templating is `string.Template` substitution,
  which can't call functions or touch attributes.
- **`http_fetch` is fenced in**: operator allowlist of hosts, `http`/`https` only, no redirects
  (one could leave the allowlist), a timeout and a response size cap. 5xx responses are retried;
  4xx responses and policy violations fail permanently.
- **`report` HTML-escapes everything** it renders from upstream data.

## Consequences

- The public demo has no code-execution attack surface. The remaining surface is resource use,
  handled by rate limits and run caps (Phase 6).
- Quorum is less general than a code-running engine. Real deployments that need custom logic
  add task types in code and review them like any other code, the same way they would add an
  endpoint.
- Declarative transforms cover the demo and common ETL shapes (filter, select, sort), not
  arbitrary computation. That's a deliberate ceiling, not an oversight.
