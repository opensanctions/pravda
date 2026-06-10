# Pravda

Pravda is the evidence layer — a service that other services build on. It uses Playwright to capture and store MHTML archives and full-page screenshots of web pages, along with response headers and snapshot metadata. It turns live web pages into durable, addressable evidence that can be inspected, diffed, and reasoned over long after the original page has changed.

## What it does (v0)

- Captures web pages as MHTML archives + screenshots
- Stores response headers
- Tracks URLs and their snapshot history
- Uses content-addressed storage
- Supports `ETag` / `Last-Modified` for HTTP 304 conditional fetching
- Runs Chrome (not Chromium) in a virtual framebuffer for realistic rendering

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```bash
# Start containers (Playwright browser + Postgres)
docker compose up -d

# Install dependencies
uv sync
```

## Usage

```bash
uv run uvicorn pravda.api:app --reload --env-file .env
```
