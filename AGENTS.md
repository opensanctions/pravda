# Pravda

Pravda is an async Python library for capturing durable web evidence with a remote browser, a SQL database, and content-addressed blob storage.

## Principles

- This is an early-stage project: do not preserve compatibility or add fallback behavior without a current need.
- Prefer direct code over abstractions made for hypothetical reuse.
- Pravda is a library, not a service. Do not add an HTTP API or application server.

## Architecture

- The project uses uv's `src` layout; package source lives in `src/pravda`.
- Use Python 3.12+ and async APIs only; do not add sync wrappers.
- The Playwright package is a client. Pravda connects to an externally provisioned browser over WebSocket and never launches one; the endpoint owns its launch configuration.
- Database access is async SQLAlchemy (PostgreSQL or SQLite). Alembic owns the schema; library code must not create it.
- Store artifacts through fsspec using content-addressed filenames.
- Runtime configuration is explicit and instance-scoped. Applications construct `PravdaConfig(browser_ws_url, storage_base_path)`, own their SQLAlchemy `AsyncEngine` and an `async_sessionmaker` configured with `expire_on_commit=False`, and pass both to a long-lived `Pravda` instance. The application disposes the engine.
- The Alembic migration scripts live inside the package at `src/pravda/migrations` so they ship with installed distributions. The public `pravda.migrate(database_url)` API upgrades a caller-supplied database URL to head without touching the environment; the developer `alembic` command still reads `DATABASE_URL` from its command environment.
- Add dependencies with `uv add`; do not edit `pyproject.toml` manually.

## Public behavior

The public API is exported from `pravda`: the configured `Pravda` instance, the `PravdaConfig` it takes, and the frozen `Snapshot` value. `Pravda` is a long-lived object that exposes async `snapshot()` and `snapshots()` methods.

- Without `drive`, `snapshot()` owns navigation and the complete capture pipeline.
- The complete pipeline must remain bounded by a wall-clock timeout.
- With `drive(page, url)`, the callback owns initial navigation and interaction; Pravda still owns capture, persistence, and cleanup.
- Browser, navigation, and Playwright callback failures are persisted as failed snapshots. Non-Playwright callback exceptions propagate and persist nothing.
- `Pravda` commits each capture attempt through the application-owned session factory.
- `snapshots(url)` returns all exact-URL matches newest first, without pagination.
- Concurrent `snapshot()` calls are safe: each opens its own browser connection, recording context, temporary directory, and database session.

## Downloads

When the browser endpoint sends viewer-handled responses such as PDFs as downloads (`AlwaysOpenPdfExternally`), capture code must recover the download bytes and associate them with the matching HAR entry as `content._file`; do not introduce a separate PDF artifact model.

## Database migrations

The scripts live inside the package at `src/pravda/migrations`; the repository-root `alembic.ini` points the developer command at them. After changing `src/pravda/db.py`, generate and review a migration:

```bash
uv run --env-file .env alembic upgrade head
uv run --env-file .env alembic revision --autogenerate -m "describe the change"
```

The public `pravda.migrate(database_url)` API runs the same packaged revisions against an explicit URL (no `DATABASE_URL` required). Tests run against an in-memory SQLite database: migration tests hold the database open while `migrate()` opens its own engine, and other tests use `Base.metadata.create_all` rather than Alembic migrations.

When a migration creates a `postgresql.ENUM`, manage the type explicitly in both `upgrade` and `downgrade`.

## Testing

- Run against the configured browser endpoint; do not use the public internet.
- Use Playwright `page.route()` and files in `tests/fixtures/` for web content.
- Use the in-memory SQLite database and configured client fixtures in `tests/conftest.py`.
- Mock boundaries only, such as temporary storage and browser routing; do not mock Pravda internals.
- Test public behavior rather than implementation details.
- Keep the test suite small and meaningful.

## Validation

```bash
uv run --env-file .env pytest
uv run ruff check .
uv run ruff format --check .
```
