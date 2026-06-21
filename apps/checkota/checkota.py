#!/usr/bin/python3
"""checkota — OTA firmware update checker for Transsion devices.

Thin entry point. All logic lives in the ``modules`` package; importing
``modules`` bootstraps the vendored ``google-ota-prober`` tree onto sys.path.
"""

import sys

from modules.cli import main

if __name__ == "__main__":
    sys.exit(main())
