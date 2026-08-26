from pathlib import Path

import yaml

from checkota.constants import BUILD_TAG_BY_ANDROID
from checkota.manager import Config


def test_all_repository_configs_use_compact_schema_and_resolve_expected_count():
    config_dir = Path(__file__).parents[1] / "configs"
    paths = sorted(config_dir.glob("config-*.yml"))

    documents = [yaml.safe_load(path.read_text(encoding="utf-8")) for path in paths]
    configs = [config for path in paths for config in Config.from_yaml(path)]

    assert len(paths) == 114
    assert len(configs) == 148
    assert all(config.variant is not None for config in configs)
    assert all(config.variant_index is None for config in configs)
    assert all(list(document) == [
        "oem", "product_base", "model", "android_version", "regions"
    ] for document in documents)
    assert all(
        not ({"variants", "product", "device"} & document.keys())
        for document in documents
    )
    build_tag_overrides = [
        region["build_tag"]
        for document in documents
        for region in document["regions"].values()
        if isinstance(region, dict) and "build_tag" in region
    ]
    assert build_tag_overrides == ["AQ3A.250226.002", "UKQ1.240826.001"]
    assert not set(build_tag_overrides) & set(BUILD_TAG_BY_ANDROID.values())

    expected_identities = []
    for document in documents:
        for region_code, region in document["regions"].items():
            overrides = region if isinstance(region, dict) else {}
            product_base = overrides.get("product_base", document["product_base"])
            device_prefix = "itel" if document["oem"] == "Itel" else document["oem"]
            expected_identities.append(
                (
                    region_code,
                    f"{product_base}-{region_code}",
                    f"{device_prefix}-{product_base}",
                )
            )

    assert [
        (config.variant, config.product, config.device) for config in configs
    ] == expected_identities
