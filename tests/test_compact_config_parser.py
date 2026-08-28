import argparse
from pathlib import Path

import pytest

from checkota.manager import Config, parse_fingerprint
from checkota.processor import _debug_label, load_config_regions


def _load(tmp_path: Path, content: str) -> list[Config]:
    path = tmp_path / "config.yml"
    path.write_text(content, encoding="utf-8")
    return Config.from_yaml(path)


def test_scalar_regions_resolve_in_yaml_order(tmp_path):
    configs = _load(
        tmp_path,
        """\
oem: "Infinix"
product_base: "X6873"
model: "Infinix GT 30 Pro"
android_version: "16"
regions:
  OP: "201500011"
  IN: "201500012"
""",
    )

    assert [(cfg.region, cfg.product, cfg.incremental) for cfg in configs] == [
        ("OP", "X6873-OP", "201500011"),
        ("IN", "X6873-IN", "201500012"),
    ]
    assert configs[0].device == "Infinix-X6873"
    assert configs[0].build_tag == "BP2A.250605.031.A3"


def test_region_filter_uses_full_multi_part_region_code(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(
        """\
oem: "TECNO"
product_base: "CN7c"
model: "Model"
android_version: "16"
regions:
  OP: "OP-BUILD"
  OP-M1: "M1-BUILD"
""",
        encoding="utf-8",
    )

    status, configs = load_config_regions(
        path, argparse.Namespace(region="op-m1", incremental=None)
    )

    assert status == 0
    assert [(config.region, config.product) for config in configs] == [
        ("OP-M1", "CN7c-OP-M1")
    ]


def test_debug_labels_keep_region_and_direct_fingerprint_names():
    region = Config(
        oem="Infinix",
        product="X6873-OP",
        device="Infinix-X6873",
        android_version="16",
        build_tag="BP2A.250605.031.A3",
        incremental="BUILD",
        model="Model",
        region="OP",
    )
    direct = Config(
        oem="Infinix",
        product="X6873-OP",
        device="Infinix-X6873",
        android_version="16",
        build_tag="BP2A.250605.031.A3",
        incremental="BUILD",
        model="Model",
    )

    assert (
        _debug_label(Path("configs/config-X6873.yml"), region)
        == "config-X6873-OP"
    )
    assert _debug_label(Path("<fingerprint>"), direct) == "<fingerprint>"


def test_expanded_region_can_override_android_version(tmp_path):
    config = _load(
        tmp_path,
        """\
oem: "Infinix"
product_base: "X6873"
model: "Infinix GT 30 Pro"
android_version: "16"
regions:
  EU:
    android_version: "15"
    incremental: "131015"
""",
    )[0]

    assert config.android_version == "15"
    assert config.build_tag == "AP3A.240905.015.A2"
    assert config.product == "X6873-EU"


def test_expanded_region_can_override_product_base(tmp_path):
    config = _load(
        tmp_path,
        """\
oem: "Infinix"
product_base: "X6857"
model: "Infinix NOTE 50X 5G"
android_version: "16"
regions:
  IN:
    product_base: "X6857B"
    incremental: "201500018"
""",
    )[0]

    assert config.product == "X6857B-IN"
    assert config.device == "Infinix-X6857B"


def test_expanded_region_can_override_build_tag(tmp_path):
    config = _load(
        tmp_path,
        """\
oem: "Infinix"
product_base: "X1301"
model: "Infinix XPAD GT"
android_version: "14"
regions:
  OP:
    build_tag: "UKQ1.240826.001"
    incremental: "251218V74"
""",
    )[0]

    assert config.build_tag == "UKQ1.240826.001"


def test_itel_device_prefix_is_derived(tmp_path):
    config = _load(
        tmp_path,
        """\
oem: "Itel"
product_base: "A671N"
model: "itel A95 5G"
android_version: "14"
regions:
  IN: "250523V548"
""",
    )[0]

    assert config.product == "A671N-IN"
    assert config.device == "itel-A671N"


def test_multi_part_region_is_preserved(tmp_path):
    config = _load(
        tmp_path,
        """\
oem: "TECNO"
product_base: "CN7c"
model: "TECNO CAMON"
android_version: "16"
regions:
  OP-M1: "201500068"
""",
    )[0]

    assert config.region == "OP-M1"
    assert config.product == "CN7c-OP-M1"
    assert config.fingerprint().startswith("TECNO/CN7c-OP-M1/TECNO-CN7c:")


def test_resolved_fingerprint_round_trips(tmp_path):
    config = _load(
        tmp_path,
        """\
oem: "Infinix"
product_base: "X6873"
model: "Infinix GT 30 Pro"
android_version: "16"
regions:
  OP: "201500011"
""",
    )[0]

    parsed = parse_fingerprint(config.fingerprint())
    assert parsed == {
        "oem": config.oem,
        "product": config.product,
        "device": config.device,
        "android_version": config.android_version,
        "build_tag": config.build_tag,
        "incremental": config.incremental,
    }


def test_empty_regions_mapping_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="regions.*non-empty"):
        _load(
            tmp_path,
            """\
oem: "Infinix"
product_base: "X1"
model: "Model"
android_version: "16"
regions: {}
""",
        )


@pytest.mark.parametrize("regions", ["[]", "null"])
def test_regions_must_be_a_mapping(tmp_path, regions):
    with pytest.raises(TypeError, match="regions.*mapping"):
        _load(
            tmp_path,
            f'''\
oem: "Infinix"
product_base: "X1"
model: "Model"
android_version: "16"
regions: {regions}
''',
        )


@pytest.mark.parametrize(
    "regions",
    [
        # Flow-style `regions` container.
        '{OP: "I"}',
        '{ "OP": "I" }',
        # Flow-style region value. The line-oriented updater cannot express
        # these, so accepting them at load time would produce a config that
        # reads fine but can never be updated.
        '\n  OP: {incremental: "I"}',
        '\n  OP: {incremental: "I", android_version: "16"}',
        '\n  OP: {\n    incremental: "I"\n  }',
    ],
)
def test_flow_style_collections_are_rejected(tmp_path, regions):
    with pytest.raises(ValueError, match="flow-style"):
        _load(
            tmp_path,
            f'''\
oem: "Infinix"
product_base: "X1"
model: "Model"
android_version: "16"
regions: {regions}
''',
        )


@pytest.mark.parametrize(
    "config",
    [
        # Multi-line quoted scalar anywhere in the file: its continuation lines
        # are indistinguishable from keys to the line-oriented updater, which
        # would otherwise mis-target the block it rewrites.
        (
            'oem: "Infinix"\nproduct_base: "X1"\n'
            'model: "Line one\nregions:\n  ZZ: nope"\n'
            'android_version: "16"\nregions:\n  OP: "I"\n'
        ),
        (
            'oem: "Infinix"\nproduct_base: "X1"\nmodel: "Model"\n'
            'android_version: "16"\nregions:\n  OP: "I\n    continued"\n'
        ),
        # Multi-line plain scalar.
        (
            'oem: "Infinix"\nproduct_base: "X1"\nmodel: Model\n  continued\n'
            'android_version: "16"\nregions:\n  OP: "I"\n'
        ),
    ],
)
def test_multi_line_scalars_are_rejected(tmp_path, config):
    with pytest.raises(ValueError, match="multi-line scalar"):
        _load(tmp_path, config)


@pytest.mark.parametrize(
    "regions_key",
    ["? regions\n:", "!!str regions:", "!<tag:yaml.org,2002:str> regions:"],
)
def test_unsupported_mapping_key_layouts_are_rejected(tmp_path, regions_key):
    with pytest.raises(ValueError, match="mapping key source layout"):
        _load(
            tmp_path,
            f'''\
oem: "Infinix"
product_base: "X1"
model: "Model"
android_version: "16"
{regions_key}
  OP: "I"
''',
        )


def test_named_tag_handle_on_mapping_key_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="mapping key source layout"):
        _load(
            tmp_path,
            """\
%TAG !e! tag:yaml.org,2002:
---
oem: "Infinix"
product_base: "X1"
model: "Model"
android_version: "16"
!e!str regions:
  OP: "I"
""",
        )


@pytest.mark.parametrize(
    ("region_value", "message"),
    [("&base \"I\"", "anchor"), ("*base", "alias")],
)
def test_yaml_anchors_and_aliases_are_rejected(tmp_path, region_value, message):
    with pytest.raises(ValueError, match=message):
        _load(
            tmp_path,
            f'''\
oem: "Infinix"
product_base: "X1"
model: "Model"
android_version: "16"
regions:
  OP: {region_value}
''',
        )


def test_missing_regions_mapping_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="missing required key.*regions"):
        _load(
            tmp_path,
            """\
oem: "Infinix"
product_base: "X1"
model: "Model"
android_version: "16"
""",
        )


def test_duplicate_region_key_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="duplicate key 'OP'"):
        _load(
            tmp_path,
            """\
oem: "Infinix"
product_base: "X1"
model: "Model"
android_version: "16"
regions:
  OP: "first"
  OP: "second"
""",
        )


def test_duplicate_top_level_key_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="duplicate key 'oem'"):
        _load(
            tmp_path,
            """\
oem: "Infinix"
oem: "TECNO"
product_base: "X1"
model: "Model"
android_version: "16"
regions:
  OP: "I"
""",
        )


def test_scalar_incremental_must_be_a_string(tmp_path):
    with pytest.raises(TypeError, match="region 'OP'.*string"):
        _load(
            tmp_path,
            """\
oem: "Infinix"
product_base: "X1"
model: "Model"
android_version: "16"
regions:
  OP: 123
""",
        )


@pytest.mark.parametrize(
    ("field", "quoted_value"),
    [
        ("oem", '"Infinix"'),
        ("product_base", '"X1"'),
        ("model", '"Model"'),
        ("android_version", '"16"'),
    ],
)
def test_top_level_values_must_be_strings(tmp_path, field, quoted_value):
    content = '''\
oem: "Infinix"
product_base: "X1"
model: "Model"
android_version: "16"
regions:
  OP: "I"
'''.replace(f"{field}: {quoted_value}", f"{field}: 123")

    with pytest.raises(TypeError, match=rf"{field} must be a string"):
        _load(tmp_path, content)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("product_base", "123"),
        ("android_version", "16"),
        ("build_tag", "123"),
        ("incremental", "123"),
    ],
)
def test_expanded_region_values_must_be_strings(tmp_path, field, value):
    region_fields = f"    {field}: {value}\n"
    if field != "incremental":
        region_fields += '    incremental: "I"\n'

    with pytest.raises(TypeError, match=rf"region 'OP'.*{field} must be a string"):
        _load(
            tmp_path,
            f'''\
oem: "Infinix"
product_base: "X1"
model: "Model"
android_version: "16"
regions:
  OP:
{region_fields}''',
        )


@pytest.mark.parametrize(
    "content",
    [
        """\
oem: ""
product_base: "X1"
model: "Model"
android_version: "16"
regions:
  OP: "I"
""",
        """\
oem: "Infinix"
product_base: "X1"
model: "Model"
android_version: "16"
regions:
  OP: ""
""",
        """\
oem: "Infinix"
product_base: "X1"
model: "Model"
android_version: "16"
regions:
  OP:
    build_tag: ""
    incremental: "I"
""",
    ],
)
def test_empty_config_values_are_rejected(tmp_path, content):
    with pytest.raises(ValueError, match="must not be empty"):
        _load(tmp_path, content)


@pytest.mark.parametrize(
    "content",
    [
        """\
oem: "Infinix\\u0007"
product_base: "X1"
model: "Model"
android_version: "16"
regions:
  OP: "I"
""",
        """\
oem: "Infinix"
product_base: "X1"
model: "Model"
android_version: "16"
regions:
  OP: "I\\u007f"
""",
    ],
)
def test_control_characters_in_config_values_are_rejected(tmp_path, content):
    with pytest.raises(ValueError, match="control characters"):
        _load(tmp_path, content)


@pytest.mark.parametrize(
    "content",
    [
        """\
oem: " Infinix"
product_base: "X1"
model: "Model"
android_version: "16"
regions:
  OP: "I"
""",
        """\
oem: "Infinix"
product_base: "X1"
model: "Model"
android_version: "16"
regions:
  OP: "I "
""",
    ],
)
def test_padded_config_values_are_rejected(tmp_path, content):
    with pytest.raises(ValueError, match="leading or trailing whitespace"):
        _load(tmp_path, content)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("oem", "Bad/OEM"),
        ("product_base", "X:1"),
        ("android_version", "16/foo"),
    ],
)
def test_top_level_fingerprint_delimiters_are_rejected(tmp_path, field, value):
    content = '''\
oem: "Infinix"
product_base: "X1"
model: "Model"
android_version: "16"
regions:
  OP: "I"
'''
    old_value = {
        "oem": "Infinix",
        "product_base": "X1",
        "android_version": "16",
    }[field]

    with pytest.raises(ValueError, match=rf"{field} must not contain"):
        _load(tmp_path, content.replace(old_value, value, 1))


@pytest.mark.parametrize(
    ("field", "value"),
    [("build_tag", "BAD/TAG"), ("incremental", "I:2")],
)
def test_region_fingerprint_delimiters_are_rejected(tmp_path, field, value):
    valid_value = {"build_tag": "CUSTOM.TAG", "incremental": "I"}[field]
    with pytest.raises(ValueError, match=rf"region 'OP'.*{field} must not contain"):
        _load(
            tmp_path,
            '''\
oem: "Infinix"
product_base: "X1"
model: "Model"
android_version: "16"
regions:
  OP:
    build_tag: "CUSTOM.TAG"
    incremental: "I"
'''.replace(f'{field}: "{valid_value}"', f'{field}: "{value}"'),
        )


def test_hyphenated_product_base_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="product_base must not contain"):
        _load(
            tmp_path,
            """\
oem: "Infinix"
product_base: "X-1"
model: "Model"
android_version: "16"
regions:
  OP: "I"
""",
        )


@pytest.mark.parametrize(
    "region_key",
    [
        '""',
        '" OP"',
        '"OP "',
        '"OP/IN"',
        '"OP:IN"',
        '"OP\\u0007"',
        "1",
        # The region code is concatenated into `product` and from there into the
        # check-in fingerprint, so it is restricted to `[A-Z0-9-]`.
        '"OP M1"',
        '"OP#1"',
        '"OP.1"',
        '"OP_1"',
        '"OPÄ"',
        '"-OP"',
    ],
)
def test_invalid_region_keys_are_rejected(tmp_path, region_key):
    with pytest.raises((TypeError, ValueError), match="region .* key"):
        _load(
            tmp_path,
            f'''\
oem: "Infinix"
product_base: "X1"
model: "Model"
android_version: "16"
regions:
  {region_key}: "I"
''',
        )


def test_region_keys_must_be_uppercase(tmp_path):
    with pytest.raises(ValueError, match="region 'op'.*uppercase"):
        _load(
            tmp_path,
            """\
oem: "Infinix"
product_base: "X1"
model: "Model"
android_version: "16"
regions:
  op: "I"
""",
        )


def test_expanded_region_requires_incremental(tmp_path):
    with pytest.raises(ValueError, match="region 'OP'.*requires 'incremental'"):
        _load(
            tmp_path,
            """\
oem: "Infinix"
product_base: "X1"
model: "Model"
android_version: "16"
regions:
  OP:
    android_version: "16"
""",
        )


@pytest.mark.parametrize(
    "content",
    [
        """\
oem: "Infinix"
product_base: "X1"
model: "Model"
android_version: "16"
unknown: "value"
regions:
  OP: "I"
""",
        """\
oem: "Infinix"
product_base: "X1"
model: "Model"
android_version: "16"
regions:
  OP:
    variant: "Global"
    incremental: "I"
""",
    ],
)
def test_unknown_keys_are_rejected(tmp_path, content):
    with pytest.raises(ValueError, match="unknown"):
        _load(tmp_path, content)


def test_unknown_android_version_requires_region_build_tag(tmp_path):
    with pytest.raises(ValueError, match="Config .* region 'OP'.*build_tag"):
        _load(
            tmp_path,
            """\
oem: "Infinix"
product_base: "X1"
model: "Model"
android_version: "19"
regions:
  OP: "I"
""",
        )


def test_unknown_android_version_accepts_explicit_region_build_tag(tmp_path):
    config = _load(
        tmp_path,
        """\
oem: "Infinix"
product_base: "X1"
model: "Model"
android_version: "19"
regions:
  OP:
    build_tag: "CUSTOM.TAG"
    incremental: "I"
""",
    )[0]

    assert config.android_version == "19"
    assert config.build_tag == "CUSTOM.TAG"


@pytest.mark.parametrize("style", ["|", ">"])
def test_block_scalar_region_values_are_rejected(tmp_path, style):
    with pytest.raises(ValueError, match="literal/folded block scalar"):
        _load(
            tmp_path,
            f'''\
oem: "Infinix"
product_base: "X1"
model: "Model"
android_version: "16"
regions:
  OP: {style}
    I
''',
        )


def test_canonical_region_build_tag_is_accepted(tmp_path):
    config = _load(
        tmp_path,
        """\
oem: "Infinix"
product_base: "X1"
model: "Model"
android_version: "16"
regions:
  OP:
    build_tag: "BP2A.250605.031.A3"
    incremental: "I"
""",
    )[0]

    assert config.build_tag == "BP2A.250605.031.A3"


def test_legacy_variants_schema_is_rejected_with_migration_error(tmp_path):
    with pytest.raises(ValueError, match="legacy 'variants'.*'regions'"):
        _load(
            tmp_path,
            """\
oem: "Infinix"
product_base: "X1"
model: "Model"
variants:
  - variant: "Global"
    product: "X1-OP"
    device: "Infinix-X1"
    android_version: "16"
    build_tag: "BP2A.250605.031.A3"
    incremental: "I"
""",
        )


def test_legacy_single_region_schema_has_migration_error(tmp_path):
    with pytest.raises(ValueError, match="legacy single-region.*product_base.*regions"):
        _load(
            tmp_path,
            """\
oem: "Infinix"
product: "X1-OP"
device: "Infinix-X1"
model: "Model"
android_version: "16"
build_tag: "BP2A.250605.031.A3"
incremental: "I"
""",
        )
