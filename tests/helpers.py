"""Shared helpers for routing browser fixtures to fixture content."""

import base64
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"

# A valid 1x1 PNG, so subresource routes record real image bytes.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBg"
    "AAAABQABh6FO1AAAAABJRU5ErkJggg=="
)


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


def fulfill_standalone(route):
    """Fulfill a route with the standalone HTML fixture."""
    return route.fulfill(
        body=(FIXTURES / "standalone.html").read_text(),
        headers={"content-type": "text/html"},
    )
