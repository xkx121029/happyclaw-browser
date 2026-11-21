"""A failed Chrome launch must not leave a half-initialised profile behind.

The fake "Chrome" is the running Python interpreter: handed Chrome's flags it
exits immediately with a non-zero status, which is exactly the shape of a real
launch failure (missing library, sandbox refusal, no desktop session). The
launcher's real detection and cleanup path runs around it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from happyclaw_browser.cdp.launcher import launch_or_attach
from happyclaw_browser.profile import ProfileManager
from happyclaw_browser.settings import HappyclawSettings
from happyclaw_browser.settings import settings as live_settings


@pytest.fixture()
def failing_chrome(monkeypatch):
    """Point the launcher at a binary that dies before binding its port."""
    monkeypatch.setattr(live_settings, "chrome_bin", sys.executable)
    monkeypatch.setattr(live_settings, "launch_retries", 3)
    monkeypatch.setattr(live_settings, "launch_delay", 0.02)
    monkeypatch.setattr(live_settings, "cdp_host", "localhost")
    return sys.executable


def _manager(tmp_path: Path) -> ProfileManager:
    # Port 0 is invalid for Chrome; the scan finds a real free port instead of
    # colliding with whatever else runs on the developer's machine.
    return ProfileManager(
        base_dir=tmp_path / "profiles",
        settings=HappyclawSettings(
            cdp_port_start=23400, cdp_host="localhost", cdp_port_scan_limit=40
        ),
    )


async def test_failed_first_launch_leaves_no_profile_directory(
    tmp_path, failing_chrome
):
    manager = _manager(tmp_path)
    profile = manager.get_or_create("fresh")
    assert profile.fresh is True
    assert profile.data_dir.is_dir()

    with pytest.raises(RuntimeError):
        await launch_or_attach(profile)

    assert not Path(profile.data_dir).exists(), (
        "a profile that never launched must not survive as a half-built "
        "user-data-dir that makes the retry fail too"
    )
    assert profile.load_pid() is None


async def test_failed_launch_keeps_an_established_profile(tmp_path, failing_chrome):
    manager = _manager(tmp_path)
    established = manager.get_or_create("existing")
    marker = Path(established.data_dir) / "Cookies"
    marker.write_text("login state", encoding="utf-8")
    lock = Path(established.data_dir) / "SingletonLock"
    lock.write_text("stale", encoding="utf-8")
    established.fresh = False

    with pytest.raises(RuntimeError):
        await launch_or_attach(established)

    assert marker.read_text(encoding="utf-8") == "login state", (
        "a profile with real cookies must survive a failed launch"
    )
    assert not lock.exists(), "the stale singleton lock must be cleared"
    assert Path(established.data_dir).is_dir()
    assert established.load_pid() is None
