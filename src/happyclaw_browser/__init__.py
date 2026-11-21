"""happyclaw-browser: AI-friendly browser automation via CDP."""

from happyclaw_browser.install import (
    InstallEvent,
    chromium_executable,
    ensure_chromium,
    install_chromium_stream,
)
from happyclaw_browser.mode import BrowserMode
from happyclaw_browser.models import ActionMetrics, TabInfo, ToolResult
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
    "TabInfo",
    "InstallEvent",
    "chromium_executable",
    "ensure_chromium",
    "install_chromium_stream",
]
