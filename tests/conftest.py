"""Shared pytest fixtures for the test browser and storage."""

import os
from dataclasses import replace

import pytest
from playwright.async_api import async_playwright

from pravda import Pravda, PravdaConfig, Timeouts
from pravda.storage import Storage


@pytest.fixture()
def timeouts() -> Timeouts:
    """Fast capture budgets; tests override fields when they need to."""
    # dom_settle_quiet is a duration paid by every capture, and fixture
    # pages are static: they settle immediately.
    return replace(Timeouts(), dom_settle_quiet=0.05)


@pytest.fixture()
def pravda_config(tmp_path, timeouts) -> PravdaConfig:
    """Configuration for a client with an isolated artifact store."""
    return PravdaConfig(
        browser_ws_url=os.environ["PRAVDA_TEST_BROWSER_WS_URL"],
        storage_base_path=str(tmp_path),
        timeouts=timeouts,
    )


@pytest.fixture()
def pravda(pravda_config: PravdaConfig):
    """A configured client."""
    return Pravda(pravda_config)


@pytest.fixture()
def storage(tmp_path):
    """An isolated content-addressed store pointing at a temporary directory."""
    return Storage.from_url(str(tmp_path))


@pytest.fixture(scope="session")
async def browser():
    playwright = await async_playwright().start()
    browser = await playwright.chromium.connect(
        os.environ["PRAVDA_TEST_BROWSER_WS_URL"]
    )
    yield browser
    await browser.close()
    await playwright.stop()
