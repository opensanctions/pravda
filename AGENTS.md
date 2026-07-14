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
- Runtime configuration is explicit and instance-scoped. Applications construct `PravdaConfig(database_url, browser_ws_url, storage_base_path)` and pass it to a long-lived `Pravda` instance, which owns its engine, session factory, and storage.
- The Alembic migration scripts live inside the package at `pravda/migrations` so they ship with installed distributions. The public `pravda.migrate(database_url)` API upgrades a caller-supplied database URL to head without touching the environment; the developer `alembic` command still reads `DATABASE_URL` from its command environment.
- Add dependencies with `uv add`; do not edit `pyproject.toml` manually.
- Do not create git commits; the user manages version control.

## Public behavior

The public API is exported from `pravda`: the configured `Pravda` instance, the `PravdaConfig` it takes, and the frozen `Snapshot` value. `Pravda` is used as an async context manager and exposes async `snapshot()` and `snapshots()` methods.

- Without `drive`, `snapshot()` owns navigation and the complete capture pipeline.
- The complete pipeline must remain bounded by a wall-clock timeout.
- With `drive(page, url)`, the callback owns initial navigation and interaction; Pravda still owns capture, persistence, and cleanup.
- Browser, navigation, and Playwright callback failures are persisted as failed snapshots. Non-Playwright callback exceptions propagate and persist nothing.
- `Pravda` owns its database sessions and commits capture attempts. Callers require no database wiring.
- `snapshots(url)` returns all exact-URL matches newest first, without pagination.
- Concurrent `snapshot()` calls are safe: each opens its own browser connection, recording context, temporary directory, and database session.

## Downloads

Chrome is configured with `AlwaysOpenPdfExternally`, so PDFs and similar viewer-handled responses become downloads. Capture code must continue recovering download bytes and associating them with the matching HAR entry as `content._file`; do not introduce a separate PDF artifact model.

## Database migrations

The scripts live inside the package at `pravda/migrations`; the repository-root `alembic.ini` points the developer command at them. After changing `pravda/db.py`, generate and review a migration:

```bash
DATABASE_URL=postgresql+asyncpg://pravda:pravda@localhost:5432/pravda \
  uv run alembic upgrade head
DATABASE_URL=postgresql+asyncpg://pravda:pravda@localhost:5432/pravda \
  uv run alembic revision --autogenerate -m "describe the change"
```

The public `pravda.migrate(database_url)` API runs the same packaged revisions against an explicit URL (no `DATABASE_URL` required); tests for that API drop and re-create the schema through their own fixture. Other tests use `Base.metadata.create_all` rather than Alembic migrations.

When a migration creates a `postgresql.ENUM`, manage the type explicitly in both `upgrade` and `downgrade`.

## Testing

- Run against the Compose browser and test Postgres; do not use the public internet.
- Use Playwright `page.route()` and files in `tests/fixtures/` for web content.
- Use the real test database and configured client fixtures in `tests/conftest.py`.
- Mock boundaries only, such as temporary storage and browser routing; do not mock Pravda internals.
- Test public behavior rather than implementation details.
- Keep the test suite small and meaningful.

## Validation

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```
