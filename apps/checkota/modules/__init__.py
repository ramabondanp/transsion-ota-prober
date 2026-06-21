"""Modules package for the checkota OTA update checking tool.

Importing the package injects the vendored ``google-ota-prober`` tree onto
``sys.path`` so submodules can import the vendored ``checkin``/``utils``
packages regardless of how the app is launched.
"""

from .paths import ensure_vendor_on_path

ensure_vendor_on_path()
