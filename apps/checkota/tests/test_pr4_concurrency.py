"""L1 fix — identity generators are pinned in __init__ and stable across retries."""

from unittest.mock import MagicMock

from modules.paths import ensure_vendor_on_path

ensure_vendor_on_path()  # ensure the vendored `checkin` package is on sys.path

from modules.manager import Config
from modules.update_checker import UpdateChecker


def _cfg():
    return Config(
        oem="Infinix",
        product="X6873-OP",
        device="Infinix-X6873",
        android_version="14",
        build_tag="B",
        incremental="I",
        model="Infinix GT 30 Pro",
    )


def test_l1_identity_persists_across_rebuilds():
    """The same UpdateChecker instance must produce identical identity bytes
    across multiple _build_request() calls (i.e. across retries)."""
    checker = UpdateChecker(_cfg(), session=MagicMock())
    identity_before = (
        checker._imei,
        checker._digest,
        checker._serial,
        checker._mac,
    )
    # Call _build_request multiple times; assert the pinned identity attrs are
    # unchanged. We do NOT compare the gzipped payload bytes because gzip
    # headers may include a timestamp on some Python versions.
    for _ in range(3):
        checker._build_request()
    identity_after = (
        checker._imei,
        checker._digest,
        checker._serial,
        checker._mac,
    )
    assert identity_before == identity_after


def test_l1_custom_imei_overrides_generator():
    """When --imei is supplied, the custom IMEI must be used (not regenerated)."""
    checker = UpdateChecker(_cfg(), session=MagicMock(), imei="123456789012345")
    assert checker._imei == "123456789012345"
