# Pravda

Pravda is a Python library for durable web evidence capture. It drives a
remote Playwright browser to preserve rendered HTML, plaintext, full-page
screenshots, metadata, and HAR recordings with response bodies. Snapshots are
recorded in Postgres and on any fsspec-compatible backend for later inspection
or comparison.

Pravda is a **library, not a service**: it connects directly from the caller's
process to the browser, database, and storage backend. Applications own that
infrastructure (see [Infrastructure](#infrastructure)).

- **Python** 3.13+
- **Browser**: a remote Playwright Chromium WebSocket endpoint (headed Chrome
  under xvfb). The browser is a client connection; Pravda does not launch one.
- **Database**: PostgreSQL, upgraded to Pravda's schema with the
  [migration helper](#database-migrations).
- **Storage**: any fsspec URL (local path, `s3://`, `gs://`, …) for
  content-addressed artifacts.

## Installation

```bash
pip install opensanctions-pravda
```

## Quick start

Construct a [`PravdaConfig`](#configuration), build a long-lived `Pravda`, and
use it as an async context manager so its database engine is disposed on
teardown. Reuse a single instance across captures — it owns the pooled engine,
session factory, and storage backend, while each capture opens its own browser
connection.

```python
from pravda import Pravda, PravdaConfig

config = PravdaConfig(
    database_url="postgresql+asyncpg://pravda:pravda@localhost:5432/pravda",
    browser_ws_url="ws://localhost:3000",
    storage_base_path="./data",
)

async def capture_example():
    async with Pravda(config) as pravda:
        snapshot = await pravda.snapshot("https://example.com")
        print(snapshot.id, snapshot.http_status, snapshot.rendered_html)
```

## Configuration

`PravdaConfig` takes three settings, supplied explicitly per instance:

- `database_url` — async SQLAlchemy Postgres URL
  (`postgresql+asyncpg://user:pass@host/db`).
- `browser_ws_url` — remote Playwright WebSocket URL.
- `storage_base_path` — fsspec storage URL, such as `./data`, `s3://bucket`,
  or `gs://bucket`.

## Usage

### Capture a page

By default, Pravda navigates to the URL, waits for the normal `load` state,
captures the evidence, and persists the result. Every phase of the complete
pipeline has an explicit wall-clock deadline:

```python
async def capture_example():
    async with Pravda(config) as pravda:
        snapshot = await pravda.snapshot("https://example.com")
```

For custom navigation or interaction, pass an async `drive(page, url)`
callback. The callback owns the initial navigation; Pravda owns capture,
persistence, and cleanup, and records the state it leaves behind.

```python
async def drive(page, url):
    await page.goto(url, wait_until="commit")
    await page.wait_for_selector(".results")

async def capture_results():
    async with Pravda(config) as pravda:
        snapshot = await pravda.snapshot("https://example.com", drive=drive)
```

`page` is a real `playwright.async_api.Page`, so selectors, clicks, form
fills, and other Playwright operations are available. Playwright errors and
timeouts from `drive` are persisted as failed snapshots. Other callback
exceptions propagate and persist nothing.

Chrome is configured to download PDFs instead of opening its viewer. In custom
callbacks, `page.goto()` may therefore raise `Download is starting`; catch
that Playwright error if the download is expected. Pravda recovers the
downloaded body into the HAR.

### Query history

The configured instance returns all snapshots for an exact URL, newest first:

```python
async def print_history():
    async with Pravda(config) as pravda:
        history = await pravda.snapshots("https://example.com")
        for snapshot in history:
            print(snapshot.captured_at, snapshot.http_status)
```

## Database migrations

Alembic owns the Pravda schema; the migration scripts ship inside the
distribution. Bring a database up to the current schema head from application
startup — the database URL is passed explicitly and **no** `DATABASE_URL`
environment variable is required:

```python
import pravda

async def setup():
    await pravda.migrate("postgresql+asyncpg://user:pass@host/db")
```

`migrate()` runs the packaged revisions through Alembic (not
`metadata.create_all`), is safe to call repeatedly (a database already at head
is a no-op), works from inside a running event loop, and disposes the engine
it creates. Database and migration failures propagate. There is no downgrade
or automatic-startup behavior: call `migrate()` where and when you want the
schema applied.

## Storage

Artifacts are content-addressed files under `Snapshot.prefix`. The `plaintext`,
`rendered_html`, and `screenshot` fields contain filenames; HAR
`response.content._file` fields refer to stored response bodies. Consumers
read these files directly from the shared fsspec backend.

## Infrastructure

Applications own the external infrastructure Pravda talks to; Pravda does not
launch or manage it:

- **Browser** — a remote Playwright Chromium server exposed over WebSocket.
  Release images are published to GitHub Container Registry as
  `ghcr.io/opensanctions/pravda-browser:<version>` (and `latest` for the newest
  non-prerelease). The image runs headed Chrome under xvfb and accepts launch
  options through the `x-playwright-launch-options` WebSocket header. Run it
  locally with:

  ```bash
  docker run --rm --init -p 3000:3000 \
    ghcr.io/opensanctions/pravda-browser:latest
  ```
- **Postgres** — a database the application provisions and
  [migrates](#database-migrations).
- **Storage** — an fsspec backend the application points at via
  `storage_base_path`.

## Development

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```bash
# Start the remote browser and Postgres
docker compose up -d

# Install dependencies
uv sync

# Validate
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

The migration scripts live inside the package at `pravda/migrations`. After
changing models in `pravda/db.py`, the developer `alembic` command reads
`DATABASE_URL` and points at the packaged scripts via `alembic.ini`:

```bash
DATABASE_URL=postgresql+asyncpg://pravda:pravda@localhost:5432/pravda \
  uv run alembic upgrade head
DATABASE_URL=postgresql+asyncpg://pravda:pravda@localhost:5432/pravda \
  uv run alembic revision --autogenerate -m "describe the change"
```

## License

MIT — see [LICENSE](LICENSE).
