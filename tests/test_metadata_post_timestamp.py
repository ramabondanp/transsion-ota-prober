"""M1 fix — bad post-timestamp is logged at warning level; valid path still works."""

from checkota.metadata import get_ota_metadata


def _fake_fetch_returning_content(monkeypatch, content: str) -> None:
    def fake_fetch(url, member, **kwargs):
        return content.encode("utf-8")

    monkeypatch.setattr("checkota.metadata.fetch_zip_member", fake_fetch)


def test_invalid_timestamp_logs_warning(monkeypatch, capsys):
    _fake_fetch_returning_content(
        monkeypatch,
        "post-build=X/Y/Z:14/A/B:1:user/release-keys\npost-timestamp=not-a-number\n",
    )
    result = get_ota_metadata("https://x/y.zip")
    captured = capsys.readouterr()
    assert result is not None
    assert "post_timestamp" in result
    assert "build_date" not in result  # not parsed
    # Log.w writes to stdout with ANSI yellow prefix; the message text is plain.
    assert "Could not parse post-timestamp" in captured.out


def test_valid_timestamp_builds_date(monkeypatch, capsys):
    _fake_fetch_returning_content(
        monkeypatch,
        "post-build=X/Y/Z:14/A/B:1:user/release-keys\npost-timestamp=1700000000\n",
    )
    result = get_ota_metadata("https://x/y.zip")
    assert result is not None
    assert "post_timestamp" in result
    assert "build_date" in result


def test_missing_description_defaults_to_placeholder(tmp_path, monkeypatch):
    """A response without update_description must not render a literal None."""
    import argparse
    from pathlib import Path

    from checkota import processor
    from checkota.manager import Config
    from checkota.runtime import RunContext

    cfg = Config(
        oem="Infinix",
        product="X6873-OP",
        device="Infinix-X6873",
        android_version="16",
        build_tag="BP2A.250605.031.A3",
        incremental="1",
        model="Infinix GT 30 Pro",
        region="OP",
    )
    ctx = RunContext(
        env={},
        processed_path=Path(tmp_path) / "processed_updates.txt",
        processed_titles=set(),
        dry_run=False,
    )
    args = argparse.Namespace(
        dry_run=False,
        register_update=False,
        update_incremental=False,
        force_notify=False,
        gen_fp=False,
        fp=None,
        incremental=None,
        imei=None,
        debug=False,
    )
    monkeypatch.setattr(
        processor,
        "_check_for_updates",
        lambda *a, **k: (
            0,
            {
                "device": cfg.model,
                "found": True,
                "title": "TITLE",
                "url": "https://android.googleapis.com/packages/ota/x.zip",
                "size": "1 GB",
                "description": None,
            },
        ),
    )
    monkeypatch.setattr(
        processor,
        "_resolve_target_metadata",
        lambda *a, **k: (
            0,
            processor._TargetMetadata(
                fingerprint=(
                    "Infinix/X6873-OP/Infinix-X6873:16/BP2A.250605.031.A3/2"
                    ":user/release-keys"
                ),
                sdk_message=None,
                post_build_incremental="2",
                post_security_patch_level=None,
                build_date=None,
                post_sdk_level=None,
                android_version=None,
            ),
        ),
    )

    status, update = processor.collect_update_info(
        ctx, cfg, Path(tmp_path) / "config-X6873.yml", args
    )

    assert status == 0
    assert update is not None
    assert update.desc == "No description"
