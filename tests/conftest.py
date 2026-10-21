"""Shared pytest fixtures for the test browser and storage."""

import os
from dataclasses import replace

import pytest
from playwright.async_api import async_playwright

from pravda import Pravda, Timeouts
from pravda.storage import Storage


@pytest.fixture()
def timeouts() -> Timeouts:
    """Fast capture budgets; tests override fields when they need to."""
    # dom_settle_quiet is a duration paid by every capture, and fixture
    # pages are static: they settle immediately.
    return replace(Timeouts(), dom_settle_quiet=0.05)


@pytest.fixture()
def storage(tmp_path):
    """An isolated content-addressed store pointing at a temporary directory."""
    return Storage.from_url(str(tmp_path))


@pytest.fixture()
def pravda(storage, timeouts):
    """A configured client sharing the test's artifact store."""
    return Pravda(
        os.environ["PRAVDA_TEST_BROWSER_WS_URL"],
        storage,
        timeouts=timeouts,
    )


@pytest.fixture(scope="session")
async def browser():
    playwright = await async_playwright().start()
    browser = await playwright.chromium.connect(
        os.environ["PRAVDA_TEST_BROWSER_WS_URL"]
    )
    yield browser
    await browser.close()
    await playwright.stop()
