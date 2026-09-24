"""Shared helpers for routing browser fixtures and stalling storage writes."""

import asyncio
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def fulfill_html(route):
    """Fulfill a route with the example HTML fixture."""
    return route.fulfill(
        body=(FIXTURES / "example.html").read_text(),
        headers={"content-type": "text/html"},
    )


def fulfill_pdf(route):
    """Fulfill a route with the sample PDF fixture."""
    return route.fulfill(
        body=(FIXTURES / "sample.pdf").read_bytes(),
        headers={"content-type": "application/pdf"},
    )


async def slow_pipe_file(path, value, **kwargs):
    """Stand in for a storage write that outlives its timeout budget."""
    await asyncio.sleep(1)
