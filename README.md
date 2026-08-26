# Reeloom

Reeloom watches a folder, works out what the media in it is, and files it into
your library under a consistent name — anime, TV series and movies, with their
Chinese subtitles.

A model handles the one genuinely uncertain question: *which file is which
episode*. Everything else — paths, names, collisions, moves — is ordinary
deterministic code.

```text
watch root                          media library
  [Group] Show S01/                   Show (2024) {tmdb-123}/
    [Group] Show - 01 [1080p].mkv       S01/
    [Group] Show - 01 [CHS].ass           Show S01E01.mkv
    Show.torrent                          Show S01E01.chs.ass
```

## How it works

1. **Discover.** Each direct child folder of a watch root is one job. A folder
   is picked up once its shape has stopped changing for a configured window
   (120s by default) — CloudDrive-style offline downloads materialize files in
   batches, and starting early would only see half of them.
2. **Identify.** The Agent reads the file list, searches TMDB, checks the
   season's episode numbering, and submits a mapping of candidate IDs to
   episodes.
3. **Execute.** Automatically, straight away. Files are renamed into the
   library; anything left over goes to the watch root's `archive` bucket.
4. **Subtitles.** For anime watches with the option on, episodes still missing
   a Chinese subtitle get one from ACG.RIP.
5. **Notify.** One Telegram message per finished job.

### Magnet downloads (optional)

With a CloudDrive2 address and API token configured, the 下载 page submits
magnets as CloudDrive offline tasks and tracks them by info hash. Tasks
download into `<dir>/in_progress` — a reserved name the scanner never picks
up — and a finished item is moved cloud-side into `<dir>` itself. Point the
download dir at a watch root's underlying cloud folder and the mounted side
sees the folder appear, settles, and archives it through the normal flow: an
incomplete download can never be archived. Failed, stalled (no progress past
a configurable timeout, 24h by default) and lost tasks raise a Telegram alert
and wait for you; the page offers per-task retry and delete (the delete also
removes the task and its data at CloudDrive).

Nothing is ever deleted, and an existing file is never overwritten: a
duplicate goes to the `fail` bucket and the copy already in your library
stays. If something looks wrong afterwards, tell the Agent what to fix and hit
**修订并重做** — Reeloom puts every file it moved back where it found it, then
applies the new plan.

## Safety model

- The model never sees or supplies a filesystem path. It submits candidate IDs
  and episode numbers; destinations are computed from the TMDB entry.
- Title and year come from TMDB, not from model output.
- Renames use `RENAME_NOREPLACE` / `RENAME_EXCL`, so an existing destination
  makes the move fail rather than clobber.
- Execution is forward-only and idempotent. Re-running a plan is a no-op, which
  is how a crashed job finishes: it just runs again.
- Nothing is deleted. Unmapped files go to `archive`, duplicates and discarded
  jobs go to `fail`, and empty directories are removed with `rmdir` only.
- The scanner never follows symlinks; `archive`, `fail`, `in_progress`,
  hidden entries and loose root files are skipped, and a folder containing a
  `.env*` file is refused outright.
- Outbound network access is limited to TMDB, your model provider, ACG.RIP,
  Telegram and your own CloudDrive2 server (gRPC, address configured in the
  settings page). No shell, no arbitrary URLs, no inbound webhooks.
- The one cloud-side deletion — dropping an offline task with its data — only
  happens when you press 删除 on the downloads page, never on the archive
  path.
- Inbound and library roots must be on one filesystem — moves are renames.

## Quick start

```bash
export REELOOM_POSTGRES_PASSWORD="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
export REELOOM_ADMIN_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
export REELOOM_MEDIA_ROOT=/absolute/path/to/media

docker compose up --build -d
```

Open <http://127.0.0.1:8080/>, sign in with `REELOOM_ADMIN_TOKEN`, then on the
settings page:

1. Add your TMDB key, model Base URL / key / name, and optionally a Telegram
   bot token and chat ID.
2. Add a watch: an inbound folder, a library folder, and a media type.

Until the credentials are in place, discovered folders simply wait — they are
not failed, and they start moving on their own once you save.

### Environment

Only deployment facts live in the environment; everything operational is
edited in the UI and stored in PostgreSQL.

| Variable | Required | Default |
| --- | --- | --- |
| `REELOOM_DATABASE_URL` | yes | — |
| `REELOOM_ADMIN_TOKEN` | yes, ≥16 chars | — |
| `REELOOM_WORK_DIR` | no | `/var/lib/reeloom` |
| `REELOOM_LISTEN_HOST` / `REELOOM_LISTEN_PORT` | no | `0.0.0.0` / `8080` |
| `REELOOM_SCAN_INTERVAL_SECONDS` | no | `30` |
| `REELOOM_PUBLIC_URL` | no | — |

`REELOOM_PUBLIC_URL` is the URL a browser uses to reach the web UI (e.g.
`https://reeloom.example.com`). When set, the title in a Telegram notification
links to that run's page; when unset, it links to the work's TMDB page.

The listen pair used to be `REELOOM_HOST` / `REELOOM_PORT`; those names are no
longer read. Kubernetes and Docker inject `<SERVICE>_PORT` and friends into
every container, so a Service named `reeloom` used to overwrite the listen port
with `tcp://10.43.0.1:80` and crash the process on startup. If you set the old
names, rename them.

Run exactly one process. There is no clustering, and the run's `state` column
is the only coordination mechanism.

## Development

```bash
uv sync
uv run pytest -q -m "not postgres"

cd web && npm ci && npm run lint && npm run typecheck && npm test && npm run build
```

The repository tests marked `postgres` run in CI against a service container.
To run them locally, point them at a throwaway database:

```bash
createdb reeloom_test
REELOOM_TEST_POSTGRES_DSN=postgresql:///reeloom_test uv run pytest -q -m postgres
```

Tests are offline: the model, TMDB and ACG.RIP are all substituted. To try the
real thing against a folder without moving anything:

```bash
python scripts/live_smoke.py --live --folder "/media/inbound/[Group] Show"
```

Layout: `scanner`/`library` read the filesystem, `naming`/`planner` decide
destinations, `agent/` runs the model, `executor`+`rename` do the moving,
`server/` is the API and the worker. See
[docs/rebuild-plan.md](docs/rebuild-plan.md) for why it is shaped this way.
