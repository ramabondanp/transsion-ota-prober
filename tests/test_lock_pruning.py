"""Lock-file lifecycle regression tests.

Covers:
  - per-title claim locks for committed titles are pruned
  - recent locks for unknown titles are kept (a failed claim may retry)
  - aged-out locks for unknown titles are pruned
  - unrelated files (db lock, non-digest names) are never touched
  - config lock files are removed after a successful config rewrite
  - Telegram error logs never contain the bot token
"""

import os
import time
from textwrap import dedent
from typing import cast

import requests

from checkota.fingerprints import (
    _database_lock_path,
    _title_lock_path,
    prune_title_locks,
    save_processed_title,
)
from checkota.manager import Config, update_config_from_fingerprint
from checkota.telegram import TgNotify


def test_prune_removes_committed_title_locks(tmp_path):
    state = tmp_path / "processed_updates.txt"
    assert save_processed_title(state, "Title A") is True
    lock = _title_lock_path(state, "Title A")
    assert lock.exists()  # the save path creates the claim lock

    removed = prune_title_locks(state, {"Title A"})

    assert removed == 1
    assert not lock.exists()
    # The stable database lock survives replacement and must be kept.
    assert _database_lock_path(state).exists()


def test_prune_keeps_recent_unknown_locks(tmp_path):
    state = tmp_path / "processed_updates.txt"
    state.write_text("", encoding="utf-8")
    lock = _title_lock_path(state, "Ghost")
    lock.touch()

    assert prune_title_locks(state, set()) == 0
    assert lock.exists()


def test_prune_removes_aged_unknown_locks(tmp_path):
    state = tmp_path / "processed_updates.txt"
    state.write_text("", encoding="utf-8")
    lock = _title_lock_path(state, "Old Ghost")
    lock.touch()
    old = time.time() - 8 * 24 * 3600
    os.utime(lock, (old, old))

    assert prune_title_locks(state, set()) == 1
    assert not lock.exists()


def test_prune_ignores_unrelated_files(tmp_path):
    state = tmp_path / "processed_updates.txt"
    state.write_text("Title A\n", encoding="utf-8")
    unrelated = [
        _database_lock_path(state),
        tmp_path / "processed_updates.txt.db.lock",
        tmp_path / "processed_updates.txt.nothex.lock",
        tmp_path / "processed_updates.txt.tmp",
        tmp_path / "other.txt",
    ]
    for path in unrelated:
        path.touch()
    aged_lock = _title_lock_path(state, "Old Ghost")
    aged_lock.touch()
    old = time.time() - 30 * 24 * 3600
    os.utime(aged_lock, (old, old))

    removed = prune_title_locks(state, {"Title A"})

    assert removed == 1  # only the aged digest lock
    for path in unrelated:
        assert path.exists(), f"{path} must not be pruned"


def test_config_lock_removed_after_update(tmp_path):
    config_path = tmp_path / "config-X6873.yml"
    config_path.write_text(
        dedent(
            """\
            oem: "Infinix"
            product: "X6873-OP"
            device: "Infinix-X6873"
            android_version: "14"
            build_tag: "B"
            incremental: "I"
            model: "Infinix GT 30 Pro"
            """
        ),
        encoding="utf-8",
    )
    cfg = Config(
        build_tag="B",
        incremental="I",
        android_version="14",
        model="Infinix GT 30 Pro",
        device="Infinix-X6873",
        oem="Infinix",
        product="X6873-OP",
    )
    target = "Infinix/X6873-OP/Infinix-X6873:16/BP2A.250605.031.A3/201350016:user/release-keys"

    assert update_config_from_fingerprint(config_path, cfg, target) is True
    assert not (tmp_path / "config-X6873.yml.lock").exists()


def test_telegram_http_error_does_not_leak_token(capsys):
    token = "SECRET123-TOKEN"

    class _Session:
        def post(self, *args, **kwargs):
            raise requests.HTTPError(
                "400 Client Error: Bad Request for url: "
                f"https://api.telegram.org/bot{token}/sendMessage"
            )

    notifier = TgNotify(token, "chat", session=cast(requests.Session, _Session()))
    assert notifier.send("hello", truncate_desc=False) is False

    logged = capsys.readouterr().out
    assert token not in logged
    assert "***" in logged


def test_telegram_transport_error_does_not_leak_token(capsys):
    token = "SECRET123-TOKEN"

    class _Session:
        def post(self, *args, **kwargs):
            raise requests.ConnectionError(
                "Failed to establish a new connection for url: "
                f"https://api.telegram.org/bot{token}/sendMessage"
            )

    notifier = TgNotify(token, "chat", session=cast(requests.Session, _Session()))
    assert notifier.send("hello", truncate_desc=False) is False

    logged = capsys.readouterr().out
    assert token not in logged
    assert "***" in logged
