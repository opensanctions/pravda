"""Instance-scoped runtime configuration."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Timeouts:
    """Wall-clock budgets for the captured website, in seconds.

    Expiry is recorded evidence — a persisted failed attempt, or a dropped
    artifact — because "the page never loaded" is a finding about the target.

        snapshot: One wall-clock bound over the whole target-facing capture;
            expiry fails the snapshot.
        nav: Reaching the first commit of the target URL.
        load: The document's ``load`` event after commit.
        drive: The caller-provided drive callback.
        download: Waiting for the target's download, then saving it.
        dom_capture: Waiting for the DOM to settle, then reading it; expiry
            drops the rendered HTML.
        dom_settle_quiet: How long the DOM must stay mutation-free before
            capture; a duration, not a ceiling.
        screenshot: The full-page screenshot; expiry drops it.
    """

    snapshot: float = 240.0
    nav: float = 10.0
    load: float = 30.0
    drive: float = 60.0
    download: float = 15.0
    dom_capture: float = 10.0
    dom_settle_quiet: float = 0.5
    screenshot: float = 15.0


@dataclass(frozen=True)
class PravdaConfig:
    """Runtime configuration for a :class:`~pravda.Pravda` instance."""

    browser_ws_url: str
    storage_base_path: str
    timeouts: Timeouts = Timeouts()
