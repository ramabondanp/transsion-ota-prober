"""Cache dedup: concurrent get_cached_ota_metadata fetches each URL once."""

import threading
from pathlib import Path
from unittest.mock import patch

from checkota.processor import get_cached_ota_metadata
from checkota.runtime import RunContext


def _make_ctx() -> RunContext:
    return RunContext(
        env={},
        processed_path=Path("/dev/null"),
        processed_titles=set(),
        dry_run=True,
        pool_size=4,
    )


def test_concurrent_fetch_runs_once_per_url():
    """Eight threads hitting the same URL must trigger exactly one
    get_ota_metadata call; the rest block on the in-flight event and reuse it."""
    ctx = _make_ctx()
    url = "https://x/ota.zip"
    calls = {"n": 0}
    fetcher_started = threading.Event()
    gate = threading.Event()

    def fake_fetch(u, session=None, stop_event=None):
        calls["n"] += 1
        fetcher_started.set()
        gate.wait()  # hold the single fetcher until the test releases it
        return {"fingerprint": "X/Y/Z:14/A/B:1:user/release-keys"}

    with patch("checkota.processor.get_ota_metadata", fake_fetch):
        results: dict = {}
        threads = []

        def worker(i: int) -> None:
            results[i] = get_cached_ota_metadata(ctx, url)

        for i in range(8):
            t = threading.Thread(target=worker, args=(i,))
            threads.append(t)
            t.start()

        # Wait until the lone fetcher is running (registered + fetching).
        assert fetcher_started.wait(timeout=5)
        # Release it; waiters then wake and read the cached result.
        gate.set()
        for t in threads:
            t.join(timeout=5)

    # Every thread must have finished (a deadlock would leave some alive and
    # the join would time out silently).
    assert not any(t.is_alive() for t in threads), "a worker thread deadlocked"
    assert len(results) == 8, f"expected 8 results, got {len(results)}"

    assert calls["n"] == 1, f"expected exactly 1 fetch, got {calls['n']}"
    expected = {"fingerprint": "X/Y/Z:14/A/B:1:user/release-keys"}
    assert all(r == expected for r in results.values())
