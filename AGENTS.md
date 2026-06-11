# Pravda

Pravda is the evidence layer — a service that other services build on. It captures and stores durable, addressable evidence of web pages (MHTML archives, screenshots, headers, metadata) that downstream services can inspect, diff, and reason over.

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

## Project structure

```
.env                     # environment-specific config (not committed)
.env.example             # template with defaults (committed)
Dockerfile               # Chrome + xvfb + run-server
docker-compose.yml       # single "playwright" service
pravda/
  __init__.py
```

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
- Don't run git commands. The user manages commits, branching, etc.

## Storage and access model

Pravda uses content-addressed storage (SHA-256 hashes as filenames). The API returns file paths in snapshot responses — there is no blob download endpoint. Downstream services that share access to the same storage (local volume, S3 bucket, GCS bucket) can read files directly from the returned path. Pravda is the evidence capture layer, not a content delivery proxy.

## Running

```bash
# Start the browser and database containers
docker compose up -d

# Run the API server
uv run uvicorn pravda.api:app --reload --env-file .env

# Stop all containers
docker compose down
```

## Adding dependencies

```bash
uv add <package>
```

## Testing

Test behavior, not implementation. If renaming an internal function breaks a test, the test is wrong.

- **Real database.** Each test runs inside a transaction that rolls back after. Tests are isolated, fast, and leave no residue.
- **No real network.** Use Playwright's `page.route()` to serve fixture content from `tests/fixtures/`. Deterministic, offline-friendly.
- **Don't mock internals.** Mock at the boundary only: tmp dirs for storage, route interception for the browser. If you feel the urge to mock something inside a module, the module probably needs a cleaner seam.
- **Keep it minimal.** Few, meaningful tests that cover the actual workflow. No tests for getters, no tests that just assert a mock was called.

## Linting and formatting

Pre-commit hooks run automatically on every commit:

- **ruff check --fix**
- **ruff format**
