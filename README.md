# Pravda

Pravda is a Python library for durable web evidence capture. It drives a
remote Playwright browser to preserve rendered HTML, plaintext, full-page
screenshots, metadata, and HAR recordings with response bodies. Snapshots are
recorded in a SQL database (PostgreSQL or SQLite) and on any fsspec-compatible
backend for later inspection or comparison.

Applications own the browser, database, and storage backend (see
[Infrastructure](#infrastructure)).

- **Python** 3.12+
- **Browser**: a remote Playwright Chromium WebSocket endpoint (headed Chrome
  under xvfb).
- **Database**: PostgreSQL or SQLite.
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

`page` is a real `playwright.async_api.Page`. Playwright errors and timeouts
from `drive` are persisted as failed snapshots. A recording-context
close failure also persists a failed snapshot without artifacts because its HAR
could not be finalized. Other callback exceptions propagate and persist nothing.

Chrome is configured to download PDFs instead of opening its viewer. In custom
callbacks, `page.goto()` may therefore raise `Download is starting`; catch
that Playwright error if the download is expected. Pravda recovers the
downloaded body into the HAR.

### Query history

`snapshots(url)` returns all snapshots for an exact URL, newest first:

```python
async def print_history():
    pravda = Pravda(config, sessionmaker)
    history = await pravda.snapshots("https://example.com")
    for snapshot in history:
        print(snapshot.captured_at, snapshot.http_status)
```

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

Applications own the external infrastructure Pravda talks to:

- **Browser** — a remote Playwright Chromium server exposed over WebSocket,
  such as the Playwright Docker image running headed Chrome under xvfb, or a
  hosted browser service.
- **Database** — a PostgreSQL or SQLite database, accessed through an async
  SQLAlchemy engine.
- **Storage** — an fsspec backend, selected via `storage_base_path`.

## Development

Requires [uv](https://docs.astral.sh/uv/) and a remote Playwright browser
endpoint. Tests run against an in-memory SQLite database, so no database
service is needed. Environment variables live in `.env`; `uv` does not read
it automatically:

```bash
# Install dependencies
uv sync

# Local environment (once)
cp .env.example .env

# Validate
uv run --env-file .env pytest
uv run ruff check .
uv run ruff format --check .
```

## License

MIT — see [LICENSE](LICENSE).
