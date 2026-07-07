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
from modules.paths import bootstrap_vendor  # noqa: E402

bootstrap_vendor(_VENDOR_DIR)

from modules.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
