"""An idle rate-window entry must not live forever — a long-running
PEP otherwise grows one entry per key it ever counted for as long as it
runs."""

from __future__ import annotations

from chaperone.app import (
    RATE_WINDOW_IDLE_MINUTES,
    _RateWindow,  # pyright: ignore[reportPrivateUsage]
)


def _keys(window: _RateWindow) -> set[str]:
    # White-box on purpose: eviction is only observable as internal state.
    return set(window._windows)  # pyright: ignore[reportPrivateUsage]


def test_idle_entries_are_evicted() -> None:
    window = _RateWindow()
    window.check_and_count("stale-g1", limit=100, now_minute=0)
    assert "stale-g1" in _keys(window)

    # A later call for a different key sweeps entries idle past the cap.
    window.check_and_count("fresh-g2", limit=100, now_minute=RATE_WINDOW_IDLE_MINUTES + 1)

    assert "stale-g1" not in _keys(window)
    assert "fresh-g2" in _keys(window)


def test_recently_used_entries_survive_the_sweep() -> None:
    window = _RateWindow()
    window.check_and_count("recent-g1", limit=100, now_minute=0)

    # A second key's call one minute later must not evict a still-fresh
    # entry — only ones idle past RATE_WINDOW_IDLE_MINUTES go.
    window.check_and_count("other-g2", limit=100, now_minute=1)

    assert "recent-g1" in _keys(window)
