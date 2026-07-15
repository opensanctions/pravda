"""Unit tests for the shared page-level response/download observer.

``_PageObserver`` is the one place both capture paths install the
``download``/``response`` listeners, so its filtering rules are tested here in
isolation (deterministic, no browser): the main-frame navigation status is
tracked, an iframe navigation response can never overwrite it, only the first
download counts, ``wait_download`` is bounded, and listener removal is
exception-safe. The end-to-end capture behavior is covered elsewhere; these
pin the observer's own contract.
"""

import asyncio

import pytest

import pravda.capture as capture_module
from pravda.capture import _PageObserver


class _FakeFrame:
    """Identity-only stand-in for a Playwright frame."""


class _FakeRequest:
    def __init__(self, is_navigation: bool) -> None:
        self._is_navigation = is_navigation

    def is_navigation_request(self) -> bool:
        return self._is_navigation


class _FakeResponse:
    def __init__(self, status: int, request: _FakeRequest, frame: _FakeFrame) -> None:
        self.status = status
        self.request = request
        self.frame = frame


class _FakeDownload:
    def __init__(self, url: str) -> None:
        self.url = url


class _FakePage:
    """Minimal page that records on/remove_listener for assertion."""

    def __init__(self) -> None:
        self.main_frame = _FakeFrame()
        self._listeners: dict[str, list] = {}

    def on(self, event: str, handler) -> None:
        self._listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler) -> None:
        self._listeners.get(event, []).remove(handler)

    def emit(self, event: str, payload) -> None:
        for handler in list(self._listeners.get(event, [])):
            handler(payload)

    def listeners(self, event: str) -> list:
        return self._listeners.get(event, [])


def _nav_response(status: int, frame: _FakeFrame) -> _FakeResponse:
    return _FakeResponse(status, _FakeRequest(True), frame)


@pytest.mark.asyncio
async def test_observer_records_main_frame_navigation_status():
    """A main-frame navigation response is recorded as the subject status."""
    page = _FakePage()
    main_frame = page.main_frame
    async with _PageObserver(page) as observer:
        page.emit("response", _nav_response(200, main_frame))
        assert observer.navigation_status == 200


@pytest.mark.asyncio
async def test_observer_ignores_iframe_navigation_status():
    """A sub-frame (iframe) navigation response never overwrites the main
    document's status — the core regression for the drive path, where the
    caller's page may load iframes."""
    page = _FakePage()
    main_frame = page.main_frame
    async with _PageObserver(page) as observer:
        page.emit("response", _nav_response(200, main_frame))
        # An iframe navigation with a different status must not overwrite.
        page.emit("response", _nav_response(404, _FakeFrame()))
        assert observer.navigation_status == 200

    # Even before any main-frame response, an iframe navigation alone sets
    # nothing — the subject stays unknown rather than adopting the iframe.
    bare_page = _FakePage()
    async with _PageObserver(bare_page) as observer2:
        bare_page.emit("response", _nav_response(302, _FakeFrame()))
        assert observer2.navigation_status is None


@pytest.mark.asyncio
async def test_observer_ignores_non_navigation_response():
    """Only navigation responses update the status; a same-frame subresource
    response (a stylesheet, XHR, etc.) is ignored."""
    page = _FakePage()
    main_frame = page.main_frame
    async with _PageObserver(page) as observer:
        page.emit("response", _nav_response(200, main_frame))
        page.emit("response", _FakeResponse(500, _FakeRequest(False), main_frame))
        assert observer.navigation_status == 200


@pytest.mark.asyncio
async def test_observer_tracks_first_download_only():
    """Only the first download becomes the capture subject; later ones are
    ignored."""
    page = _FakePage()
    first = _FakeDownload("https://example.com/first")
    second = _FakeDownload("https://example.com/second")
    async with _PageObserver(page) as observer:
        page.emit("download", first)
        assert observer.download is first
        page.emit("download", second)
        assert observer.download is first


@pytest.mark.asyncio
async def test_observer_wait_download_returns_seen_download():
    """wait_download returns immediately (no blocking) once a download fired."""
    page = _FakePage()
    download = _FakeDownload("https://example.com/a")
    async with _PageObserver(page) as observer:
        page.emit("download", download)
        # If this blocked, wait_for's own 1s budget would trip.
        result = await asyncio.wait_for(observer.wait_download("u"), timeout=1.0)
        assert result is download


@pytest.mark.asyncio
async def test_observer_wait_download_times_out(monkeypatch):
    """wait_download is bounded: with no download it returns None within the
    DOWNLOAD_TIMEOUT_S budget (tightened here to keep the test fast)."""
    monkeypatch.setattr(capture_module, "DOWNLOAD_TIMEOUT_S", 0.1)
    page = _FakePage()
    async with _PageObserver(page) as observer:
        result = await observer.wait_download("https://example.com")
    assert result is None


@pytest.mark.asyncio
async def test_observer_removes_listeners_on_normal_exit():
    """Listeners are unregistered after a clean context exit."""
    page = _FakePage()
    async with _PageObserver(page):
        pass
    assert page.listeners("download") == []
    assert page.listeners("response") == []


@pytest.mark.asyncio
async def test_observer_removes_listeners_on_exception():
    """Listeners are unregistered even when the body raises — the observer
    never leaks a listener past the capture."""
    page = _FakePage()
    with pytest.raises(RuntimeError):
        async with _PageObserver(page):
            raise RuntimeError("boom")
    assert page.listeners("download") == []
    assert page.listeners("response") == []
