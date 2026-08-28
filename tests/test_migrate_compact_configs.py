import os
import runpy
import stat
from contextlib import contextmanager

import pytest
import yaml

_MIGRATOR = runpy.run_path("scripts/migrate_compact_configs.py")
LegacyRegion = _MIGRATOR["LegacyRegion"]
config_paths = _MIGRATOR["config_paths"]
load_legacy_regions = _MIGRATOR["load_legacy_regions"]
migrate_text = _MIGRATOR["migrate_text"]
migrate_file = _MIGRATOR["migrate_file"]
migrate_directory = _MIGRATOR["migrate_directory"]


def _legacy_config(
    region: str = "OP", incremental: str = "1", newline: str = "\n"
) -> bytes:
    text = f"""\
oem: "TECNO"
product: "T1-{region}"
device: "TECNO-T1"
model: "Example"
android_version: "15"
build_tag: "AP3A.240905.015.A2"
incremental: "{incremental}"
"""
    return text.replace("\n", newline).encode()


def _compact_config(
    region: str = "OP", incremental: str = "1", newline: str = "\n"
) -> bytes:
    text = f'''\
# already compact; preserve this comment and quoting
oem: "TECNO"
product_base: "T1"
model: "Example"
android_version: "15"
regions:
  {region}: "{incremental}"
'''
    return text.replace("\n", newline).encode()


def test_legacy_variants_are_merged_and_region_order_is_preserved(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(
        """\
oem: "Infinix"
device: "Infinix-X1"
model: "Example Phone"
variants:
  - product: "X1-OP"
    android_version: "16"
    build_tag: "BP2A.250605.031.A3"
    incremental: "OP"
  - product: "X1-EU"
    android_version: "15"
    build_tag: "AP3A.240905.015.A2"
    incremental: "EU"
""",
        encoding="utf-8",
    )

    regions = load_legacy_regions(path)
    output = migrate_text(regions)
    parsed = yaml.safe_load(output)

    assert list(parsed["regions"]) == ["OP", "EU"]
    assert parsed["regions"]["OP"] == "OP"
    assert parsed["regions"]["EU"] == {
        "android_version": "15",
        "incremental": "EU",
    }


def test_migration_keeps_only_required_overrides_and_special_cases():
    regions = [
        LegacyRegion(
            oem="Infinix",
            product="X6857-OP",
            product_base="X6857",
            region="OP",
            device="Infinix-X6857",
            model="NOTE 50X",
            android_version="16",
            build_tag="BP2A.250605.031.A3",
            incremental="OP",
        ),
        LegacyRegion(
            oem="Infinix",
            product="X6857B-IN",
            product_base="X6857B",
            region="IN",
            device="Infinix-X6857B",
            model="NOTE 50X",
            android_version="16",
            build_tag="BP2A.250605.031.A3",
            incremental="IN",
        ),
        LegacyRegion(
            oem="Infinix",
            product="X6857-RU",
            product_base="X6857",
            region="RU",
            device="Infinix-X6857",
            model="NOTE 50X",
            android_version="16",
            build_tag="CUSTOM.TAG",
            incremental="RU",
        ),
    ]

    parsed = yaml.safe_load(migrate_text(regions))

    assert parsed["product_base"] == "X6857"
    assert parsed["regions"]["OP"] == "OP"
    assert parsed["regions"]["IN"] == {
        "product_base": "X6857B",
        "incremental": "IN",
    }
    assert parsed["regions"]["RU"] == {
        "build_tag": "CUSTOM.TAG",
        "incremental": "RU",
    }
    assert migrate_text(regions).count("build_tag:") == 1


def test_migrated_scalar_values_are_quoted():
    region = LegacyRegion(
        oem="TECNO",
        product="T1-OP",
        product_base="T1",
        region="OP",
        device="TECNO-T1",
        model="Example",
        android_version="15",
        build_tag="AP3A.240905.015.A2",
        incremental="001: #value",
    )

    output = migrate_text([region])

    assert 'oem: "TECNO"' in output
    assert 'product_base: "T1"' in output
    assert 'OP: "001: #value"' in output


def test_migration_rejects_unrepresentable_identity_and_duplicate_regions():
    base = {
        "oem": "Infinix",
        "product": "X1-OP",
        "product_base": "X1",
        "region": "OP",
        "device": "wrong-X1",
        "model": "Example",
        "android_version": "15",
        "build_tag": "AP3A.240905.015.A2",
        "incremental": "1",
    }

    with pytest.raises(ValueError, match="cannot be derived"):
        migrate_text([LegacyRegion(**base)])

    base["device"] = "Infinix-X1"
    with pytest.raises(ValueError, match="duplicate legacy region"):
        migrate_text([LegacyRegion(**base), LegacyRegion(**base)])


def test_yaml_ambiguous_region_keys_are_quoted():
    region = LegacyRegion(
        oem="TECNO",
        product="T1-NO",
        product_base="T1",
        region="NO",
        device="TECNO-T1",
        model="Example",
        android_version="15",
        build_tag="AP3A.240905.015.A2",
        incremental="1",
    )

    output = migrate_text([region])

    assert '  "NO": "1"' in output
    assert list(yaml.safe_load(output)["regions"]) == ["NO"]


def test_default_ties_use_first_region_and_expanded_keys_are_ordered():
    regions = [
        LegacyRegion(
            oem="TECNO",
            product="B-OP",
            product_base="B",
            region="OP",
            device="TECNO-B",
            model="Example",
            android_version="15",
            build_tag="CUSTOM",
            incremental="1",
        ),
        LegacyRegion(
            oem="TECNO",
            product="A-IN",
            product_base="A",
            region="IN",
            device="TECNO-A",
            model="Example",
            android_version="14",
            build_tag="UP1A.231005.007",
            incremental="2",
        ),
    ]

    output = migrate_text(regions)

    assert 'product_base: "B"' in output
    assert 'android_version: "15"' in output
    assert output.index('    build_tag: "CUSTOM"') < output.index(
        '    incremental: "1"'
    )
    assert output.index('    product_base: "A"') < output.index(
        '    android_version: "14"'
    )


def test_directory_write_is_byte_identical_on_second_migration(tmp_path):
    first = tmp_path / "first.yml"
    second = tmp_path / "second.yaml"
    first.write_bytes(_legacy_config(incremental="first", newline="\r\n"))
    second.write_bytes(_legacy_config(region="EU", incremental="second"))
    first.chmod(0o640)
    second.chmod(0o600)

    migrate_directory(tmp_path, write=True)
    migrated = {
        path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        for path in (first, second)
    }

    outputs = migrate_directory(tmp_path, write=True)

    assert outputs == {
        path: content.decode("utf-8")
        for path, (content, _) in migrated.items()
    }
    for path, (content, mode) in migrated.items():
        assert path.read_bytes() == content
        assert stat.S_IMODE(path.stat().st_mode) == mode
    assert set(tmp_path.iterdir()) == {first, second}


def test_mixed_directory_migrates_legacy_and_preserves_compact(tmp_path):
    legacy = tmp_path / "legacy.yml"
    compact = tmp_path / "compact.yaml"
    legacy.write_bytes(_legacy_config(incremental="legacy", newline="\r\n"))
    compact_content = _compact_config(incremental="compact", newline="\r\n")
    compact.write_bytes(compact_content)
    legacy.chmod(0o600)
    compact.chmod(0o640)

    outputs = migrate_directory(tmp_path, write=True)

    assert legacy.read_bytes() == outputs[legacy].encode("utf-8")
    assert b'regions:\n  OP: "legacy"' in legacy.read_bytes()
    assert compact.read_bytes() == compact_content
    assert outputs[compact].encode("utf-8") == compact_content
    assert stat.S_IMODE(legacy.stat().st_mode) == 0o600
    assert stat.S_IMODE(compact.stat().st_mode) == 0o640
    assert set(tmp_path.iterdir()) == {legacy, compact}


def test_single_compact_file_write_and_dry_run_are_noops(tmp_path):
    path = tmp_path / "compact.yml"
    content = _compact_config(newline="\r\n")
    path.write_bytes(content)
    path.chmod(0o640)

    assert migrate_file(path) == content.decode("utf-8")
    assert migrate_file(path, write=True) == content.decode("utf-8")
    assert path.read_bytes() == content
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_legacy_dry_run_returns_validated_output_without_publication(tmp_path):
    path = tmp_path / "legacy.yml"
    original = _legacy_config(incremental="dry-run", newline="\r\n")
    path.write_bytes(original)

    output = migrate_file(path)

    assert yaml.safe_load(output)["regions"]["OP"] == "dry-run"
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_legacy_dry_run_rejects_runtime_invalid_output_without_publication(tmp_path):
    path = tmp_path / "legacy.yml"
    original = _legacy_config(region="op", newline="\r\n")
    path.write_bytes(original)

    with pytest.raises(ValueError, match="uppercase"):
        migrate_file(path)

    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_compact_looking_invalid_document_fails_preflight(tmp_path):
    valid_legacy = tmp_path / "legacy.yml"
    invalid_compact = tmp_path / "invalid.yaml"
    legacy_content = _legacy_config()
    valid_legacy.write_bytes(legacy_content)
    invalid_compact.write_text(
        '''\
oem: "TECNO"
product_base: "T1"
model: "Example"
android_version: "15"
regions: []
''',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="regions.*mapping"):
        migrate_directory(tmp_path, write=True)

    assert valid_legacy.read_bytes() == legacy_content


def test_batch_preflight_writes_nothing_when_any_legacy_config_is_invalid(tmp_path):
    valid = tmp_path / "config-A.yml"
    invalid = tmp_path / "config-B.yml"
    valid_text = """\
oem: "TECNO"
product: "T1-OP"
device: "TECNO-T1"
model: "Example"
android_version: "15"
build_tag: "AP3A.240905.015.A2"
incremental: "1"
"""
    valid.write_text(valid_text, encoding="utf-8")
    invalid.write_text("oem: TECNO\noem: Infinix\n", encoding="utf-8")

    with pytest.raises(yaml.constructor.ConstructorError, match="duplicate key"):
        migrate_directory(tmp_path, write=True)

    assert valid.read_text(encoding="utf-8") == valid_text


def test_single_file_migration_locks_final_read_and_publication(
    tmp_path, monkeypatch
):
    path = tmp_path / "config.yml"
    path.write_bytes(_legacy_config(incremental="before-lock"))
    events = []

    @contextmanager
    def recording_lock(locked_path):
        events.append("acquire")
        path.write_bytes(_legacy_config(incremental="after-lock"))
        try:
            yield
        finally:
            events.append("release")

    migration_globals = migrate_file.__globals__
    real_load = migration_globals["load_legacy_regions"]
    real_replace = migration_globals["os"].replace

    def recording_load(source_path):
        events.append("read")
        return real_load(source_path)

    def recording_replace(source, destination):
        events.append("publish")
        return real_replace(source, destination)

    monkeypatch.setitem(migration_globals, "_config_lock", recording_lock)
    monkeypatch.setitem(migration_globals, "load_legacy_regions", recording_load)
    monkeypatch.setattr(migration_globals["os"], "replace", recording_replace)

    output = migrate_file(path, write=True)

    assert events == ["acquire", "read", "publish", "release"]
    assert yaml.safe_load(output)["regions"]["OP"] == "after-lock"
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["regions"]["OP"] == (
        "after-lock"
    )


def test_batch_migration_locks_before_final_read_and_publication(
    tmp_path, monkeypatch
):
    first = tmp_path / "A-first.yaml"
    second = tmp_path / "b-second.yml"
    first.write_bytes(_legacy_config(incremental="first"))
    second.write_bytes(_legacy_config(region="EU", incremental="second"))

    events = []

    lock_count = 0

    @contextmanager
    def recording_lock(path):
        nonlocal lock_count
        events.append(("acquire", path.name))
        lock_count += 1
        if lock_count == 1:
            first.write_bytes(_legacy_config(incremental="runtime-update"))
        try:
            yield
        finally:
            events.append(("release", path.name))

    real_load = _MIGRATOR["load_legacy_regions"]

    def recording_load(path):
        events.append(("read", path.name))
        return real_load(path)

    migration_os = _MIGRATOR["os"]
    real_replace = migration_os.replace

    def recording_replace(source, destination):
        events.append(("publish", destination.name))
        return real_replace(source, destination)

    migration_globals = migrate_directory.__globals__
    monkeypatch.setitem(migration_globals, "_config_lock", recording_lock)
    monkeypatch.setitem(migration_globals, "load_legacy_regions", recording_load)
    monkeypatch.setattr(migration_os, "replace", recording_replace)

    migrate_directory(tmp_path, write=True)

    parsed_first = yaml.safe_load(first.read_text(encoding="utf-8"))
    assert parsed_first["regions"]["OP"] == "runtime-update"
    acquisitions = [name for kind, name in events if kind == "acquire"]
    assert acquisitions == [first.name, second.name]
    last_acquire = max(
        index for index, (kind, _) in enumerate(events) if kind == "acquire"
    )
    final_reads = [
        index
        for index, (kind, _) in enumerate(events)
        if kind == "read" and index > last_acquire
    ]
    publications = [
        index for index, (kind, _) in enumerate(events) if kind == "publish"
    ]
    releases = [
        (index, name)
        for index, (kind, name) in enumerate(events)
        if kind == "release"
    ]

    assert len(final_reads) == 2
    assert publications
    assert max(final_reads) < min(publications)
    assert max(publications) < min(index for index, _ in releases)
    assert [name for _, name in releases] == [second.name, first.name]


def test_config_paths_discovers_custom_yaml_names_in_cli_order(tmp_path):
    alpha = tmp_path / "Alpha-device.yaml"
    zulu = tmp_path / "zulu-device.yml"
    alpha.write_text("", encoding="utf-8")
    zulu.write_text("", encoding="utf-8")
    (tmp_path / "ignored.YML").write_text("", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("", encoding="utf-8")
    (tmp_path / "directory.yml").mkdir()

    assert config_paths(tmp_path) == [alpha, zulu]


def test_runtime_validation_happens_before_publication(tmp_path):
    path = tmp_path / "custom-device.yaml"
    original = _legacy_config(region="op", newline="\r\n")
    path.write_bytes(original)
    path.chmod(0o640)
    original_mode = stat.S_IMODE(path.stat().st_mode)

    with pytest.raises(ValueError, match="uppercase"):
        migrate_directory(tmp_path, write=True)

    assert path.read_bytes() == original
    assert stat.S_IMODE(path.stat().st_mode) == original_mode
    assert list(tmp_path.iterdir()) == [path]


def test_later_replace_failure_rolls_back_and_allows_rerun(tmp_path, monkeypatch):
    first = tmp_path / "A-first.yaml"
    second = tmp_path / "b-second.yml"
    compact = tmp_path / "c-compact.yaml"
    originals = {
        first: _legacy_config(incremental="first", newline="\r\n"),
        second: _legacy_config(region="EU", incremental="second"),
        compact: _compact_config(incremental="unchanged", newline="\r\n"),
    }
    modes = {first: 0o640, second: 0o600, compact: 0o644}
    for path, content in originals.items():
        path.write_bytes(content)
        path.chmod(modes[path])

    real_replace = os.replace
    replace_calls = 0

    def fail_second_replace(source, destination):
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise OSError("simulated later replacement failure")
        return real_replace(source, destination)

    monkeypatch.setattr(_MIGRATOR["os"], "replace", fail_second_replace)

    with pytest.raises(OSError, match="simulated later replacement failure"):
        migrate_directory(tmp_path, write=True)

    for path, content in originals.items():
        assert path.read_bytes() == content
        assert stat.S_IMODE(path.stat().st_mode) == modes[path]
    assert set(tmp_path.iterdir()) == set(originals)

    outputs = migrate_directory(tmp_path, write=True)

    assert list(outputs) == [first, second, compact]
    for path, output in outputs.items():
        assert path.read_bytes() == output.encode()
        assert stat.S_IMODE(path.stat().st_mode) == modes[path]
    assert set(tmp_path.iterdir()) == set(originals)


def test_publication_and_rollback_failures_are_both_reported(tmp_path, monkeypatch):
    first = tmp_path / "first.yml"
    second = tmp_path / "second.yaml"
    first.write_bytes(_legacy_config(incremental="first"))
    second.write_bytes(_legacy_config(region="EU", incremental="second"))

    real_replace = os.replace
    replace_calls = 0

    def fail_publication_and_rollback(source, destination):
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise OSError("publication exploded")
        if replace_calls == 3:
            raise PermissionError("rollback exploded")
        return real_replace(source, destination)

    monkeypatch.setattr(
        _MIGRATOR["os"], "replace", fail_publication_and_rollback
    )

    with pytest.raises(RuntimeError) as error:
        migrate_directory(tmp_path, write=True)

    assert "publication exploded" in str(error.value)
    assert "rollback exploded" in str(error.value)
    assert isinstance(error.value.__cause__, OSError)

    remaining = set(tmp_path.iterdir())
    retained = remaining - {first, second}
    assert len(retained) == 1
    backup = retained.pop()
    assert str(backup) in str(error.value)
    assert backup.read_bytes() == _legacy_config(incremental="first")
    assert stat.S_IMODE(backup.stat().st_mode) == stat.S_IMODE(first.stat().st_mode)
    assert second.read_bytes() == _legacy_config(region="EU", incremental="second")
