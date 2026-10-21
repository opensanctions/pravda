# Pravda

Pravda is a Python library for durable web evidence capture. It drives a
remote Playwright browser to capture rendered HTML, full-page screenshots,
and HAR recordings with response bodies, written content-addressed to any
fsspec-compatible backend. Captures return immutable `Snapshot` values;
persisting them is the application's policy.

- **Browser**: a remote Playwright Chromium WebSocket endpoint.
- **Storage**: any fsspec URL (`./data`, `s3://bucket`, `gs://bucket`, …).

## Installation

Python 3.12+:

```bash
pip install opensanctions-pravda
pip install opensanctions-pravda[unmhtml]  # with standalone HTML derivation
```

## Quick start

Build a `Storage` once, then a long-lived `Pravda` over it. Instances are
safe to reuse: concurrent captures each open their own connection, and the
storage handle reads artifacts back later.

```python
from pravda import Pravda, Storage

storage = Storage.from_url("./data")
pravda = Pravda("ws://localhost:3000", storage)

snapshot = await pravda.capture("https://example.com")
print(snapshot.id, snapshot.http_status, snapshot.rendered_html)
```

`capture()` returns a `Snapshot` with client-minted `id` and `captured_at`;
artifact fields are storage-relative paths, read back with
`storage.read_artifact(path)`.

## Configuration

`Pravda(browser_ws_url, storage, *, timeouts)`:

- `timeouts` — wall-clock budgets bounding one capture attempt, closing
  the recording context included. Defaults are sensible; see the
  `Timeouts` docstring.

A browser setup failure raises `InfrastructureError` and captures
nothing — nothing about the URL was attempted. Every other failure,
including closing the recording context, is returned as a failed
snapshot. Storage carries no budget; its failures propagate.

## Usage

### Capture a page

By default Pravda navigates, waits for `load`, and captures. For custom
navigation or interaction, pass an async `drive(page, url)` callback:

```python
async def drive(page, url):
    await page.goto(url, wait_until="commit")
    await page.wait_for_selector(".results")


snapshot = await pravda.capture("https://example.com", drive=drive)
```

The callback owns navigation; Pravda owns capture and cleanup. Playwright
errors from `drive` are returned as failed snapshots with `error` set —
Pravda cannot tell target, browser, and callback failures apart; other
exceptions propagate and capture nothing. Viewer-handled downloads
(e.g. PDFs) surface in `drive` as Playwright's `Download is starting`;
Pravda recovers the body into the HAR.

### Derive standalone HTML

```python
path = await snapshot.derive_standalone_html(storage)
```

Derives a self-contained rendering — stylesheets inlined, images embedded
as data URIs, active content sanitized — from the snapshot's stored
evidence, stores it like any other artifact, and returns its path. Needs
no browser, so it works in any process. Requires the `unmhtml` extra.

## Storage

Artifacts are content-addressed files under the captured page's hostname
(`example.com/<sha1>.html`). The `Snapshot` records each artifact's path:
`rendered_html`, `screenshot`, and every HAR `response.content._file`.

## Development

Requires [uv](https://docs.astral.sh/uv/) and a remote Playwright browser
endpoint. Environment variables live in `.env`, which `uv` does not read
automatically:

```bash
uv sync
cp .env.example .env
uv run --env-file .env pytest
uv run ruff check .
uv run ruff format --check .
```

## License

MIT — see [LICENSE](LICENSE).
