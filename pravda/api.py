import logging

from fastapi import FastAPI

from pravda.db import init_db

logger = logging.getLogger(__name__)

app = FastAPI(title="Pravda", description="Evidence layer for web pages")


@app.on_event("startup")
async def startup() -> None:
    await init_db()
    logger.info("Database initialized")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
