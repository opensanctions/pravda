# Pravda

Pravda is an async Python library for capturing durable web evidence with a remote browser, Postgres, and content-addressed blob storage.

## Principles

- This is an early-stage project: do not preserve compatibility or add fallback behavior without a current need.
- Prefer direct code over abstractions made for hypothetical reuse.
- Pravda is a library, not a service. Do not add an HTTP API or application server.

## Architecture

- Use Python 3.13+ and async APIs only; do not add sync wrappers.
- The Playwright package is a client. Browsers run only in the Docker container as headed Chrome under xvfb.
- Browser launch options are sent through the `x-playwright-launch-options` WebSocket header; do not add custom server JavaScript.
- Postgres access is async SQLAlchemy. Alembic owns the schema; library code must not create it.
- Store artifacts through fsspec using content-addressed filenames.
- Read `BROWSER_WS_URL`, `DATABASE_URL`, and `STORAGE_BASE_PATH` from the environment in the module that uses them; do not inject configuration through constructors.
- Add dependencies with `uv add`; do not edit `pyproject.toml` manually.
- Do not create git commits; the user manages version control.

## Public behavior

The public API is exported from `pravda`: `snapshot`, `snapshots`, and the frozen `Snapshot` value.

- Without `drive`, `snapshot()` owns navigation and the complete capture pipeline.
- The complete pipeline must remain bounded by a wall-clock timeout.
- With `drive(page, url)`, the callback owns initial navigation and interaction; Pravda still owns capture, persistence, and cleanup.
- Browser, navigation, and Playwright callback failures are persisted as failed snapshots. Non-Playwright callback exceptions propagate and persist nothing.
- Pravda owns its database sessions and commits capture attempts. Callers require no database wiring.
- `snapshots(url)` returns all exact-URL matches newest first, without pagination.

## Downloads

Chrome is configured with `AlwaysOpenPdfExternally`, so PDFs and similar viewer-handled responses become downloads. Capture code must continue recovering download bytes and associating them with the matching HAR entry as `content._file`; do not introduce a separate PDF artifact model.

## Database migrations

After changing `pravda/db.py`, generate and review a migration:

```bash
uv run --env-file .env alembic revision --autogenerate -m "describe the change"
```

When a migration creates a `postgresql.ENUM`, manage the type explicitly in both `upgrade` and `downgrade`. Tests use `Base.metadata.create_all` rather than Alembic migrations.

## Testing

- Run against the Compose browser and test Postgres; do not use the public internet.
- Use Playwright `page.route()` and files in `tests/fixtures/` for web content.
- Use the real database with the transaction-isolation fixtures in `tests/conftest.py`.
- Mock boundaries only, such as temporary storage and browser routing; do not mock Pravda internals.
- Test public behavior rather than implementation details.
- Keep the test suite small and meaningful.

## Validation

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```
