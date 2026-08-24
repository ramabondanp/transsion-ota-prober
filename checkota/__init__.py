"""checkota - OTA firmware update checker for Transsion devices.

The package bootstrap makes both the source checkout and a regular wheel
self-contained: source uses its repository vendor tree, while a wheel uses the
namespaced vendor resources included by setuptools. Wheel config defaults are
seeded lazily only when config lookup needs them.
"""

from .paths import ensure_vendor_on_path

ensure_vendor_on_path()
