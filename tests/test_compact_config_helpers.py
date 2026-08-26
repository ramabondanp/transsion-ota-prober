import pytest

from checkota.manager import derive_product_and_device, resolve_build_tag


@pytest.mark.parametrize(
    ("android_version", "expected"),
    [
        ("13", "TP1A.220624.014"),
        ("14", "UP1A.231005.007"),
        ("15", "AP3A.240905.015.A2"),
        ("16", "BP2A.250605.031.A3"),
    ],
)
def test_resolve_build_tag_uses_canonical_mapping(android_version, expected):
    assert resolve_build_tag(android_version) == expected


def test_resolve_build_tag_explicit_override_wins():
    assert resolve_build_tag("14", "NONSTANDARD.TAG") == "NONSTANDARD.TAG"


def test_resolve_build_tag_unknown_android_requires_override():
    with pytest.raises(ValueError, match="provide an explicit build_tag"):
        resolve_build_tag("19")


def test_resolve_build_tag_unknown_android_accepts_override():
    assert resolve_build_tag("19", "CUSTOM.TAG") == "CUSTOM.TAG"


def test_derive_product_and_device_for_regular_oem():
    assert derive_product_and_device("Infinix", "X6873", "OP") == (
        "X6873-OP",
        "Infinix-X6873",
    )


def test_derive_product_and_device_preserves_multi_part_region():
    assert derive_product_and_device("TECNO", "CN7c", "OP-M1") == (
        "CN7c-OP-M1",
        "TECNO-CN7c",
    )


def test_derive_product_and_device_uses_itel_prefix():
    assert derive_product_and_device("Itel", "A671N", "IN") == (
        "A671N-IN",
        "itel-A671N",
    )


def test_derive_product_and_device_falls_back_to_other_oem_name():
    assert derive_product_and_device("NewOEM", "D1", "GL") == (
        "D1-GL",
        "NewOEM-D1",
    )
