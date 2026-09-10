"""Filesystem paths and bootstrap for source and wheel installations.

Source checkouts continue to use repository-local configs and state. A regular
wheel has no writable repository beside the package, so it uses per-user XDG
directories and copies packaged defaults there on first use.
"""

import contextlib
import errno
import os
import shutil
import sys
import tempfile
from importlib import resources
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
# checkota/paths.py -> checkota/ -> the source root or site-packages.
PROJECT_ROOT = PACKAGE_ROOT.parent
SOURCE_CONFIGS_DIR = PROJECT_ROOT / "configs"
SOURCE_VENDOR_DIR = PROJECT_ROOT / "vendor" / "google-ota-prober"
PACKAGED_VENDOR_DIR = PACKAGE_ROOT / "_vendor"
PROCESSED_UPDATES_FILE = "processed_updates.txt"


def _is_source_checkout() -> bool:
    """Return whether this package is running from the repository checkout."""
    configured_vendor = os.environ.get("CHECKOTA_VENDOR_DIR")
    override_available = bool(
        configured_vendor and Path(configured_vendor).expanduser().is_dir()
    )
    return (
        (PROJECT_ROOT / "pyproject.toml").is_file()
        and SOURCE_CONFIGS_DIR.is_dir()
        and (SOURCE_VENDOR_DIR.is_dir() or override_available)
    )


IS_SOURCE_CHECKOUT = _is_source_checkout()


def _xdg_home(variable: str, fallback: str) -> Path:
    """Return an absolute XDG base directory, ignoring relative overrides."""
    configured = os.environ.get(variable)
    if configured:
        path = Path(configured).expanduser()
        if path.is_absolute():
            return path
    return (Path.home() / fallback).resolve()


def _wheel_config_dir() -> Path:
    return _xdg_home("XDG_CONFIG_HOME", ".config") / "checkota" / "configs"


def _wheel_state_dir() -> Path:
    return _xdg_home("XDG_STATE_HOME", ".local/state") / "checkota"


# Keep this name stable for callers that need to inspect the active location.
# Merely importing the package never creates or populates this directory.
APP_CONFIGS_DIR = SOURCE_CONFIGS_DIR if IS_SOURCE_CHECKOUT else _wheel_config_dir()
STATE_DIR = PROJECT_ROOT if IS_SOURCE_CHECKOUT else _wheel_state_dir()

# An explicit override always wins, including in a wheel. Otherwise source
# checkouts use the repository tree and wheels use the namespaced package tree.
_explicit_vendor_dir = os.environ.get("CHECKOTA_VENDOR_DIR")
VENDOR_DIR = (
    Path(_explicit_vendor_dir).expanduser()
    if _explicit_vendor_dir
    else (SOURCE_VENDOR_DIR if IS_SOURCE_CHECKOUT else PACKAGED_VENDOR_DIR)
)

_vendor_ready = False


def _resource_root():
    """Return the packaged config resource directory."""
    return resources.files("checkota.bundled_configs")


def _publish_if_missing(source, destination: Path, mode: int) -> bool:
    """Copy source to destination without replacing a concurrent/user file."""
    if destination.exists():
        return False

    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as input_file, os.fdopen(fd, "wb") as output_file:
            fd = -1
            shutil.copyfileobj(input_file, output_file)
        os.chmod(temporary, mode)
        try:
            # Linking, rather than replacing, makes publication non-destructive
            # if another process created the destination after the first check.
            os.link(temporary, destination)
        except FileExistsError:
            return False
        except OSError as exc:
            # Hard links may be unavailable on some filesystems, but an
            # arbitrary link failure must not turn into an overwrite. In
            # particular, permission errors should remain errors rather than
            # falling through to os.replace().
            unsupported = {
                errno.EXDEV,
                errno.EOPNOTSUPP,
                getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
            }
            if exc.errno not in unsupported:
                raise

            # There is no portable atomic rename-with-NOREPLACE primitive in
            # Python. O_EXCL still makes the fallback publication exclusive:
            # a concurrent process/user can win the race, but this process can
            # never overwrite the winner.
            try:
                destination_fd = os.open(
                    destination,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    mode,
                )
            except FileExistsError:
                return False
            destination_identity: tuple[int, int] | None = None
            try:
                stat_result = os.fstat(destination_fd)
                destination_identity = (stat_result.st_dev, stat_result.st_ino)
            except OSError:
                destination_identity = None
            try:
                try:
                    with (
                        os.fdopen(destination_fd, "wb") as output_file,
                        temporary.open("rb") as input_file,
                    ):
                        destination_fd = -1
                        shutil.copyfileobj(input_file, output_file)
                        output_file.flush()
                        os.fsync(output_file.fileno())
                except BaseException:
                    # A partial O_EXCL destination would otherwise be treated
                    # as a user file forever and never replaced by seeding.
                    # Only remove the inode we created: a concurrent process
                    # may have replaced the path after our O_EXCL open.
                    remove = destination_identity is None
                    if destination_identity is not None:
                        try:
                            current = destination.stat()
                            remove = (
                                current.st_dev,
                                current.st_ino,
                            ) == destination_identity
                        except OSError:
                            remove = False
                    if remove:
                        with contextlib.suppress(OSError):
                            destination.unlink(missing_ok=True)
                    raise
            finally:
                if destination_fd != -1:
                    os.close(destination_fd)
        return True
    finally:
        if fd != -1:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def ensure_config_resources() -> Path:
    """Create the active config directory and seed missing wheel defaults."""
    if IS_SOURCE_CHECKOUT:
        return APP_CONFIGS_DIR

    APP_CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    for resource in _resource_root().iterdir():
        if (
            resource.is_file()
            and resource.name.startswith("config-")
            and resource.name.endswith((".yml", ".yaml"))
        ):
            _publish_if_missing(resource, APP_CONFIGS_DIR / resource.name, 0o644)
    return APP_CONFIGS_DIR


def active_config_dir() -> Path:
    """Return the application config directory, seeding wheel defaults first."""
    return ensure_config_resources()


def processed_updates_path() -> Path:
    """Return the dedup state path and create its parent when necessary.

    Source mode preserves the existing repository anchor and CWD fallback.
    Wheel mode never imports state from the launch directory because an
    arbitrary CWD is not a trustworthy migration source.
    """
    if IS_SOURCE_CHECKOUT:
        anchored = PROJECT_ROOT / PROCESSED_UPDATES_FILE
        if anchored.exists():
            return anchored
        legacy = Path.cwd() / PROCESSED_UPDATES_FILE
        if legacy.exists() and legacy != anchored:
            return legacy
        return anchored

    state_path = STATE_DIR / PROCESSED_UPDATES_FILE
    state_path.parent.mkdir(parents=True, exist_ok=True)
    return state_path


def bootstrap_vendor(vendor_dir: Path) -> None:
    """Insert ``vendor_dir`` on ``sys.path`` if present, or fail loudly."""
    if vendor_dir.is_dir():
        paths = [vendor_dir]
        checkin_dir = vendor_dir / "checkin"
        if checkin_dir.is_dir():
            # The generated checkin_pb2.py uses an historical top-level
            # sibling import (android_checkin_pb2), so expose that directory
            # as well as the vendor root without editing generated code.
            paths.append(checkin_dir)
        for path in reversed(paths):
            vendor_path = str(path)
            if vendor_path not in sys.path:
                sys.path.insert(0, vendor_path)
        return
    sys.stderr.write(
        f"Vendored google-ota-prober not found at {vendor_dir}. "
        "Install from a source checkout, use the packaged wheel, or set "
        "CHECKOTA_VENDOR_DIR to the vendored tree.\n"
    )
    raise SystemExit(1)


def ensure_vendor_on_path() -> None:
    """Inject the vendored tree onto ``sys.path`` idempotently."""
    global _vendor_ready
    if _vendor_ready:
        return
    bootstrap_vendor(VENDOR_DIR)
    _vendor_ready = True
