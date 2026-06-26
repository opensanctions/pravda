# Pravda

Pravda is the evidence layer — a service that other services build on. It uses Playwright to capture and store durable, addressable evidence of web pages: rendered HTML, plaintext, full-page screenshots, and a network archive (a HAR recording with response bodies). It turns live web pages into durable, addressable evidence that can be inspected, diffed, and reasoned over long after the original page has changed.

## What it does (v0)

- Captures web pages as rendered HTML + plaintext + full-page screenshots
- Records a HAR of all network activity, with response bodies stored as separate content-addressed blobs
- Tracks URLs and their snapshot history
- Uses content-addressed storage
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
