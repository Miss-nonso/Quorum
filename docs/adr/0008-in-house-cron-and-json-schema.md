# 0008. In-house cron parser and JSON Schema subset

- **Status:** Accepted
- **Date:** 2026-11-25

## Context

Two features need parsing and evaluation logic that libraries usually provide:

- **Cron schedules** (Phase 3). The spec requires precise DST semantics: a skipped local time
  runs at the next valid instant, and a repeated local time fires once. Common libraries
  (`croniter`) implement their own DST behaviour, which differs from this, so they would need
  wrapping and re-testing either way.
- **JSON Schema validation** for the `validate` task. The full specification (`$ref`, `oneOf`,
  formats, dialects) is far more than a pipeline step needs, and every supported keyword is also
  attack surface in the public demo.

The project's working agreement is to avoid dependencies outside the agreed stack unless they
earn their place.

## Decision

Both are implemented in-house as pure modules in `domain/`, with property-based tests.

- `domain/jsonschema.py` supports a documented **subset**: `type`, `enum`, `const`,
  `properties`, `required`, `additionalProperties` (boolean), `items`, length, size and numeric
  bounds, and `pattern`.
  - **Unknown keywords are rejected when the schema is checked.** They are never silently
    ignored, so a schema can't quietly mean less than its author intended.
  - JSON semantics apply: `true` is not `1`.
- `domain/cron.py` (Phase 3) supports standard 5-field expressions (lists, ranges, steps, names)
  and computes the next fire time in UTC from an IANA zone, with the DST rules above.

## Consequences

- No new runtime dependencies. Behaviour is exactly what the spec says, and is tested
  (including America/New_York, Europe/London and Africa/Lagos across DST boundaries for cron).
- We own the code. Both modules are small (a few hundred lines) and pure, so the cost is mostly
  writing the tests, which we would need anyway to pin the DST semantics.
- If users need full JSON Schema, swap the `validate` handler to a library behind the same
  params model. The API contract doesn't change.
