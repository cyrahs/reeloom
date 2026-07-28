# M8 threat model

## Scope and protected assets

M8 protects authorized media roots, immutable RenamePlan/AmendmentPlan bytes,
approval authority, provider and TMDB credentials, PostgreSQL control-plane
history, SDK session identity, and Executor journals. The browser, filenames,
subtitles, TMDB text, Agent output, interaction text, tool observations, HTTP
clients, provider responses, and media-directory contents are untrusted.

The deployment operator, `reeloom` database owner role, supported PostgreSQL
instance, host kernel, mounted state root, and configured reverse proxy are
trusted computing base. Compromise of those components is outside the
application boundary and requires credential rotation plus backup restore.

## Trust boundaries and controls

| Boundary | Main threats | M8 controls |
| --- | --- | --- |
| Browser → API | credential misuse, CSRF-like cross-origin calls, request smuggling, replay, secret/path disclosure | bearer roles, exact Host/Origin, strict JSON with duplicate rejection, bounded body/text/page/rate/concurrency, required idempotency key and expected head, allowlisted errors/events/SSE |
| Admin config → filesystem/network capability | Agent-selected paths or URL, secret disclosure, config drift | admin-only versioned CAS, exact config revision bound to every run, authorized absolute roots, write-only no-follow SecretStore; configuring an HTTPS provider explicitly authorizes disclosure to that origin |
| Provider/TMDB network | redirect, environment proxy, DNS rebinding, arbitrary Agent HTTP | HTTPS-only provider URL, pinned resolved origin, redirects and environment proxy disabled, bounded transport; TMDB is the sole business network adapter and neither adapter is an Agent URL tool |
| Agent/session → domain plan | prompt injection, assistant-text completion, stale mapping reuse, arbitrary paths | fixed content-addressed AgentDefinitionRevision, original SDK session, opaque candidate IDs, phase/capability tool policy, fresh `submit_mapping`, deterministic compiler; no shell/path/URL tools |
| API/application → PostgreSQL | lost update, replay, dual owner, history mutation, long transaction | PostgreSQL is the sole control-plane owner, CAS/unique constraints, immutable history privileges and triggers, short use-case transactions, durable idempotency |
| Approval → Executor | forged/stale/replayed approval, plan substitution | exact `run_id + plan_hash + scope + expiry + nonce`, immutable approval, unique claim, persisted plan hash only |
| Executor → media | symlink/path escape, target overwrite, TOCTOU, partial move | authorized roots, no-follow identity revalidation, destination absence, no-replace rename, journal and fsync before mutation, rollback and deterministic recovery |
| Crash/restart | duplicate model call, duplicate rename, ambiguous commit, semantic recovery | terminal interaction records, typed idempotency resolution, one active run operation, one-time claim, terminal journal reconciliation, recovery without LLM |
| Deployment | second instance/worker, schema drift, database loss | process plus lifetime advisory locks, `workers=1`, PostgreSQL 16–18/version/checksum health, one explicit database-owner DSN, fatal background DB state |

## Data exposure rules

Provider secrets are accepted only on authenticated config writes and are never
returned. Deployment DSNs and TMDB credentials are environment-only settings.
Absolute paths, prompts, interaction messages, assistant replies, raw tool
observations, provider bodies, and journal locations are excluded from browser
read models and SSE. Logs record bounded identifiers and error types, not
untrusted content.

## Residual operational risks

- Host, PostgreSQL, reverse-proxy, or bearer-token compromise bypasses the
  application boundary; use a secret manager, least privilege, TLS, network
  isolation, rotation, and audit.
- The single `reeloom` role owns its database and can alter its schema. This
  deployment simplicity trades away protection from a fully compromised
  application database credential; immutable triggers still prevent accidental
  history mutation during normal SQL use.
- PostgreSQL, state-root, and media backups are one recovery set. An inconsistent
  restore must stay offline until reconciled from a known common snapshot.
- Provider and TMDB availability can stop planning, but cannot authorize or
  execute filesystem mutation.
- Same-filesystem atomic rename is required. Cross-filesystem copy/delete remains
  unsupported and must fail closed.

Security changes must retain the offline filesystem attack tests and PostgreSQL
concurrency/immutability tests described in `docs/m8-requirements.md`.
