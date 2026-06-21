#!/usr/bin/python3
# ruff: noqa: E402
"""checkota — OTA firmware update checker for Transsion devices.

Thin entry point. Vendor bootstrap runs here before any module imports so
the vendored ``checkin``/``utils`` packages are always on sys.path, regardless
of how the app is launched (script, ``python -m``, editable install, etc.).
"""

import os
import sys
from pathlib import Path

# Bootstrap vendor path before any module imports.
# This must run before `from modules.cli import main` because modules transitively
# import from the vendored checkin/utils packages.
_APP_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _APP_DIR.parent.parent
_VENDOR_DIR = Path(
    os.environ.get(
        "CHECKOTA_VENDOR_DIR", _PROJECT_ROOT / "vendor" / "google-ota-prober"
    )
)
if _VENDOR_DIR.is_dir():
    _vendor_path = str(_VENDOR_DIR)
    if _vendor_path not in sys.path:
        sys.path.insert(0, _vendor_path)
else:
    sys.stderr.write(
        f"Vendored google-ota-prober not found at {_VENDOR_DIR}.\n"
        "checkota requires an editable/source install (pip install -e .) or "
        "set CHECKOTA_VENDOR_DIR to the vendored tree.\n"
    )
    sys.exit(1)

from modules.cli import main

if __name__ == "__main__":
    sys.exit(main())
