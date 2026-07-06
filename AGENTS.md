# Pravda

Pravda is the evidence layer — a service that other services build on. It captures and stores durable, addressable evidence of web pages (HAR network recordings, screenshots, rendered HTML, plaintext, metadata) that downstream services can inspect, diff, and reason over.

## Project philosophy

- Early-stage. No backward compatibility. No fallback behaviors.
- Simple, direct code. No premature abstraction.
- If two approaches exist, prefer the simpler one.
- Grow the code as needs emerge, not in anticipation of them.

## Stack

- **Python** 3.13+ managed by **uv**.
- **FastAPI** — HTTP API for service-to-service access.
- **Playwright** (Python) connecting over WebSocket to a Docker container.
- Docker container runs **headed** Chrome in a virtual framebuffer (xvfb), exposed via `playwright run-server`. Headed mode avoids headless-detection fingerprinting that some sites use to block scrapers.
- Launch options (`channel`, `headless`, etc.) are sent from the Python client via the `x-playwright-launch-options` WebSocket header — no custom server JS needed.
- **Postgres** accessed via **SQLAlchemy** (async) — stores snapshot metadata and the index of recorded response bodies. Schema changes are managed with **Alembic** migrations (see [Database migrations](#database-migrations)); the app does not create the schema on startup.
- **fsspec** for content-addressed blob storage on any filesystem (local, S3, GCS). The local FS is wrapped in `AsyncFileSystemWrapper` so writes don't block the event loop; remote backends are natively async. See `pravda/storage.py`.

## Conventions

- Async only (`asyncio`). No sync wrappers.
- Dependencies are added with `uv add`. Don't edit `pyproject.toml` manually.
- Playwright browsers are NOT installed locally — they live in the Docker container. The `playwright` Python package is a client only.
- Keep imports at the top of each file. No lazy imports unless there's a real cost.
- Use `pathlib.Path` for file paths, not string manipulation.
- Environment-specific config goes in `.env`, loaded by uvicorn via `--env-file`.
- Read env vars with `os.environ` in the module that needs them.
- True constants (paths, format strings, etc.) live in the module that uses them.
- Use the Python `logging` module for logging. Get loggers with `logging.getLogger(__name__)`.
- The user manages git commits, branching, etc.
- Use full, descriptive variable names. No abbreviations.

## Downloads and PDFs

Chrome's viewer extensions can consume a response stream before it reaches the renderer (the PDF viewer is the main case). The browser image sets the `AlwaysOpenPdfExternally` Chrome policy so these become downloads instead; `capture_page` recovers the bytes via Playwright's `download` event and the API layer folds them back into the matching HAR entry as a `content._file`. So a downloaded body looks like any other — no dedicated PDF field.

## Storage and access model

Pravda uses content-addressed storage (filenames of the form `<sha1>.<extension>`, where the extension carries the artifact's type). The API returns file paths in snapshot responses — there is no blob download endpoint. Downstream services that share access to the same storage (local volume, S3 bucket, GCS bucket) can read files directly from the returned path. Pravda is the evidence capture layer, not a content delivery proxy.

## Running

```bash
# Playwright + Postgres
docker compose up -d

# Run the API server
uv run uvicorn pravda.api:app --reload --reload-dir pravda --env-file .env

# Stop all containers
docker compose down
```

`docker-compose.yml` runs two Postgres instances: `postgres_dev` (:5432, used by the API) and `postgres_test` (:5433, used by the test suite). Both start together so a fresh checkout is ready for either.

The compose `db_migrate` service runs `alembic upgrade head` and the `api` service waits for it to finish, so `docker compose up` applies pending migrations before the app boots. (The `db_migrate` and `api` services override `DATABASE_URL` to the `postgres_dev` service hostname; `.env` keeps `localhost` for running uvicorn on the host.)

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

- **Real database.** Each test runs inside a transaction that rolls back after. Tests are isolated, fast, and leave no residue. The schema is built with `Base.metadata.create_all` in `tests/conftest.py` (not via Alembic) so tests stay independent of the migration history.
- **No real network.** Use Playwright's `page.route()` to serve fixture content from `tests/fixtures/`. Deterministic, offline-friendly.
- **Don't mock internals.** Mock at the boundary only: tmp dirs for storage, route interception for the browser. If you feel the urge to mock something inside a module, the module probably needs a cleaner seam.
- **Keep it minimal.** Few, meaningful tests that cover the actual workflow. No tests for getters, no tests that just assert a mock was called.

## Linting and formatting

Pre-commit hooks run automatically on every commit:

- **ruff check --fix**
- **ruff format**
