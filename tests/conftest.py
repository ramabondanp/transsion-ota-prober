"""Pytest configuration: make the operational scripts importable in tests."""

import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if SCRIPT_DIR.is_dir() and str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


@pytest.fixture(autouse=True)
def isolate_regional_proxy_template(monkeypatch, tmp_path):
    """Never let mocked check-ins read real paid-proxy credentials."""
    from checkota import proxy

    monkeypatch.setattr(proxy, "ENV_FILE", tmp_path / ".env")
    monkeypatch.delenv(proxy.PROXMINT_PROXY_TEMPLATE_ENV, raising=False)
