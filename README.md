# Pravda

Pravda is a Python evidence-capture library. It uses a remote Playwright browser to preserve rendered HTML, plaintext, full-page screenshots, metadata, and HAR recordings with response bodies. Snapshots are stored in Postgres and on any fsspec-compatible backend for later inspection or comparison.

Pravda is a library, not a service: it connects directly from the caller's process to the browser, database, and storage backend.

## Setup

Requires Python 3.13+, [uv](https://docs.astral.sh/uv/), and Docker.

```bash
# Start the remote browser and Postgres
docker compose up -d

# Install dependencies
uv sync
```

Compose starts headed Chrome on port `3000` and Postgres on port `5432`. The
test suite manages the database schema and clears it between runs.

## Configuration

`PravdaConfig` takes three settings:

- `browser_ws_url` — remote Playwright WebSocket URL
- `database_url` — async SQLAlchemy Postgres URL
- `storage_base_path` — fsspec storage URL, such as `./data`, `s3://bucket`, or `gs://bucket`

Applications supply these values when constructing a `Pravda` instance.

## Usage

Build a `PravdaConfig`, construct a long-lived `Pravda`, and use it as an
async context manager so its database engine is disposed on teardown. Reuse a
single instance across captures — it owns the pooled engine, session factory,
and storage backend, while each capture opens its own browser connection.

### Capture a page

By default, Pravda navigates to the URL, waits for the normal `load` state,
captures the evidence, and persists the result. The complete pipeline is
bounded by a wall-clock timeout:

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

## Storage

Artifacts are content-addressed files under `Snapshot.prefix`. The `plaintext`, `rendered_html`, and `screenshot` fields contain filenames; HAR `response.content._file` fields refer to stored response bodies. Consumers read these files directly from the shared fsspec backend.

## Development

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

After changing models in `pravda/db.py`, generate and review a migration:

```bash
DATABASE_URL=postgresql+asyncpg://pravda:pravda@localhost:5432/pravda \
  uv run alembic upgrade head
DATABASE_URL=postgresql+asyncpg://pravda:pravda@localhost:5432/pravda \
  uv run alembic revision --autogenerate -m "describe the change"
```
