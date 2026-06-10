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
- Docker container runs Chrome in a virtual framebuffer (xvfb), exposed via `playwright run-server`.
- Launch options (`channel`, `headless`, etc.) are sent from the Python client via the `x-playwright-launch-options` WebSocket header — no custom server JS needed.

## Project structure

```
.env                     # environment-specific config (not committed)
.env.example             # template with defaults (committed)
Dockerfile               # Chrome + xvfb + run-server
docker-compose.yml       # single "browser" service
pravda/
  __init__.py
  __main__.py            # entry point
  constants.py           # loads .env, defines constants
```

## Conventions

- Async only (`asyncio`). No sync wrappers.
- Dependencies are added with `uv add`. Don't edit `pyproject.toml` manually.
- Playwright browsers are NOT installed locally — they live in the Docker container. The `playwright` Python package is a client only.
- Keep imports at the top of each file. No lazy imports unless there's a real cost.
- Use `pathlib.Path` for file paths, not string manipulation.
- Environment-specific config goes in `.env`, loaded via `python-dotenv` in `pravda/constants.py`.
- True constants (paths, format strings, etc.) live in `pravda/constants.py`.
- Access config through `constants.py`, never call `os.environ` or `dotenv` elsewhere.
- Use the Python `logging` module for logging. Get loggers with `logging.getLogger(__name__)`.
- Don't run git commands. The user manages commits, branching, etc.

## Running

```bash
# Start the browser container
docker compose up -d browser

# Run the API server
uv run uvicorn pravda.api:app --reload

# Stop the browser container
docker compose down
```

## Adding dependencies

```bash
uv add <package>
```

## Linting and formatting

Pre-commit hooks run automatically on every commit:

- **ruff check --fix**
- **ruff format**
