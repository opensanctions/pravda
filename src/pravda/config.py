"""Instance-scoped runtime configuration."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Timeouts:
    """Wall-clock budgets for one capture attempt, in seconds.

    Expiry never raises: it fails the snapshot, or drops the artifact
    it bounded.

        snapshot: One wall-clock bound over the whole target-facing capture.
        nav: Reaching the first commit of the target URL.
        load: The document's ``load`` event after commit.
        drive: The caller-provided drive callback.
        download: Waiting for the target's download, then saving it.
        dom_capture: Waiting for the DOM to settle, then reading it.
        dom_settle_quiet: How long the DOM must stay mutation-free before
            capture; a duration, not a ceiling.
        screenshot: The full-page screenshot.
        close: Closing the recording context, which finalizes its recording.
    """

    snapshot: float = 240.0
    nav: float = 10.0
    load: float = 30.0
    drive: float = 60.0
    download: float = 15.0
    dom_capture: float = 10.0
    dom_settle_quiet: float = 0.5
    screenshot: float = 15.0
    close: float = 30.0
