# 0005. Server-Sent Events, not WebSockets, for live updates

- **Status:** Accepted
- **Date:** 2026-11-25

## Context

The dashboard shows live task, run and worker state. Data flows one way, from server to browser.
Browser actions (trigger a run, kill a worker, replay a dead letter) are ordinary REST calls that
need request IDs, validation, rate limits and error shapes anyway.

## Decision

- **`GET /api/v1/stream` is a Server-Sent Events stream.** It carries three event types:
  - `run_event`: one row of the `run_events` table, with an SSE `id` equal to the row id;
  - `workers` and `stats`: snapshots every 2s, which are not replayed because only the latest
    one matters;
  - `hello`: sent first, with the server time, so the UI can correct for clock skew in
    heartbeat ages.
- **Reconnects are lossless.** The browser's `EventSource` resends `Last-Event-ID`
  automatically. The server replays `run_events` with a greater id from the table before
  streaming live events, and drops duplicates by id.
- **One database listener per API process.** An `EventHub` holds one LISTEN connection and runs
  one set of polls. Client count doesn't change database load.
  - A statement-level trigger on `run_events` sends an empty `NOTIFY`, so one transaction
    produces one wake-up.
  - The hub reads rows by `id > last_seen`, so a lost notification costs latency, never events.
  - A 1-second poll is the safety net.
- **Slow clients are cut loose.** Each subscriber has a bounded queue. A client that falls
  behind is disconnected, then reconnects and catches up from the table, instead of growing
  server memory.

## Consequences

- Plain HTTP: works through Caddy, CDNs and corporate proxies; automatic reconnect is built into
  browsers; no upgrade handshake, no ping/pong protocol to maintain.
- A comment line every 15s keeps idle proxies from closing the connection. Caddy is configured
  not to buffer the stream (`flush_interval -1`).
- **Limits:**
  - SSE is text-only and one-way. That's fine here, since commands go over REST.
  - Browsers limit HTTP/1.1 connections to about six per origin. HTTP/2 through Caddy removes
    that limit.
- If the dashboard ever needed client-to-server streaming (collaborative editing, say),
  WebSockets would be the right tool. Nothing here needs that.
- **Tested by** `tests/integration/test_stream.py`. It runs a real uvicorn server and checks
  live delivery, reconnect with `Last-Event-ID` (missed events replayed in order, none twice)
  and per-run filtering.
