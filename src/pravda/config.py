"""Instance-scoped runtime configuration."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Timeouts:
    """Wall-clock budgets for one snapshot, in milliseconds throughout.

    Playwright timeout arguments take these values directly; asyncio
    deadlines divide by 1000 where they consume a budget.

    Expiry either fails the snapshot (a failed attempt is persisted without
    evidence), degrades it (only the affected artifact is dropped), or
    propagates to the caller (nothing is persisted). ``cleanup`` is
    best-effort and never changes the outcome.

    Failing the snapshot:

        snapshot_milliseconds: Everything: connection, capture, and
            finalization.
        connect_milliseconds: WebSocket handshake with the browser endpoint.
        nav_milliseconds: Reaching the first commit of the target URL.
        load_milliseconds: The document's ``load`` event after commit.
        drive_milliseconds: The caller-provided drive callback.
        download_milliseconds: Saving a viewer-handled download.
        context_close_milliseconds: Closing the recording context; an
            unfinalized recording invalidates all evidence from the capture.

    Degrading to a missing artifact:

        dom_capture_milliseconds: Waiting for the DOM to settle, then
            reading it.
        dom_settle_quiet_milliseconds: How long the DOM must stay
            mutation-free before capture; a duration, not a ceiling.
        screenshot_milliseconds: The full-page screenshot.
        standalone_milliseconds: Deriving self-contained HTML.

    Propagating to the caller, nothing persisted:

        har_processing_milliseconds: Unpacking and storing the recorded
            archive.
        persist_milliseconds: Committing the snapshot row.

    Best-effort, never changing the outcome:

        cleanup_milliseconds: Tearing down the browser connection and driver.
    """

    # Failing the snapshot.
    snapshot_milliseconds: float = 240_000
    connect_milliseconds: float = 10_000
    nav_milliseconds: float = 10_000
    load_milliseconds: float = 30_000
    drive_milliseconds: float = 60_000
    download_milliseconds: float = 15_000
    context_close_milliseconds: float = 30_000
    # Degrading to a missing artifact.
    dom_capture_milliseconds: float = 10_000
    dom_settle_quiet_milliseconds: float = 500
    screenshot_milliseconds: float = 15_000
    standalone_milliseconds: float = 20_000
    # Propagating to the caller, nothing persisted.
    har_processing_milliseconds: float = 20_000
    persist_milliseconds: float = 15_000
    # Best-effort.
    cleanup_milliseconds: float = 5_000


@dataclass(frozen=True)
class PravdaConfig:
    """Runtime configuration for a :class:`~pravda.Pravda` instance."""

    browser_ws_url: str
    storage_base_path: str
    timeouts: Timeouts = Timeouts()
