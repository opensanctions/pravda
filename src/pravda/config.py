"""Instance-scoped runtime configuration."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Timeouts:
    """Wall-clock budgets for the captured website, in milliseconds.

    Expiry is recorded evidence — a persisted failed attempt, or a dropped
    artifact — because "the page never loaded" is a finding about the target.

        snapshot_milliseconds: One wall-clock bound over the whole
            target-facing capture; expiry fails the snapshot.
        nav_milliseconds: Reaching the first commit of the target URL.
        download_milliseconds: Waiting for the target's download, then
            saving it.
        dom_capture_milliseconds: Waiting for the DOM to settle, then
            reading it; expiry drops the rendered HTML.
        dom_settle_quiet_milliseconds: How long the DOM must stay
            mutation-free before capture; a duration, not a ceiling.
        screenshot_milliseconds: The full-page screenshot; expiry drops it.
    """

    snapshot_milliseconds: float = 240_000
    nav_milliseconds: float = 10_000
    load_milliseconds: float = 30_000
    drive_milliseconds: float = 60_000
    download_milliseconds: float = 15_000
    dom_capture_milliseconds: float = 10_000
    dom_settle_quiet_milliseconds: float = 500
    screenshot_milliseconds: float = 15_000


@dataclass(frozen=True)
class PravdaConfig:
    """Runtime configuration for a :class:`~pravda.Pravda` instance."""

    browser_ws_url: str
    storage_base_path: str
    timeouts: Timeouts = Timeouts()
