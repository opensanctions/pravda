# Pravda

Pravda is the evidence layer — a Python library that other services build on. It captures and stores durable, addressable evidence of web pages (HAR network recordings, screenshots, rendered HTML, plaintext, metadata) that downstream services can inspect, diff, and reason over.

## Project philosophy

- Early-stage. No backward compatibility. No fallback behaviors.
- Simple, direct code. No premature abstraction.
- If two approaches exist, prefer the simpler one.
- Grow the code as needs emerge, not in anticipation of them.

## Stack

- **Python** 3.13+ managed by **uv**.
- **Pravda is a library, not a service.** It is imported by the services that build on it; it connects from the caller's process to a remote browser and to Postgres/storage directly. There is no HTTP API, no Uvicorn, and no shipped image.
- **Playwright** (Python) connecting over WebSocket to a Docker container.
- Docker container runs **headed** Chrome in a virtual framebuffer (xvfb), exposed via `playwright run-server`. Headed mode avoids headless-detection fingerprinting that some sites use to block scrapers.
- Launch options (`channel`, `headless`, etc.) are sent from the Python client via the `x-playwright-launch-options` WebSocket header — no custom server JS needed.
- **Postgres** accessed via **SQLAlchemy** (async) — stores snapshot metadata and the index of recorded response bodies. Schema changes are managed with **Alembic** migrations (see [Database migrations](#database-migrations)); the library does not create the schema.
- **fsspec** for content-addressed blob storage on any filesystem (local, S3, GCS). The local FS is wrapped in `AsyncFileSystemWrapper` so writes don't block the event loop; remote backends are natively async. See `pravda/storage.py`.

## Public API

Exported from `pravda`:

- `snapshot(url, *, drive=None)` — capture and persist. Without `drive` it is the default (one-shot) path: connect to the remote browser, navigate (waiting for the normal `load` state), capture evidence, process the HAR, and persist. With `drive(page, url)` the caller pilots the recording page itself with **real Playwright** (selectors, load states, clicks, form fills) and **owns the initial navigation**; Pravda still owns the browser connection, the HAR, capture, persistence, and cleanup, and captures whatever state `drive` leaves behind. The whole pipeline runs under a wall-clock breaker; browser/navigation/timeout failures — including Playwright errors/timeouts raised from `drive` — are still persisted as `Snapshot` rows with `error` set and no evidence. An arbitrary (non-Playwright) exception raised by `drive` propagates and persists nothing.
- `snapshots(url)` — history query returning every exact-URL match newest first. No pagination.
- `Snapshot` — the immutable public value (a frozen dataclass).

Pravda owns its database sessions: every attempt (success, partial, or failure) is committed through `pravda.db.async_session` and returned as a public `Snapshot`. Callers need no database wiring.

## Conventions

- Async only (`asyncio`). No sync wrappers.
- Dependencies are added with `uv add`. Don't edit `pyproject.toml` manually.
- Playwright browsers are NOT installed locally — they live in the Docker container. The `playwright` Python package is a client only.
- Keep imports at the top of each file. No lazy imports unless there's a real cost.
- Use `pathlib.Path` for file paths, not string manipulation.
- Configuration is read from the environment (`os.environ`) in the module that needs it — `BROWSER_WS_URL`, `DATABASE_URL`, `STORAGE_BASE_PATH`. The library is config-injected by env, never by constructor arguments. `.env` holds those values for ad-hoc commands and migrations, loaded via `uv run --env-file .env`.
- True constants (paths, format strings, etc.) live in the module that uses them.
- Use the Python `logging` module for logging. Get loggers with `logging.getLogger(__name__)`.
- The user manages git commits, branching, etc.
- Use full, descriptive variable names. No abbreviations.

## Downloads and PDFs

Chrome's viewer extensions can consume a response stream before it reaches the renderer (the PDF viewer is the main case). The browser image sets the `AlwaysOpenPdfExternally` Chrome policy so these become downloads instead; `capture_page`/`capture_current` recovers the bytes via Playwright's `download` event and HAR processing (`pravda.http_archive`) folds them back into the matching HAR entry as a `content._file`. So a downloaded body looks like any other — no dedicated PDF field.

## Storage and access model

Pravda uses content-addressed storage (filenames of the form `<sha1>.<extension>`, where the extension carries the artifact's type). `Snapshot` values expose file paths — there is no blob download endpoint. Downstream services that share access to the same storage (local volume, S3 bucket, GCS bucket) can read files directly from the returned path. Pravda is the evidence capture layer, not a content delivery proxy.

## Running

Pravda has no server to run. Bring up the infrastructure it connects to, install the library, and apply migrations from the host:

```bash
# Remote browser + dev/test Postgres
# (playwright server, postgres_dev :5432, postgres_test :5433)
docker compose up -d

# Install the library and its dependencies
uv sync

# Apply migrations to the dev database
uv run --env-file .env alembic upgrade head

# Stop the containers
docker compose down
```

`docker-compose.yml` runs three services: the headed `playwright` browser server, `postgres_dev` (`:5432`, what the library commits to via `DATABASE_URL`), and `postgres_test` (`:5433`, used by the test suite). Both Postgres instances start together so a fresh checkout is ready for either. The schema is not created automatically — run `alembic upgrade head` once the dev database is up. `.env` keeps hostnames as `localhost` so the library and migrations run against the compose-exposed ports from the host.

## Database migrations

Alembic manages the schema. The migration env (`alembic/env.py`) reuses the async engine from `pravda.db` and `Base.metadata` for autogenerate.

```bash
# Apply pending migrations to the dev database
uv run --env-file .env alembic upgrade head

# After changing models in pravda/db.py, generate a migration
uv run --env-file .env alembic revision --autogenerate -m "describe the change"
```

Review the generated file under `alembic/versions/` (autogenerate is a starting point, not always correct). When a migration creates a `postgresql.ENUM`, manage the type explicitly in both `upgrade` and `downgrade` so the round-trip stays clean (see the initial migration). The test suite does not run migrations — `tests/conftest.py` builds the schema with `Base.metadata.create_all`.

## Adding dependencies

```bash
uv add <package>
```

## Testing

Test behavior, not implementation. If renaming an internal function breaks a test, the test is wrong.

- **Real database.** The schema is built once per session with `Base.metadata.create_all` in `tests/conftest.py` (not via Alembic) so tests stay independent of the migration history. Because Pravda owns its sessions and commits through `pravda.db.async_session`, an autouse fixture rebinds that session factory to a single connection joined to one outer transaction (`join_transaction_mode='create_savepoint'`) and rolls it back on teardown, so every test's commits vanish without manual cleanup. Storage and route interception are the other boundaries.
- **No real network.** Use Playwright's `page.route()` to serve fixture content from `tests/fixtures/`. Deterministic, offline-friendly.
- **Don't mock internals.** Mock at the boundary only: tmp dirs for storage, route interception for the browser. If you feel the urge to mock something inside a module, the module probably needs a cleaner seam.
- **Keep it minimal.** Few, meaningful tests that cover the actual workflow. No tests for getters, no tests that just assert a mock was called.

## Linting and formatting

Pre-commit hooks run automatically on every commit:

- **ruff check --fix**
- **ruff format**
