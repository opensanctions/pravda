# Pravda

Pravda is a Python evidence-capture library. It uses a remote Playwright browser to preserve rendered HTML, plaintext, full-page screenshots, metadata, and HAR recordings with response bodies. Snapshots are stored in Postgres and on any fsspec-compatible backend for later inspection or comparison.

Pravda is a library, not a service: it connects directly from the caller's process to the browser, database, and storage backend.

## Setup

Requires Python 3.13+, [uv](https://docs.astral.sh/uv/), and Docker.

```bash
# Start the remote browser and dev/test Postgres databases
docker compose up -d

# Install dependencies
uv sync

# Apply migrations to the dev database
uv run --env-file .env alembic upgrade head
```

Compose starts headed Chrome plus Postgres on ports `5432` (development) and `5433` (tests). The development schema is not created automatically.

## Configuration

Copy `.env.example` to `.env` and configure:

- `BROWSER_WS_URL` — remote Playwright WebSocket URL
- `DATABASE_URL` — async SQLAlchemy Postgres URL
- `TEST_DATABASE_URL` — Postgres URL used by tests
- `STORAGE_BASE_PATH` — fsspec storage URL, such as `./data`, `s3://bucket`, or `gs://bucket`

## Usage

### Capture a page

By default, Pravda navigates to the URL, waits for the normal `load` state, captures the evidence, and persists the result. The complete pipeline is bounded by a wall-clock timeout:

```python
import pravda

snapshot = await pravda.snapshot("https://example.com")
print(snapshot.id, snapshot.http_status, snapshot.rendered_html)
```

For custom navigation or interaction, pass an async `drive(page, url)` callback. The callback owns the initial navigation; Pravda owns capture, persistence, and cleanup, and records the state it leaves behind.

```python
async def drive(page, url):
    await page.goto(url, wait_until="commit")
    await page.wait_for_selector(".results")

snapshot = await pravda.snapshot("https://example.com", drive=drive)
```

`page` is a real `playwright.async_api.Page`, so selectors, clicks, form fills, and other Playwright operations are available. Playwright errors and timeouts from `drive` are persisted as failed snapshots. Other callback exceptions propagate and persist nothing.

Chrome is configured to download PDFs instead of opening its viewer. In custom callbacks, `page.goto()` may therefore raise `Download is starting`; catch that Playwright error if the download is expected. Pravda recovers the downloaded body into the HAR.

### Query history

`pravda.snapshots()` returns all snapshots for an exact URL, newest first:

```python
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
uv run --env-file .env alembic revision --autogenerate -m "describe the change"
```
