"""happyclaw-browser: AI-friendly browser automation via CDP."""

from happyclaw_browser.install import (
    InstallEvent,
    chromium_executable,
    ensure_chromium,
    install_chromium_stream,
)
from happyclaw_browser.mode import BrowserMode
from happyclaw_browser.models import ActionMetrics, ArtifactRef, TabInfo, ToolResult
from happyclaw_browser.pool import (
    PoolExhaustedError,
    SessionPool,
    default_pool,
    reset_default_pool,
)
from happyclaw_browser.session import BrowserSession
from happyclaw_browser.settings import HappyclawSettings, settings
from happyclaw_browser.tool_interface import browser_tool

__all__ = [
    "BrowserSession",
    "browser_tool",
    "BrowserMode",
    "HappyclawSettings",
    "settings",
    "ToolResult",
    "ActionMetrics",
    "ArtifactRef",
    "TabInfo",
    "InstallEvent",
    "chromium_executable",
    "ensure_chromium",
    "install_chromium_stream",
    "SessionPool",
    "PoolExhaustedError",
    "default_pool",
    "reset_default_pool",
]
