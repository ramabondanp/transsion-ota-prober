"""Title ownership must never make overlapping sweeps wait on each other."""

import multiprocessing
from pathlib import Path

import pytest

from checkota import fingerprints, processor
from checkota.runtime import RunContext

pytestmark = pytest.mark.skipif(fingerprints._fcntl is None, reason="requires flock")


def _ctx(path):
    return RunContext(env={}, processed_path=path, processed_titles=set(), dry_run=False)


def _opposite_claims(path, first, second, barrier, results):
    ctx = _ctx(Path(path))
    try:
        assert processor._claim_new_update(ctx, first) is True
        barrier.wait(timeout=5)
        try:
            processor._claim_new_update(ctx, second)
        except fingerprints.TitleClaimBusy:
            busy = True
        else:
            busy = False
        # Keep the first claim held until both contenders have tried the second.
        barrier.wait(timeout=5)
        results.put((busy, ctx.processed_titles))
    finally:
        ctx.stop()


def test_overlapping_sweeps_with_opposite_claim_order_do_not_deadlock(tmp_path):
    path = tmp_path / "titles"
    spawn = multiprocessing.get_context("spawn")
    barrier = spawn.Barrier(2)
    results = spawn.Queue()
    processes = [
        spawn.Process(target=_opposite_claims, args=(str(path), a, b, barrier, results))
        for a, b in [("A", "B"), ("B", "A")]
    ]
    for process in processes:
        process.start()
    try:
        assert [results.get(timeout=10) for _ in processes] == [(True, set()), (True, set())]
    finally:
        for process in processes:
            process.join(timeout=1)
            if process.is_alive():
                process.terminate()
                process.join()
        results.close()
        results.join_thread()
    assert all(process.exitcode == 0 for process in processes)
    assert fingerprints.load_processed_titles(path) == set()


def test_busy_title_is_not_processed_and_can_be_retried(tmp_path):
    owner = _ctx(tmp_path / "titles")
    contender = _ctx(owner.processed_path)
    try:
        assert processor._claim_new_update(owner, "TITLE") is True
        with pytest.raises(fingerprints.TitleClaimBusy):
            processor._claim_new_update(contender, "TITLE")
        assert contender.processed_titles == set()
        assert contender.claimed_titles == set()
        assert contender.claimed_handles == {}
        # Registration/no-notifier saves must not block on the same claim either.
        assert fingerprints.save_processed_title(owner.processed_path, "TITLE") is False
        processor._release_claimed_update(owner, "TITLE")
        assert processor._claim_new_update(contender, "TITLE") is True
        assert processor._commit_claimed_update(contender, "TITLE") is True
        assert processor._claim_new_update(owner, "TITLE") is False
        assert owner.processed_titles == {"TITLE"}
    finally:
        owner.stop()
        contender.stop()


def test_claim_io_does_not_hold_context_lock_and_reserves_local_title(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path / "titles")

    def claim(path, title):
        assert ctx.file_lock.acquire(blocking=False)
        ctx.file_lock.release()
        with pytest.raises(fingerprints.TitleClaimBusy):
            processor._claim_new_update(ctx, title)
        # No handle: already committed by another process.

    monkeypatch.setattr(processor, "claim_processed_title", claim)
    assert processor._claim_new_update(ctx, "TITLE") is False
    assert ctx.processed_titles == {"TITLE"}
    assert ctx.claimed_titles == set()


def test_claim_io_failure_clears_local_reservation(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path / "titles")

    def claim(path, title):
        raise OSError("cannot open state")

    monkeypatch.setattr(processor, "claim_processed_title", claim)
    assert processor._claim_new_update(ctx, "TITLE") is None
    assert ctx.claimed_titles == set()
    assert ctx.claimed_handles == {}
    assert ctx.processed_titles == set()
