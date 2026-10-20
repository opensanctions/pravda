# Pravda

Pravda is an async Python library for capturing durable web evidence with a remote browser, a SQL database, and content-addressed blob storage.

## Principles

- This is an early-stage project: do not preserve compatibility or add fallback behavior without a current need.
- Prefer direct code over abstractions made for hypothetical reuse.
- Pravda is a library, not a service. Do not add an HTTP API or application server.

## Architecture

- Use async APIs only; do not add sync wrappers.
- The Playwright package is a client. Pravda connects to an externally provisioned browser over WebSocket and never launches one; the endpoint owns its launch configuration.
- Store artifacts through fsspec using content-addressed filenames.
- Runtime configuration is explicit and instance-scoped (`PravdaConfig`); its construction is documented in the `pravda` package docstring.
- The schema in `src/pravda/db.py` is `pravda.db.Base` metadata. Consumers own the database and its migration ledger, generating their own migrations; library code must not create the schema. Schema changes are breaking for consumers.
- Add dependencies with `uv add`; do not edit `pyproject.toml` manually.

## Public behavior

`Pravda` is a long-lived object that exposes an async `snapshot()` method.

- Without `drive`, `snapshot()` owns navigation and the complete capture pipeline.
- The target-facing capture must remain bounded by one wall-clock timeout.
  Browser setup and context close carry liveness deadlines from
  `PravdaConfig.liveness`; their expiry is infrastructure failure, not
  evidence about the target: it raises and persists nothing. Storage and
  database carry no budgets: dead infrastructure fails on its own errors,
  and a wedged-but-alive dependency hangs until the caller cancels.
- With `drive(page, url)`, the callback owns initial navigation and interaction; Pravda still owns capture, persistence, and cleanup.
- Navigation, capture, and Playwright callback failures are persisted as failed snapshots; `Pravda` commits each capture attempt through the application-owned session factory. Browser connection setup and context close are Pravda's own infrastructure: their failures raise `InfrastructureError` and persist nothing, like storage and database failures. Non-Playwright callback exceptions propagate and persist nothing.
- Concurrent `snapshot()` calls are safe: each opens its own browser connection, recording context, temporary directory, and database session.

## Downloads

When the browser endpoint sends viewer-handled responses such as PDFs as downloads (`AlwaysOpenPdfExternally`), capture code must recover the download bytes and associate them with the matching HAR entry as `content._file`; do not introduce a separate PDF artifact model.

## Testing

- Run against the configured browser endpoint; do not use the public internet.
- Use Playwright `page.route()` and files in `tests/fixtures/` for web content.
- Use the in-memory SQLite database (schema created via `Base.metadata.create_all` in `tests/conftest.py`) and the configured client fixtures.
- Use mocks to inject failure or delay in a dependency, never to fake working behavior.
- Test public behavior rather than implementation details.
- Keep the test suite small and meaningful.

## Validation

```bash
uv run --env-file .env pytest
uv run ruff check .
uv run ruff format --check .
```
