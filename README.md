# Pravda

Pravda is the evidence layer — a service that other services build on. It uses Playwright to capture and store durable, addressable evidence of web pages: rendered HTML, plaintext, full-page screenshots, and a network archive (a HAR recording with response bodies). It turns live web pages into durable, addressable evidence that can be inspected, diffed, and reasoned over long after the original page has changed.

## What it does (v0)

- Captures rendered HTML, plaintext, and full-page screenshots
- Records a HAR with response bodies stored as separate content-addressed blobs
- Recovers bodies Chrome's viewers swallow (e.g. PDFs) by forcing downloads and folding them back into the HAR
- Tracks URLs and their snapshot history
- Accepts a readiness condition: a lifecycle load state or a CSS selector
- Stores artifacts on any fsspec filesystem (local, S3, GCS)
- Runs Chrome (not Chromium) in a virtual framebuffer

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```bash
# Start containers (Playwright browser + dev and test Postgres)
docker compose up -d

# Install dependencies
uv sync
```

## Usage

```bash
uv run uvicorn pravda.api:app --reload --reload-dir pravda --env-file .env
```

## Storage and access

Artifacts live as `<sha1>.<extension>` files under a per-hostname prefix on any [fsspec](https://filesystem-spec.readthedocs.io/) filesystem (`local`, `s3://`, `gs://`), configured via `STORAGE_BASE_PATH` in `.env`. Snapshot responses return this `prefix` (storage base + normalized hostname) plus content-addressed filenames; downstream services read each artifact directly as `<prefix>/<filename>` from the shared backend. There is no blob download endpoint — Pravda is the evidence capture layer, not a content delivery proxy.
