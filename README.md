# Pravda

Pravda is a Python library for durable web evidence capture. It drives a
remote Playwright browser to preserve rendered HTML, plaintext, full-page
screenshots, metadata, and HAR recordings with response bodies. Snapshots are
recorded in a SQL database (PostgreSQL or SQLite) and on any fsspec-compatible
backend for later inspection or comparison.

Pravda is a **library, not a service**: it connects directly from the caller's
process to the browser, database, and storage backend. Applications own that
infrastructure (see [Infrastructure](#infrastructure)).

- **Python** 3.12+
- **Browser**: a remote Playwright Chromium WebSocket endpoint (headed Chrome
  under xvfb). The browser is a client connection; Pravda does not launch one.
- **Database**: PostgreSQL or SQLite, upgraded to Pravda's schema with the
  [migration helper](#database-migrations).
- **Storage**: any fsspec URL (local path, `s3://`, `gs://`, …) for
  content-addressed artifacts.

## Installation

```bash
pip install opensanctions-pravda
```

## Quick start

The application owns the SQLAlchemy engine and an `async_sessionmaker`
configured with `expire_on_commit=False`; construct a
[`PravdaConfig`](#configuration) for the remaining settings and build a
long-lived `Pravda`. Reuse a single instance across captures — each capture
opens its own browser connection and database session — and dispose the engine
on shutdown.

```python
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pravda import Pravda, PravdaConfig

engine = create_async_engine("postgresql+psycopg://pravda:pravda@localhost:5432/pravda")
sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
config = PravdaConfig(
    browser_ws_url="ws://localhost:3000",
    storage_base_path="./data",
)

async def capture_example():
    pravda = Pravda(config, sessionmaker)
    snapshot = await pravda.snapshot("https://example.com")
    print(snapshot.id, snapshot.http_status, snapshot.rendered_html)
```

## Configuration

`PravdaConfig` takes two settings, supplied explicitly per instance, plus an
application-owned `async_sessionmaker` passed to `Pravda`:

- `browser_ws_url` — remote Playwright WebSocket URL.
- `storage_base_path` — fsspec storage URL, such as `./data`, `s3://bucket`,
  or `gs://bucket`.

The session factory must use `expire_on_commit=False`, because Pravda reads
persisted snapshots after commit.

## Usage

### Capture a page

By default, Pravda navigates to the URL, waits for the normal `load` state,
captures the evidence, and persists the result. Capture and HAR finalization
share one wall-clock deadline; persistence is bounded separately:

```python
async def capture_example():
    pravda = Pravda(config, sessionmaker)
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
    pravda = Pravda(config, sessionmaker)
    snapshot = await pravda.snapshot("https://example.com", drive=drive)
```

`page` is a real `playwright.async_api.Page`, so selectors, clicks, form
fills, and other Playwright operations are available. Playwright errors and
timeouts from `drive` are persisted as failed snapshots. A recording-context
close failure also persists a failed snapshot without artifacts because its HAR
could not be finalized. Other callback exceptions propagate and persist nothing.

Chrome is configured to download PDFs instead of opening its viewer. In custom
callbacks, `page.goto()` may therefore raise `Download is starting`; catch
that Playwright error if the download is expected. Pravda recovers the
downloaded body into the HAR.

### Query history

The configured instance returns all snapshots for an exact URL, newest first:

```python
async def print_history():
    pravda = Pravda(config, sessionmaker)
    history = await pravda.snapshots("https://example.com")
    for snapshot in history:
        print(snapshot.captured_at, snapshot.http_status)
```

## Database migrations

Alembic owns the Pravda schema; the migration scripts ship inside the
distribution. Bring a PostgreSQL or SQLite database up to the current schema
head from application startup — the database URL is passed explicitly and **no**
`DATABASE_URL` environment variable is required:

```python
import pravda

async def setup():
    await pravda.migrate("postgresql+psycopg://user:pass@host/db")
```

`migrate()` runs the packaged revisions through Alembic (not
`metadata.create_all`), is safe to call repeatedly (a database already at head
is a no-op), works from inside a running event loop, and disposes the engine
it creates. Database and migration failures propagate. There is no downgrade
or automatic-startup behavior: call `migrate()` where and when you want the
schema applied.

## Storage

Artifacts are content-addressed files organized under the captured URL's
hostname. The public `Snapshot` resolves its artifact fields to full storage
paths: `plaintext`, `rendered_html`, and `screenshot` point at stored files,
and each HAR `response.content._file` resolves to its stored response body.
Persisted database fields and HAR values keep their relative, content-addressed
names; consumers read the resolved paths directly from the shared fsspec
backend. Storage writes are bounded; timeouts and other storage failures
propagate, and no snapshot record is persisted.

## Infrastructure

Applications own the external infrastructure Pravda talks to; Pravda does not
launch or manage it:

- **Browser** — a remote Playwright Chromium server exposed over WebSocket.
  Applications provide their own, such as the Playwright Docker image running
  headed Chrome under xvfb, or a hosted browser service.
- **Database** — a PostgreSQL or SQLite database the application provisions,
  opens an async engine for, and [migrates](#database-migrations). The
  application owns the engine and session factory.
- **Storage** — an fsspec backend the application points at via
  `storage_base_path`.

## Development

Requires [uv](https://docs.astral.sh/uv/) and a remote Playwright browser
endpoint. Tests run against an in-memory SQLite database, so no database
service is needed.

```bash
# Install dependencies
uv sync

# Validate (set PRAVDA_TEST_BROWSER_WS_URL if the browser is not on :3000)
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

`docker compose up -d` starts a local PostgreSQL for developing migrations
against the Postgres dialect.

The migration scripts live inside the package at `pravda/migrations`. After
changing models in `pravda/db.py`, the developer `alembic` command reads
`DATABASE_URL` and points at the packaged scripts via `alembic.ini`:

```bash
DATABASE_URL=postgresql+psycopg://pravda:pravda@localhost:5432/pravda \
  uv run alembic upgrade head
DATABASE_URL=postgresql+psycopg://pravda:pravda@localhost:5432/pravda \
  uv run alembic revision --autogenerate -m "describe the change"
```

## License

MIT — see [LICENSE](LICENSE).
