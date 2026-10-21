# Pravda

Pravda is an async Python library for capturing durable web evidence with a remote browser and content-addressed blob storage.

## Principles

- This is an early-stage project: do not preserve compatibility or add fallback behavior without a current need.
- Prefer direct code over abstractions made for hypothetical reuse.

## Architecture

- Use async APIs only; do not add sync wrappers.
- The Playwright package is a client. Pravda connects to an externally provisioned browser over WebSocket and never launches one; the endpoint owns its launch configuration.
- Store artifacts through fsspec using content-addressed filenames.
- Runtime configuration is explicit and instance-scoped: constructor parameters on `Pravda` and `Storage.from_url`, documented in the `pravda` package docstring. Storage is injected — the caller's handle is shared by capture and reads.
- Pravda has no database: `capture()` returns `Snapshot` values with client-minted `id` and `captured_at`, and the application owns any schema for persisting them.
- Add dependencies with `uv add`; do not edit `pyproject.toml` manually.

## Public behavior

`Pravda` is a long-lived object that exposes an async `capture()` method.
Standalone HTML — a self-contained page rendering — is derived from a
snapshot's stored evidence by the async method
`snapshot.derive_standalone_html(storage)`.

- Without `drive`, `capture()` owns navigation and the complete capture pipeline.
- One capture attempt is bounded by one wall-clock timeout; `Timeouts`
  covers the whole attempt, closing the recording context included.
  Storage carries no budget: dead infrastructure fails on its own
  errors, and a wedged-but-alive backend hangs until the caller cancels.
- With `drive(page, url)`, the callback owns initial navigation and interaction; Pravda still owns capture and cleanup.
- `capture()` raises only when a failure is provably not about the URL:
  storage failures, non-Playwright callback exceptions, and cancellation.
  Everything else, including recording finalization, is returned as a
  failed snapshot (`error` set); persisting them is the caller's policy.
- Concurrent `capture()` calls are safe: each opens its own browser connection, recording context, and temporary directory.

## Downloads

When the browser endpoint sends viewer-handled responses such as PDFs as downloads (`AlwaysOpenPdfExternally`), capture code must recover the download bytes and associate them with the matching HAR entry as `content._file`; do not introduce a separate PDF artifact model.

## Testing

- Run against the configured browser endpoint; do not use the public internet.
- Use Playwright `page.route()` and files in `tests/fixtures/` for web content.
- Use the configured client fixtures; assert returned values and stored artifacts.
- Use mocks to inject failure or delay in a dependency, never to fake working behavior.
- Test public behavior rather than implementation details.
- Keep the test suite small and meaningful.

## Validation

```bash
uv run --env-file .env pytest
uv run ruff check .
uv run ruff format --check .
```
