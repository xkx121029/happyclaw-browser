"""Global configuration loaded from environment variables.

All settings can be overridden by:
1. Setting environment variables before importing the package (highest priority)
2. Instantiating HappyclawSettings with explicit values and passing it to
   BrowserSession.create()
3. Module-level defaults (lowest priority)

Environment variables (all optional):

  BROWSER_USE_PROFILES_DIR   - base directory for Chrome user-data-dirs
                          default: ~/.happyclaw-browser/profiles
  BROWSER_USE_SCREENSHOTS_DIR - base directory where the screenshot action writes
                          PNG files. Defaults to ~/.happyclaw-browser/screenshots.
  BROWSER_USE_CDP_HOST       - host or IP serving Chrome's HTTP/WebSocket CDP endpoint
                          default: localhost
  BROWSER_USE_CDP_PORT_START - first CDP debug port assigned to profiles
                          default: 9222
  BROWSER_USE_MODE   - launch mode: "auto" | "headed" | "headless"
                          default: auto (uses headed when DISPLAY/WAYLAND_DISPLAY
                          is set or on macOS/Windows, headless otherwise)
  BROWSER_USE_CHROME_BIN     - absolute path to Chrome/Chromium executable
                          default: auto-detect
  BROWSER_USE_CDP_TIMEOUT    - seconds to wait for a CDP command response
                          default: 30.0
  BROWSER_USE_LAUNCH_RETRIES - number of times to poll Chrome after launch
                          default: 20
  BROWSER_USE_LAUNCH_DELAY   - seconds between launch poll attempts
                          default: 0.25
  BROWSER_USE_NO_SANDBOX     - if "1"/"true"/"yes", force --no-sandbox on every
                          Chrome launch. If "0"/"false", force-disable the
                          auto-detection (Chrome runs with its full sandbox).
                          When unset, Chrome is launched with --no-sandbox iff
                          we detect that the host kernel will not allow
                          unprivileged user namespaces (typical inside
                          containers / restricted sandboxes).
  BROWSER_USE_VIEWPORT_WIDTH - viewport width in CSS pixels applied via
                          Emulation.setDeviceMetricsOverride after connect.
                          Controls the default screenshot/page render size.
                          default: 1440
  BROWSER_USE_VIEWPORT_HEIGHT - viewport height in CSS pixels.
                          default: 900
  BROWSER_USE_CDP_MAX_MESSAGE_SIZE - max CDP WebSocket frame size in bytes.
                          ``websockets`` defaults to 1 MiB; raise this so
                          ``Page.captureScreenshot`` of large pages does
                          not trip code 1009 (message too big). Set to 0
                          to disable the cap entirely.
                          default: 33554432 (32 MiB)
  BROWSER_USE_CDP_WS_URL     - if set, bypass launcher and connect directly to this
                          Chrome DevTools WebSocket URL (e.g. for remote/Docker Chrome)
                          default: None
  BROWSER_USE_IDLE_TIMEOUT_MINUTES - stop a locally managed Chrome profile after
                          this many minutes without real CDP activity. 0 disables.
                          default: 0
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from happyclaw_browser.mode import BrowserMode, normalize_mode


def _env_path(key: str, default: Path) -> Path:
    val = os.environ.get(key)
    return Path(val) if val else default


#: State directory under ``$HOME``. New installs use ``.happyclaw-browser``; a
#: directory left behind by an earlier release is reused instead of orphaning
#: the login cookies and recordings inside it, and the user is told where the
#: new location is so they can move it themselves.
_STATE_DIR = ".happyclaw-browser"
_LEGACY_STATE_DIRS = (".octop-browser", ".harness-browser")

#: Legacy directories already reported in this process.
_hinted_state_dirs: set[Path] = set()


def state_dir() -> Path:
    """Return the base directory holding profiles, screenshots and recordings.

    Fresh installs use ``~/.happyclaw-browser``. When that path does not exist
    but a state directory from an earlier release is present, that one is
    reused so existing Chrome profiles (cookies) and recordings stay attached,
    and a one-line note points at the new path. Nothing is moved on disk.
    """
    home = Path.home()
    current = home / _STATE_DIR
    if current.exists():
        return current
    for name in _LEGACY_STATE_DIRS:
        legacy = home / name
        if legacy.exists():
            _hint_legacy_state_dir(legacy, current)
            return legacy
    return current


def _hint_legacy_state_dir(legacy: Path, current: Path) -> None:
    """Report a reused legacy state directory once per process."""
    if legacy in _hinted_state_dirs:
        return
    _hinted_state_dirs.add(legacy)
    print(
        f"happyclaw-browser: reusing the state directory {legacy} left by an "
        f"earlier release. Move it to {current} to adopt the new location; "
        "nothing was changed on disk.",
        file=sys.stderr,
    )


def _env_float(key: str, default: float) -> float:
    val = os.environ.get(key)
    return float(val) if val else default


def _env_int(key: str, default: int) -> int:
    val = os.environ.get(key)
    return int(val) if val else default


def _env_str(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


@dataclass
class HappyclawSettings:
    """
    Runtime configuration for happyclaw-browser.

    Instantiate with no arguments to read from environment variables.
    Pass explicit values to override individual settings::

        from happyclaw_browser import HappyclawSettings, BrowserSession

        custom = HappyclawSettings(cdp_port_start=9300, cdp_timeout=60.0)
        sess = await BrowserSession.create(profile="work", settings=custom)

    Or point to a remote Chrome::

        import os
        os.environ["BROWSER_USE_CDP_WS_URL"] = "ws://remote-host:9222/devtools/browser/..."
    """

    profiles_dir: Path = field(
        default_factory=lambda: _env_path(
            "BROWSER_USE_PROFILES_DIR", state_dir() / "profiles"
        )
    )
    screenshots_dir: Path = field(
        default_factory=lambda: _env_path(
            "BROWSER_USE_SCREENSHOTS_DIR",
            state_dir() / "screenshots",
        )
    )
    cdp_host: str = field(
        default_factory=lambda: (
            _env_str("BROWSER_USE_CDP_HOST", "localhost") or "localhost"
        )
    )
    cdp_port_start: int = field(
        default_factory=lambda: _env_int("BROWSER_USE_CDP_PORT_START", 9222)
    )
    chrome_bin: str | None = field(
        default_factory=lambda: _env_str("BROWSER_USE_CHROME_BIN")
    )
    cdp_timeout: float = field(
        default_factory=lambda: _env_float("BROWSER_USE_CDP_TIMEOUT", 30.0)
    )
    launch_retries: int = field(
        default_factory=lambda: _env_int("BROWSER_USE_LAUNCH_RETRIES", 20)
    )
    launch_delay: float = field(
        default_factory=lambda: _env_float("BROWSER_USE_LAUNCH_DELAY", 0.25)
    )
    cdp_ws_url: str | None = field(
        default_factory=lambda: _env_str("BROWSER_USE_CDP_WS_URL")
    )
    idle_timeout_minutes: float = field(
        default_factory=lambda: _env_float("BROWSER_USE_IDLE_TIMEOUT_MINUTES", 0.0)
    )
    viewport_width: int = field(
        default_factory=lambda: _env_int("BROWSER_USE_VIEWPORT_WIDTH", 1440)
    )
    viewport_height: int = field(
        default_factory=lambda: _env_int("BROWSER_USE_VIEWPORT_HEIGHT", 900)
    )
    cdp_max_message_size: int | None = field(
        default_factory=lambda: (
            None
            if _env_int("BROWSER_USE_CDP_MAX_MESSAGE_SIZE", 33554432) == 0
            else _env_int("BROWSER_USE_CDP_MAX_MESSAGE_SIZE", 33554432)
        )
    )
    browser_mode: BrowserMode = field(
        default_factory=lambda: normalize_mode(_env_str("BROWSER_USE_MODE"))
    )


# Module-level singleton — reads env vars at import time.
# Tests should instantiate HappyclawSettings() directly instead of mutating it.
settings = HappyclawSettings()
