# ADR 0006: Telegram outbound notifications through a PostgreSQL outbox

## Status

Proposed. The pure M12.0 presentation contract is implemented; real Telegram
network access remains unapproved.

## Context

Administrators benefit from compact plan-ready, completion, and attention
notifications outside the Web UI. ani-rss demonstrates the useful presentation
shape: TMDB artwork plus a short caption, with text fallback. Its process-local
queue is not sufficient for Reeloom because a restart must not silently lose
pending delivery work.

Reeloom already treats PostgreSQL as the control-plane metadata owner and has
strict boundaries around Agent tools, approvals, filesystem effects, secrets,
and network adapters. Notification delivery must not weaken those boundaries.

## Decision

Use a PostgreSQL transactional outbox and a single bounded delivery worker.
Domain facts are deterministically projected to versioned notification payloads
and inserted with a stable unique dedupe key. The worker claims rows with a
lease, performs HTTP outside the database transaction, and settles the row as
sent, retryable, or dead.

The public notification family is closed to plan ready, archive completed, and
attention required. A field-free test variant exists only for an explicit Admin
configuration test. All copy is fixed application code; arbitrary templates,
Agent text, paths, filenames, raw exceptions, and arbitrary URLs are excluded.

Presentation uses Telegram MarkdownV2 with complete dynamic-text escaping.
Artwork is optional and can only be formed from a validated TMDB poster ref and
the fixed TMDB image host. Captions are capped at 900 bytes so the same rendered
payload fits the more restrictive photo-caption path.

Telegram is an observational side effect. Delivery state never enters the
Reeloom domain reducer and cannot approve, apply, recover, or mutate files.

## Reliability semantics

The outbox provides durable at-least-once delivery. A unique dedupe key removes
duplicate local production, leases recover worker crashes, and bounded retry
handles transient network errors, `5xx`, and `429`. Telegram does not expose a
caller idempotency key, so a crash after remote success but before the receipt is
committed can produce a duplicate message. The system reports this honestly and
does not claim exactly-once delivery.

## Security gate

The current repository invariant permits TMDB as the only business network
adapter. A real Telegram adapter is therefore deferred. M12.1 may implement the
database outbox using only a fake sender. M12.2 requires an explicit, separately
reviewed policy change that names Telegram as a fixed-purpose adapter; this ADR
does not grant that authority by itself.

## Consequences

- pending notifications survive process and worker restarts;
- notification failures remain isolated from organizing and apply correctness;
- schemas, retry behavior, and residual duplicate risk are explicit and testable;
- PostgreSQL and worker lifecycle gain additional operational state;
- full delivery cannot ship until the network invariant is deliberately updated.

## References

- [M12 plan](../m12-plan.md)
- [M12 threat model](../m12-threat-model.md)
- [M12 requirement matrix](../m12-requirements.md)
- [ani-rss](https://github.com/wushuo894/ani-rss)
