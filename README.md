# Pravda

Pravda is a Python library — the evidence layer that other services build on. It uses Playwright (driving a remote, isolated browser) to capture and store durable, addressable evidence of web pages: rendered HTML, plaintext, full-page screenshots, and a network archive (a HAR recording with response bodies). It turns live web pages into durable evidence that can be inspected, diffed, and reasoned over long after the original page has changed.

Pravda is a library, not a service. It connects from your process to a remote Playwright server and to Postgres and your storage backend directly. There is no HTTP API, no server to run, and no image to ship.

## What it does (v0)

- Captures rendered HTML, plaintext, and full-page screenshots
- Records a HAR with response bodies stored as separate content-addressed blobs
- Recovers bodies Chrome's viewers swallow (e.g. PDFs) by forcing downloads and folding them back into the HAR
- Tracks URLs and their snapshot history
- Stores artifacts on any fsspec filesystem (local, S3, GCS)
- Runs Chrome (not Chromium) in a virtual framebuffer

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```bash
# Start the remote browser and the dev/test Postgres databases
docker compose up -d

# Install the library and its dependencies
uv sync

# Apply database migrations to the dev database
uv run --env-file .env alembic upgrade head
```

`docker-compose.yml` runs three services: the headed `playwright` browser server, `postgres_dev` (`:5432`, used by the library against the dev database), and `postgres_test` (`:5433`, used by the test suite). The schema is **not** created automatically — run `alembic upgrade head` once the dev database is up.

## Environment variables

Configured in `.env` (see `.env.example`):

- `BROWSER_WS_URL` — WebSocket URL of the remote Playwright server (`ws://localhost:3000`).
- `DATABASE_URL` — Postgres URL the library commits snapshots to (`postgresql+asyncpg://...`).
- `TEST_DATABASE_URL` — Postgres URL used by the test suite.
- `STORAGE_BASE_PATH` — fsspec URL for artifact storage (`./data`, `s3://bucket`, `gs://bucket`).

## Usage

Import the public API from `pravda`:

```python
import pravda
```

### One-shot snapshot

Capture a page with the library's default readiness (navigate, then wait for the normal `load` state) and persist it:

```python
snapshot = await pravda.snapshot("https://example.com")
print(snapshot.id, snapshot.http_status, snapshot.rendered_html)
```

### Interactive session

When you need custom readiness — selectors, clicks, form fills, or a specific load state — drive the real Playwright page yourself, then call the terminal `snapshot()`:

```python
async with pravda.browser() as browser:
    page = browser.page
    await page.goto("https://example.com", wait_until="commit")
    await page.wait_for_selector(".results")
    snapshot = await browser.snapshot()
```

`browser.page` is a real `playwright.async_api.Page`; everything Playwright can do is available. `browser.snapshot()` is **terminal**: it captures the current page state, closes the recording context to flush the HAR, and persists the evidence. After it returns the session is terminal — `.page` and further `snapshot()` calls raise `pravda.PravdaError`. Open a new `pravda.browser()` session to capture again.

### History

Look up every snapshot captured for an exact URL, newest first:

```python
history = await pravda.snapshots("https://example.com")
for snapshot in history:
    print(snapshot.captured_at, snapshot.http_status)
```

There is no pagination — every exact-URL match is returned.

## Storage and access

Artifacts live as `<sha1>.<extension>` files under a per-hostname prefix on any [fsspec](https://filesystem-spec.readthedocs.io/) filesystem (`local`, `s3://`, `gs://`), configured via `STORAGE_BASE_PATH`. Each `Snapshot` exposes this `prefix` (storage base + normalized hostname) plus content-addressed filenames (`plaintext`, `rendered_html`, `screenshot`); the `http_archive` is the recorded HAR, and each of its `response.content._file` entries names a body the same way. Downstream services read each artifact directly as `<prefix>/<filename>` from the shared backend. There is no blob download endpoint — Pravda is the evidence capture layer, not a content delivery proxy.

## Database migrations

The schema is managed with [Alembic](https://alembic.sqlalchemy.org/). Apply migrations from the host against the dev database:

```bash
uv run --env-file .env alembic upgrade head
```

After changing the models in `pravda/db.py`, generate a migration and review it:

```bash
uv run --env-file .env alembic revision --autogenerate -m "describe the change"
```

The test suite does not run migrations — `tests/conftest.py` builds the schema with `Base.metadata.create_all` so tests stay independent of the migration history.
