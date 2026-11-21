"""Unit tests for HappyclawSettings configuration."""

from __future__ import annotations

import importlib
import warnings
from pathlib import Path

import pytest

from happyclaw_browser.settings import HappyclawSettings, state_dir


def test_defaults_match_expected():
    s = HappyclawSettings()
    # state_dir() resolves to ~/.happyclaw-browser, or a directory left by an
    # earlier release when that one already exists on this machine (which is
    # exactly the case the migration warning reports, hence the filter).
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        base = state_dir()
    assert s.profiles_dir == base / "profiles"
    assert s.screenshots_dir == base / "screenshots"
    assert s.cdp_host == "localhost"
    assert s.cdp_port_start == 9222
    assert s.chrome_bin is None
    assert s.cdp_timeout == 30.0
    assert s.launch_retries == 20
    assert s.launch_delay == 0.25
    assert s.cdp_ws_url is None
    assert s.viewport_width == 1440
    assert s.viewport_height == 900
    assert s.cdp_max_message_size == 33554432
    assert s.browser_mode == "auto"
    assert s.idle_timeout_minutes == 0.0


def test_state_dir_prefers_new_name_for_fresh_installs(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert state_dir() == tmp_path / ".happyclaw-browser"


def test_state_dir_keeps_using_legacy_dir_when_present(monkeypatch, tmp_path):
    """An earlier release's state dir holds login cookies: keep using it."""
    from happyclaw_browser.settings import _LEGACY_STATE_DIRS

    legacy = tmp_path / _LEGACY_STATE_DIRS[0]
    legacy.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    with pytest.warns(UserWarning, match="Move it to"):
        assert state_dir() == legacy
    assert HappyclawSettings().profiles_dir == legacy / "profiles"


def test_state_dir_falls_back_through_every_legacy_name(monkeypatch, tmp_path):
    from happyclaw_browser.settings import _LEGACY_STATE_DIRS

    oldest = tmp_path / _LEGACY_STATE_DIRS[-1]
    oldest.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    with pytest.warns(UserWarning, match="Move it to"):
        assert state_dir() == oldest


def test_state_dir_migration_warns_once_per_process(monkeypatch, tmp_path):
    from happyclaw_browser.settings import _LEGACY_STATE_DIRS

    legacy = tmp_path / _LEGACY_STATE_DIRS[0]
    legacy.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(
        importlib.import_module("happyclaw_browser.settings"),
        "_hinted_state_dirs",
        set(),
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        for _ in range(3):
            assert state_dir() == legacy
    assert len([w for w in caught if issubclass(w.category, UserWarning)]) == 1


def test_state_dir_prefers_new_dir_when_both_exist(monkeypatch, tmp_path):
    from happyclaw_browser.settings import _LEGACY_STATE_DIRS, _STATE_DIR

    (tmp_path / _LEGACY_STATE_DIRS[0]).mkdir()
    (tmp_path / _STATE_DIR).mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert state_dir() == tmp_path / _STATE_DIR


def test_env_override_profiles_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("BROWSER_USE_PROFILES_DIR", str(tmp_path / "custom_profiles"))
    s = HappyclawSettings()
    assert s.profiles_dir == tmp_path / "custom_profiles"


def test_env_override_cdp_host(monkeypatch):
    monkeypatch.setenv("BROWSER_USE_CDP_HOST", "cdp.example.internal")
    s = HappyclawSettings()
    assert s.cdp_host == "cdp.example.internal"


def test_env_override_cdp_port_start(monkeypatch):
    monkeypatch.setenv("BROWSER_USE_CDP_PORT_START", "9300")
    s = HappyclawSettings()
    assert s.cdp_port_start == 9300


def test_env_override_cdp_ws_url(monkeypatch):
    monkeypatch.setenv(
        "BROWSER_USE_CDP_WS_URL", "ws://remote:9222/devtools/browser/abc"
    )
    s = HappyclawSettings()
    assert s.cdp_ws_url == "ws://remote:9222/devtools/browser/abc"


def test_env_override_idle_timeout(monkeypatch):
    monkeypatch.setenv("BROWSER_USE_IDLE_TIMEOUT_MINUTES", "30")
    assert HappyclawSettings().idle_timeout_minutes == 30.0


def test_explicit_override_takes_precedence(monkeypatch):
    monkeypatch.setenv("BROWSER_USE_CDP_PORT_START", "9300")
    monkeypatch.setenv("BROWSER_USE_CDP_TIMEOUT", "60.0")
    # Explicit values ignore env vars (dataclass field values passed directly)
    s = HappyclawSettings(cdp_port_start=9400, cdp_timeout=10.0)
    assert s.cdp_port_start == 9400
    assert s.cdp_timeout == 10.0


def test_env_override_browser_mode(monkeypatch):
    monkeypatch.setenv("BROWSER_USE_MODE", "headless")
    s = HappyclawSettings()
    assert s.browser_mode == "headless"


def test_env_browser_mode_invalid(monkeypatch):
    monkeypatch.setenv("BROWSER_USE_MODE", "ghost")
    with pytest.raises(ValueError):
        HappyclawSettings()


def test_env_override_screenshots_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("BROWSER_USE_SCREENSHOTS_DIR", str(tmp_path / "shots"))
    s = HappyclawSettings()
    assert s.screenshots_dir == tmp_path / "shots"


def test_env_override_viewport(monkeypatch):
    monkeypatch.setenv("BROWSER_USE_VIEWPORT_WIDTH", "1920")
    monkeypatch.setenv("BROWSER_USE_VIEWPORT_HEIGHT", "1080")
    s = HappyclawSettings()
    assert s.viewport_width == 1920
    assert s.viewport_height == 1080


def test_env_override_cdp_max_message_size(monkeypatch):
    monkeypatch.setenv("BROWSER_USE_CDP_MAX_MESSAGE_SIZE", "67108864")
    s = HappyclawSettings()
    assert s.cdp_max_message_size == 67108864


def test_cdp_max_message_size_zero_disables_cap(monkeypatch):
    """``0`` is the documented escape hatch for "no cap" — websockets
    treats ``max_size=None`` as unlimited, so settings must translate."""
    monkeypatch.setenv("BROWSER_USE_CDP_MAX_MESSAGE_SIZE", "0")
    s = HappyclawSettings()
    assert s.cdp_max_message_size is None
