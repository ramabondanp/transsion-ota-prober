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


def test_malformed_metadata_is_failure_cached(tmp_path):
    ctx = _make_ctx()
    url = "https://x/malformed.zip"
    with patch(
        "checkota.processor.get_ota_metadata",
        return_value={"fingerprint": ""},
    ) as fetch:
        assert get_cached_ota_metadata(ctx, url) is None
        assert get_cached_ota_metadata(ctx, url) is None
    assert fetch.call_count == 1
    assert url not in ctx.metadata_cache
    assert url in ctx.metadata_failures


def test_stopped_metadata_fetch_is_not_failure_cached(tmp_path):
    ctx = _make_ctx()
    url = "https://x/stopped.zip"
    ctx.stop_event.set()
    with patch(
        "checkota.processor.get_ota_metadata",
        return_value=None,
    ) as fetch:
        assert get_cached_ota_metadata(ctx, url) is None
    assert fetch.call_count == 0
    assert url not in ctx.metadata_failures


def test_stopped_metadata_fetch_is_not_failure_cached_after_fetch(tmp_path):
    ctx = _make_ctx()
    url = "https://x/stopped-during-fetch.zip"
    calls = {"n": 0}

    def fake_fetch(url, session=None, stop_event=None):
        calls["n"] += 1
        stop_event.set()

    with patch("checkota.processor.get_ota_metadata", fake_fetch):
        assert get_cached_ota_metadata(ctx, url) is None
    assert calls["n"] == 1
    assert url not in ctx.metadata_failures

    ctx.stop_event.clear()
    with patch(
        "checkota.processor.get_ota_metadata",
        return_value={"fingerprint": "X/Y/Z:14/A/B:1:user/release-keys"},
    ) as retry:
        assert get_cached_ota_metadata(ctx, url) is not None
    assert retry.call_count == 1


def test_metadata_waiter_times_out(monkeypatch):
    ctx = _make_ctx()
    url = "https://x/stuck.zip"
    event = threading.Event()
    with ctx.cache_lock:
        ctx._metadata_inflight[url] = event
    monkeypatch.setattr("checkota.processor._METADATA_WAIT_TIMEOUT", 0.01)
    assert get_cached_ota_metadata(ctx, url) is None


def test_get_cached_ota_metadata_uses_zip_proxy_flag():
    ctx = RunContext(
        env={},
        processed_path=Path("/dev/null"),
        processed_titles=set(),
        dry_run=True,
        pool_size=4,
        zip_proxy=True,
    )
    url = "https://x/proxy_ota.zip"
    received = {}

    def fake_fetch(u, session=None, stop_event=None, use_proxy_env=False):
        received["session"] = session
        received["use_proxy_env"] = use_proxy_env
        return {"fingerprint": "X/Y/Z:14/A/B:1:user/release-keys"}

    with patch("checkota.processor.get_ota_metadata", fake_fetch):
        res = get_cached_ota_metadata(ctx, url)
        assert res is not None
        assert received["session"] is ctx.session()
        assert received["use_proxy_env"] is True

