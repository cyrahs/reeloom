# Reeloom

Reeloom is an agent-native media organizer for anime, TV series, and movies.
It identifies media with TMDB, builds an immutable rename plan, presents every
move for review in a same-origin web UI, and executes only an exact approved
plan.

The model helps with uncertain semantic decisions. Deterministic code owns
paths, naming, collision checks, approvals, file operations, rollback, and
recovery.

## What it does

- Treats each stable direct child folder of a configured watch root as one
  independent intake run.
- Identifies anime, TV series, and single-feature movies through TMDB.
- Associates external Chinese subtitles and classifies them as Simplified
  (`chs`), Traditional (`cht`), or unknown Chinese (`chi`).
- Shows immutable plan lineage and relative source/destination previews.
- Supports questions, plan revisions, completed-layout reapply, manual approval,
  deterministic automatic policy, rollback, and crash recovery.
- Stores control-plane history in PostgreSQL and serves the React UI from the
  same application image.
- Moves successful residual content to the watch root's managed `archive`
  bucket and eligible deterministic failures to `fail`.

Typical output:

```text
<archive>/
  Series Name (2024) {tmdb-123}/
    S01/
      Series Name S01E01.mkv
      Series Name S01E01.chs.srt

  Movie Name (2024) {tmdb-456}/
    Movie Name (2024).mkv
    Movie Name (2024).cht.ass
```

Movie v1 organizes one main video and zero or more Chinese subtitles. Extra
videos, trailers, alternate cuts, and multipart movies remain unmapped.

## Safety model

Reeloom is intentionally fail-closed:

- The default workflow is review-first; the Agent cannot approve or execute a
  plan.
- The Agent never receives arbitrary filesystem, shell, or URL capabilities.
- All executable moves are derived from opaque candidate IDs by the
  deterministic kernel.
- Plans are canonical, immutable, content-addressed, and bound to source file
  identity and authorized roots.
- Approval is bound to the exact run and plan hash and can be claimed only once.
- Execution never deletes media or overwrites an existing destination.
- `archive`, `fail`, hidden top-level folders, loose root files, and symlinks
  are excluded from discovery; any `.env*` entry blocks its intake folder.
- Sources, roots, symlinks, collisions, and plan integrity are revalidated
  immediately before execution.
- Journaled rollback and durable recovery do not depend on an LLM.
- Watch and archive paths used by one transaction must be on the same
  filesystem.

See the [threat model](docs/threat-model.md) and
[deployment guide](docs/deployment.md) before enabling file operations.

## Web workflow

1. Sign in with the deployment-provided Admin Bearer token.
2. Configure inbound watch roots, media-library routes, TMDB-backed media
   types, and the model provider.
3. Keep the first configuration in `plan_only`.
4. Review discoveries, runs, immutable plans, unmapped files, and interaction
   history.
5. Revise the plan when needed, then approve the exact plan hash.
6. Read the durable terminal settlement; do not infer success from a network
   response or SSE disconnect.

Drop each title into its own direct child folder under a watch root. Reeloom
creates and ignores the watch-local `archive` and `fail` buckets automatically.

The browser stores a successfully validated Admin token in `localStorage`.
Deploy only behind a trusted origin with the supplied CSP and response headers
intact.

## Quick start with Docker Compose

Requirements:

- Docker with Compose support.
- An absolute media root that can be mounted at the same absolute path.
- A TMDB API key.
- An OpenAI-compatible provider configured later through the Admin UI.

Set credentials in the process environment; Reeloom itself does not load
dotenv files:

```bash
export REELOOM_POSTGRES_PASSWORD="$(
  python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
)"
export REELOOM_ADMIN_TOKEN="$(
  python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
)"
export REELOOM_TMDB_API_KEY="replace-with-your-tmdb-key"
export REELOOM_MEDIA_ROOT="/absolute/path/to/media"
export REELOOM_ALLOWED_HOSTS="127.0.0.1,localhost"
export REELOOM_ALLOWED_UI_ORIGINS="http://127.0.0.1:8080"

docker compose up --build -d
```

Open <http://127.0.0.1:8080/> and sign in with `REELOOM_ADMIN_TOKEN`. The
included composition uses PostgreSQL 17; Reeloom supports PostgreSQL 16, 17,
and 18.

## Production deployment

The published image is available as `ghcr.io/cyrahs/reeloom`. Pin a successful
build by digest instead of deploying a mutable tag.

Production requirements:

- PostgreSQL 16, 17, or 18 with a `reeloom` login role that owns the
  `reeloom` database.
- Exactly one Reeloom process and one worker.
- A persistent state root for secrets, plans, and executor journals.
- Explicit media mounts and HTTPS-only provider Base URLs.
- Exact Host and UI Origin allowlists.
- A 16–4096 character base64url Admin token.
- A reverse proxy that preserves Authorization and SSE reconnect headers,
  disables response buffering for streams, and never logs Bearer credentials.
- Coordinated backups of PostgreSQL, the Reeloom state root, and media storage.

Environment contracts, database privileges, reverse-proxy requirements, and
backup/recovery behavior are documented in the
[deployment guide](docs/deployment.md). The stable HTTP contract is described
in [API documentation](docs/api.md) and
[OpenAPI](docs/openapi-v1.json).

## Development

Python 3.11+ and Node.js 24 are required. Tests are offline by default; model
and TMDB behavior use scripted adapters.

```bash
.venv/bin/python -m pytest -q -m "not postgres"
export REELOOM_TEST_POSTGRES_DSN="postgresql://user:password@127.0.0.1:5432/postgres"
.venv/bin/python scripts/run_postgres_tests.py

cd web
npm ci
npm run lint
npm run typecheck
npm test
npm run build
npm run e2e
```

Replace the example test DSN with a dedicated local database before running the
PostgreSQL and browser journeys. Live model and TMDB smoke tests are opt-in and
are documented in the
[internal development status](docs/internal-development.md).

Architecture decisions, milestone evidence, and implementation history are kept
under [`docs/`](docs/), beginning with the
[initial plan](docs/initial-plan.md).
