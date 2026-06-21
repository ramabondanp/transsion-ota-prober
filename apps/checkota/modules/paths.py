"""Filesystem anchors and vendored-dependency bootstrap.

Importing this module is side-effect free; call ``ensure_vendor_on_path()`` to
inject the vendored ``google-ota-prober`` tree onto ``sys.path``. That bootstrap
runs automatically from ``modules/__init__.py`` so any module that imports the
vendored ``checkin``/``utils`` packages (e.g. ``update_checker``) resolves them
regardless of how the app is launched.
"""

import os
import sys
from pathlib import Path

# modules/ -> apps/checkota/ -> <repo root>
APP_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = APP_DIR.parent.parent
APP_CONFIGS_DIR = APP_DIR / "configs"

# Vendor dir lives at the repo root (outside the package tree), so only an
# editable/source install can reach it. Allow an env override for relocated
# vendor trees (e.g. real wheel installs that copy it elsewhere).
VENDOR_DIR = Path(
    os.environ.get("CHECKOTA_VENDOR_DIR", PROJECT_ROOT / "vendor" / "google-ota-prober")
)

_vendor_ready = False


def ensure_vendor_on_path() -> None:
    """Inject the vendored google-ota-prober tree onto sys.path (idempotent).

    Exits the process with a clear message if the vendor tree is missing, since
    the vendored protobuf modules are a hard runtime dependency.
    """
    global _vendor_ready
    if _vendor_ready:
        return
    if VENDOR_DIR.is_dir():
        vendor_path = str(VENDOR_DIR)
        if vendor_path not in sys.path:
            sys.path.insert(0, vendor_path)
        _vendor_ready = True
        return
    sys.stderr.write(
        f"Vendored google-ota-prober not found at {VENDOR_DIR}. "
        "checkota requires an editable/source install (pip install -e .) or "
        "set CHECKOTA_VENDOR_DIR to the vendored tree.\n"
    )
    sys.exit(1)
