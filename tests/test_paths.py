"""Tests for source, wheel, XDG, and copy-once path behavior."""

from __future__ import annotations

import errno
import io
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from checkota import paths


class _Resource:
    def __init__(self, name: str, content: bytes):
        self.name = name
        self._content = content

    def is_file(self) -> bool:
        return True

    def open(self, mode: str):
        assert mode == "rb"
        return io.BytesIO(self._content)


class _ResourceRoot:
    def __init__(self, *resources):
        self._resources = resources

    def iterdir(self):
        return iter(self._resources)


def test_source_paths_remain_repository_local():
    assert paths.IS_SOURCE_CHECKOUT
    assert paths.APP_CONFIGS_DIR == paths.PROJECT_ROOT / "configs"
    assert paths.STATE_DIR == paths.PROJECT_ROOT
    assert paths.VENDOR_DIR == paths.PROJECT_ROOT / "vendor" / "google-ota-prober"


def test_relative_xdg_values_use_absolute_home_fallback(monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/config")
    monkeypatch.setenv("XDG_STATE_HOME", "relative/state")

    config_home = paths._xdg_home("XDG_CONFIG_HOME", ".config")
    state_home = paths._xdg_home("XDG_STATE_HOME", ".local/state")

    assert config_home == (Path.home() / ".config").resolve()
    assert state_home == (Path.home() / ".local/state").resolve()


def test_wheel_seeds_only_missing_configs(monkeypatch, tmp_path):
    config_dir = tmp_path / "config" / "checkota" / "configs"
    existing = config_dir / "config-existing.yml"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"user-edited: true\n")
    monkeypatch.setattr(paths, "IS_SOURCE_CHECKOUT", False)
    monkeypatch.setattr(paths, "APP_CONFIGS_DIR", config_dir)
    monkeypatch.setattr(
        paths,
        "_resource_root",
        lambda: _ResourceRoot(
            _Resource("config-existing.yml", b"bundled: true\n"),
            _Resource("config-new.yml", b"new: true\n"),
            _Resource("not-a-config.txt", b"ignored\n"),
        ),
    )

    paths.ensure_config_resources()

    assert existing.read_bytes() == b"user-edited: true\n"
    assert (config_dir / "config-new.yml").read_bytes() == b"new: true\n"
    assert not (config_dir / "not-a-config.txt").exists()


@pytest.mark.parametrize("hardlinks", [True, False])
def test_wheel_config_seeding_is_safe_for_concurrent_first_use(
    monkeypatch, tmp_path, hardlinks
):
    if not hardlinks:
        def no_hardlinks(*args):
            raise OSError(errno.EOPNOTSUPP, "hardlinks unavailable")

        monkeypatch.setattr(paths.os, "link", no_hardlinks)
    config_dir = tmp_path / "configs"
    monkeypatch.setattr(paths, "IS_SOURCE_CHECKOUT", False)
    monkeypatch.setattr(paths, "APP_CONFIGS_DIR", config_dir)
    monkeypatch.setattr(
        paths,
        "_resource_root",
        lambda: _ResourceRoot(
            _Resource(
                "config-X6873.yml",
                (
                    b'oem: "Infinix"\nproduct_base: "X6873"\n'
                    b'model: "Infinix GT 30 Pro"\nandroid_version: "16"\n'
                    b'regions:\n  OP: "I"\n'
                ),
            ),
        ),
    )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: paths.ensure_config_resources(), range(8)))

    assert (config_dir / "config-X6873.yml").read_bytes() == (
        b'oem: "Infinix"\nproduct_base: "X6873"\n'
        b'model: "Infinix GT 30 Pro"\nandroid_version: "16"\n'
        b'regions:\n  OP: "I"\n'
    )
    assert list(config_dir.glob("*.tmp")) == []


def test_publish_does_not_overwrite_after_link_error(monkeypatch, tmp_path):
    source = tmp_path / "config-X6873.yml"
    source.write_bytes(b"bundled: true\n")
    destination = tmp_path / "configs" / "config-X6873.yml"

    def link_permission_denied(src, dst):
        raise OSError(errno.EACCES, "permission denied")

    monkeypatch.setattr(paths.os, "link", link_permission_denied)

    with pytest.raises(OSError):
        paths._publish_if_missing(source, destination, 0o644)
    assert not destination.exists()


def test_publish_falls_back_to_atomic_rename_without_hardlink_support(
    monkeypatch, tmp_path
):
    source = tmp_path / "config-X6873.yml"
    source.write_bytes(
        b'oem: "Infinix"\nproduct_base: "X6873"\n'
        b'model: "Infinix GT 30 Pro"\nandroid_version: "16"\n'
        b'regions:\n  OP: "I"\n'
    )
    destination = tmp_path / "configs" / "config-X6873.yml"

    def link_without_hardlink_support(src, dst):
        raise OSError(errno.EOPNOTSUPP, "operation not supported")

    monkeypatch.setattr(paths.os, "link", link_without_hardlink_support)

    assert paths._publish_if_missing(source, destination, 0o644) is True
    assert destination.read_bytes() == (
        b'oem: "Infinix"\nproduct_base: "X6873"\n'
        b'model: "Infinix GT 30 Pro"\nandroid_version: "16"\n'
        b'regions:\n  OP: "I"\n'
    )
    assert list(destination.parent.glob("*.tmp")) == []


@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
def test_publish_leaves_no_partial_destination_on_copy_failure(
    monkeypatch, tmp_path, failure
):
    source = tmp_path / "config-X6873.yml"
    source.write_bytes(b"x" * 100)
    destination = tmp_path / "configs" / "config-X6873.yml"

    def fail_copy(input_file, output_file):
        output_file.write(b"partial")
        output_file.flush()
        raise failure("simulated write failure")

    monkeypatch.setattr(paths.shutil, "copyfileobj", fail_copy)
    with pytest.raises(failure, match="simulated write failure"):
        paths._publish_if_missing(source, destination, 0o644)
    assert list(destination.parent.iterdir()) == []


def test_publish_preserves_user_file_on_copy_failure(monkeypatch, tmp_path):
    source = tmp_path / "config-X6873.yml"
    source.write_bytes(b"bundled config")
    destination = tmp_path / "configs" / "config-X6873.yml"
    replacement = tmp_path / "user-edited.yml"
    replacement.write_bytes(b"user config")

    def fail_copy(input_file, output_file):
        output_file.write(b"partial")
        output_file.flush()
        os.replace(replacement, destination)
        raise OSError("simulated write failure")

    monkeypatch.setattr(paths.shutil, "copyfileobj", fail_copy)
    with pytest.raises(OSError, match="simulated write failure"):
        paths._publish_if_missing(source, destination, 0o644)
    assert destination.read_bytes() == b"user config"
    assert list(destination.parent.iterdir()) == [destination]


def test_fallback_never_exposes_partial_destination(monkeypatch, tmp_path):
    source = tmp_path / "bundled.yml"
    source.write_bytes(b"bundled config")
    destination = tmp_path / "configs" / "config-X6873.yml"
    copies = []

    def no_hardlinks(*args):
        raise OSError(errno.EOPNOTSUPP, "hardlinks unavailable")

    def copy_in_chunks(input_file, output_file):
        data = input_file.read()
        output_file.write(data[:3])
        output_file.flush()
        assert not destination.exists(), "partial config became visible"
        output_file.write(data[3:])
        copies.append(1)

    monkeypatch.setattr(paths.os, "link", no_hardlinks)
    monkeypatch.setattr(paths.shutil, "copyfileobj", copy_in_chunks)
    assert paths._publish_if_missing(source, destination, 0o644)
    assert copies == [1]
    assert destination.read_bytes() == b"bundled config"
    assert list(destination.parent.iterdir()) == [destination]


@pytest.mark.parametrize("symlink", [False, True])
def test_fallback_preserves_concurrent_winner(monkeypatch, tmp_path, symlink):
    source = tmp_path / "bundled.yml"
    source.write_bytes(b"bundled config")
    destination = tmp_path / "configs" / "config-X6873.yml"
    missing_target = tmp_path / "missing.yml"

    def no_hardlinks(src, dst):
        # Win the name after the initial exists() check but before publication.
        if symlink:
            destination.symlink_to(missing_target)
        else:
            destination.write_bytes(b"user config")
        raise OSError(errno.EOPNOTSUPP, "hardlinks unavailable")

    monkeypatch.setattr(paths.os, "link", no_hardlinks)
    assert paths._publish_if_missing(source, destination, 0o644) is False
    if symlink:
        assert destination.is_symlink()
        assert destination.readlink() == missing_target
        assert not missing_target.exists()
    else:
        assert destination.read_bytes() == b"user config"
    assert list(destination.parent.iterdir()) == [destination]


def test_atomic_rename_publishes_the_staged_inode(tmp_path):
    source = tmp_path / "staged.yml"
    source.write_bytes(b"complete config")
    source_inode = source.stat().st_ino
    destination = tmp_path / "config-é.yml"

    assert paths._rename_if_missing(source, destination) is True
    assert not source.exists()
    assert destination.read_bytes() == b"complete config"
    assert destination.stat().st_ino == source_inode


def test_atomic_rename_does_not_replace_existing_file(tmp_path):
    source = tmp_path / "staged.yml"
    source.write_bytes(b"bundled config")
    destination = tmp_path / "config.yml"
    destination.write_bytes(b"user config")
    user_inode = destination.stat().st_ino

    assert paths._rename_if_missing(source, destination) is False
    assert source.read_bytes() == b"bundled config"
    assert destination.read_bytes() == b"user config"
    assert destination.stat().st_ino == user_inode


@pytest.mark.skipif(sys.platform != "linux", reason="Linux renameat2 binding")
@pytest.mark.parametrize(
    "error", [errno.EACCES, errno.ENOSPC, errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP]
)
def test_atomic_rename_errors_never_fall_back_to_copy(monkeypatch, tmp_path, error):
    source = tmp_path / "bundled.yml"
    source.write_bytes(b"bundled config")
    destination = tmp_path / "configs" / "config.yml"

    def no_hardlinks(*args):
        raise OSError(errno.EOPNOTSUPP, "hardlinks unavailable")

    def renameat2(src_dir, src, dst_dir, dst, flags):
        assert (src_dir, dst_dir, flags) == (-100, -100, 1)
        assert dst == os.fsencode(destination)
        paths.ctypes.set_errno(error)
        return -1

    monkeypatch.setattr(paths.os, "link", no_hardlinks)
    monkeypatch.setattr(
        paths.ctypes, "CDLL", lambda *a, **k: SimpleNamespace(renameat2=renameat2)
    )
    with pytest.raises(OSError) as failure:
        paths._publish_if_missing(source, destination, 0o644)
    assert failure.value.errno == error
    assert list(destination.parent.iterdir()) == []


@pytest.mark.skipif(sys.platform != "linux", reason="Linux renameat2 binding")
def test_missing_renameat2_fails_without_creating_destination(monkeypatch, tmp_path):
    source = tmp_path / "bundled.yml"
    source.write_bytes(b"bundled config")
    destination = tmp_path / "configs" / "config.yml"

    def no_hardlinks(*args):
        raise OSError(errno.EOPNOTSUPP, "hardlinks unavailable")

    monkeypatch.setattr(paths.os, "link", no_hardlinks)
    monkeypatch.setattr(paths.ctypes, "CDLL", lambda *a, **k: SimpleNamespace())
    with pytest.raises(OSError, match="Atomic no-replace"):
        paths._publish_if_missing(source, destination, 0o644)
    assert list(destination.parent.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX fallback guard")
def test_unsupported_platform_does_not_use_overwriting_rename(monkeypatch, tmp_path):
    source = tmp_path / "staged.yml"
    source.write_bytes(b"complete config")
    destination = tmp_path / "config.yml"
    monkeypatch.setattr(paths.sys, "platform", "unsupported")

    with pytest.raises(OSError, match="Atomic no-replace"):
        paths._rename_if_missing(source, destination)
    assert not destination.exists()
    assert source.read_bytes() == b"complete config"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux renameat2 binding")
def test_atomic_rename_rejects_nul_instead_of_truncating_path(tmp_path):
    source = tmp_path / "staged.yml"
    source.write_bytes(b"complete config")
    destination = tmp_path / "config.yml"

    with pytest.raises(ValueError, match="embedded null byte"):
        paths._rename_if_missing(Path(str(source) + "\x00ignored"), destination)
    assert source.read_bytes() == b"complete config"
    assert not destination.exists()


def test_publish_still_skips_existing_destination_when_link_fails(
    monkeypatch, tmp_path
):
    source = tmp_path / "config-X6873.yml"
    source.write_bytes(b"bundled: true\n")
    destination = tmp_path / "configs" / "config-X6873.yml"
    destination.parent.mkdir()
    destination.write_bytes(b"user-edited: true\n")

    def link_without_hardlink_support(src, dst):
        raise OSError(errno.EOPNOTSUPP, "operation not supported")

    monkeypatch.setattr(paths.os, "link", link_without_hardlink_support)

    # A pre-existing user file must never be replaced by bundled defaults,
    # even when publication has to fall back to renaming.
    assert paths._publish_if_missing(source, destination, 0o644) is False
    assert destination.read_bytes() == b"user-edited: true\n"


def test_source_detection_accepts_relocated_vendor_override(monkeypatch, tmp_path):
    project_root = tmp_path / "checkout"
    configs = project_root / "configs"
    override = tmp_path / "relocated-vendor"
    configs.mkdir(parents=True)
    override.mkdir()
    (project_root / "pyproject.toml").touch()
    monkeypatch.setattr(paths, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(paths, "SOURCE_CONFIGS_DIR", configs)
    monkeypatch.setattr(
        paths, "SOURCE_VENDOR_DIR", project_root / "vendor" / "google-ota-prober"
    )
    monkeypatch.setenv("CHECKOTA_VENDOR_DIR", str(override))

    assert paths._is_source_checkout()


def test_wheel_state_ignores_arbitrary_cwd_file(monkeypatch, tmp_path):
    state_dir = tmp_path / "xdg-state" / "checkota"
    legacy = tmp_path / "processed_updates.txt"
    legacy.write_bytes(b"old title\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(paths, "IS_SOURCE_CHECKOUT", False)
    monkeypatch.setattr(paths, "STATE_DIR", state_dir)

    state_path = paths.processed_updates_path()

    assert state_path == state_dir / "processed_updates.txt"
    assert not state_path.exists()
    assert legacy.read_bytes() == b"old title\n"
    assert state_dir.is_dir()


def test_source_state_preserves_legacy_cwd_fallback(monkeypatch, tmp_path):
    project_root = tmp_path / "checkout"
    launch_dir = tmp_path / "launch"
    launch_dir.mkdir()
    legacy = launch_dir / "processed_updates.txt"
    legacy.write_bytes(b"old title\n")
    monkeypatch.chdir(launch_dir)
    monkeypatch.setattr(paths, "IS_SOURCE_CHECKOUT", True)
    monkeypatch.setattr(paths, "PROJECT_ROOT", project_root)

    assert paths.processed_updates_path() == legacy


def test_pyproject_declares_packaged_vendor_and_configs():
    import tomllib

    project_root = Path(__file__).resolve().parents[1]
    with (project_root / "pyproject.toml").open("rb") as project_file:
        data = tomllib.load(project_file)

    setuptools = data["tool"]["setuptools"]
    assert "checkota._vendor.checkin" in setuptools["packages"]
    assert "checkota._vendor.utils" in setuptools["packages"]
    assert "checkota.bundled_configs" in setuptools["packages"]
    assert data["tool"]["setuptools"]["package-data"]["checkota.bundled_configs"] == [
        "config-*.yml",
        "config-*.yaml",
    ]
    assert (
        "ATTRIBUTION" in data["tool"]["setuptools"]["package-data"]["checkota._vendor"]
    )
