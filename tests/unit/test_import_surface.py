"""The import surface of the package.

Most users of octop-browser import it in an agent process that may never touch
the browser in a given run, and every CLI subcommand imports it. Pulling the
WebSocket and HTTP stacks in at import time made all of them pay for a
transport they were not using, so the deference is pinned here.
"""

from __future__ import annotations

import subprocess
import sys

_PROBE = """
import sys
import octop_browser  # noqa: F401
missing = [m for m in ("aiohttp", "websockets") if m in sys.modules]
print(",".join(missing) or "clean")
"""


def test_importing_the_package_does_not_load_a_transport():
    out = subprocess.run(
        [sys.executable, "-c", _PROBE],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert out == "clean", f"import octop_browser pulled in {out}"


def test_public_api_is_still_reachable_from_the_top_level():
    import octop_browser

    for name in (
        "BrowserSession",
        "browser_tool",
        "OctopSettings",
        "settings",
        "ToolResult",
        "ActionMetrics",
        "TabInfo",
        "ensure_chromium",
        "chromium_executable",
        "install_chromium_stream",
    ):
        assert hasattr(octop_browser, name), name


def test_launcher_still_exposes_aiohttp_for_patching():
    """Four tests monkeypatch ``launcher.aiohttp.ClientSession``.

    Deferring imports elsewhere must not turn that module attribute into a
    call-time-only name.
    """
    import aiohttp

    from octop_browser.cdp import launcher

    assert launcher.aiohttp is aiohttp
