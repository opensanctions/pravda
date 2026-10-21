# Pravda

Pravda is a Python library for durable web evidence capture. It drives a
remote Playwright browser to preserve rendered HTML, full-page
screenshots, standalone HTML, metadata, and HAR recordings with response
bodies. Captures are returned as immutable `Snapshot` values, and
artifacts are written to any fsspec-compatible backend for later
inspection or comparison. Persisting snapshots to a database is the
application's policy.

Applications own the browser and storage backend.

- **Browser**: a remote Playwright Chromium WebSocket endpoint (e.g. headed
  Chrome under xvfb).
- **Storage**: any fsspec URL (local path, `s3://`, `gs://`, …) for
  content-addressed artifacts.

## Installation

**Python** 3.12+

```bash
pip install opensanctions-pravda
```

## Quick start

Construct a [`PravdaConfig`](#configuration) and build a long-lived
`Pravda`. Reuse a single instance across captures — each capture opens its
own browser connection.

```python
from pravda import Pravda, PravdaConfig

config = PravdaConfig(
    browser_ws_url="ws://localhost:3000",
    storage_base_path="./data",
)


async def capture_example():
    pravda = Pravda(config)
    snapshot = await pravda.capture("https://example.com")
    print(snapshot.id, snapshot.http_status, snapshot.rendered_html)
```

`Pravda.capture` returns a `Snapshot` value with a client-minted `id` and
`captured_at`. Persisting it — with your own session, in your own schema,
in your own transaction — is up to the application.

## Configuration

- `browser_ws_url` — remote Playwright WebSocket URL.
- `storage_base_path` — fsspec storage URL, such as `./data`, `s3://bucket`,
  or `gs://bucket`.
- `timeouts` — a frozen `Timeouts` of wall-clock budgets for the captured
  website, defaulting to sensible values; see its docstring for what each
  budget does on expiry. Override individual fields with
  `dataclasses.replace(Timeouts(), load=60)`.
- `liveness` — a frozen `Liveness` of deadlines for Pravda's own browser
  machinery. `setup` bounds connecting and preparing the recording context;
  `close` bounds closing it, which finalizes the recording. Both are
  seconds, default 30.

## Usage

### Capture a page

By default, Pravda navigates to the URL, waits for the normal `load`
state, captures the evidence, and returns the snapshot. The target-facing
capture is bounded by one wall-clock budget; a wedged-but-alive storage
backend hangs until the caller cancels. Browser machinery — connecting,
preparing, or closing the recording context — is Pravda's infrastructure,
not the target: its failures raise `InfrastructureError` and capture
nothing, with a wedged-but-alive endpoint failing at the `liveness`
deadlines.

For custom navigation or interaction, pass an async `drive(page, url)`
callback. The callback owns the initial navigation; Pravda owns capture
and cleanup, and records the state it leaves behind.

```python
async def drive(page, url):
    await page.goto(url, wait_until="commit")
    await page.wait_for_selector(".results")


async def capture_results():
    pravda = Pravda(config)
    snapshot = await pravda.capture("https://example.com", drive=drive)
```

`page` is a real `playwright.async_api.Page`. Playwright errors and timeouts
from `drive` are returned as failed snapshots with `error` set. Other
callback exceptions propagate and capture nothing.

When the browser endpoint sends PDFs as downloads, `page.goto()` in custom
callbacks may raise `Download is starting`; catch that Playwright error if
the download is expected. Pravda recovers the downloaded body into the HAR.

## Storage

Artifacts are content-addressed files organized under the captured page's
hostname. The `Snapshot` keeps storage-relative artifact names. Read
artifacts back by name with the page's final URL:
`storage.read_artifact(name, snapshot.final_url)` works for `rendered_html`, `screenshot`, `standalone_html`, and each HAR
`response.content._file` entry.
`standalone_html` is a self-contained rendering of the page —
stylesheets inlined, images embedded as data URIs, active content sanitized —
derived from the rendered DOM and the recorded HAR bodies for direct
inspection; derivation failures drop only that artifact. Other storage
failures propagate, and capture returns nothing.

## Development

Requires [uv](https://docs.astral.sh/uv/) and a remote Playwright browser
endpoint. Environment variables live in `.env`; `uv` does not read
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
