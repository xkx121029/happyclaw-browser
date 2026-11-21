"""Wait primitives and artifact capture against a real browser.

Static ``data:`` pages keep this deterministic — no network, no flakiness —
while still exercising real CDP: selector visibility, frame navigation,
network-idle accounting, and the three artifact encoders.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from happyclaw_browser.profile import ProfileManager
from happyclaw_browser.session import BrowserSession
from happyclaw_browser.settings import HappyclawSettings

_BUTTON_PAGE = (
    "data:text/html,<h1>Ready</h1>"
    "<button id='go' style='padding:20px'>Continue</button>"
)
_TEXT_PAGE = "data:text/html,<h1>Only a heading</h1>"


@pytest.fixture()
async def sess(tmp_path: Path):
    cfg = HappyclawSettings(
        profiles_dir=tmp_path / "profiles",
        screenshots_dir=tmp_path / "screenshots",
        artifacts_dir=tmp_path / "artifacts",
        cdp_port_start=19900,
        cdp_port_scan_limit=60,
    )
    manager = ProfileManager(base_dir=tmp_path / "profiles", settings=cfg)
    session = await BrowserSession.create(
        profile="waits", headless=True, profile_manager=manager, settings=cfg
    )
    try:
        yield session
    finally:
        await session.close(kill=True)


async def test_wait_for_selector_finds_a_painted_button(sess: BrowserSession):
    await sess.navigate(_BUTTON_PAGE)

    result = await sess.wait_for_selector("#go")

    assert result.success is True
    payload = (result.metadata or {})["wait"]
    assert payload["selector"] == "#go"
    assert payload["interactive_elements"] >= 1


async def test_wait_for_selector_timeout_captures_the_page(sess: BrowserSession):
    await sess.navigate(_TEXT_PAGE)

    result = await sess.wait_for_selector("#missing", timeout_ms=300)

    assert result.success is False
    payload = (result.metadata or {})["wait"]
    assert payload["url"].startswith("data:text/html")
    assert payload["selector_matches"] == 0
    assert "matched no element" in payload["hint"]
    screenshot = payload["screenshot"]
    assert screenshot is not None and Path(screenshot).exists(), (
        "a timeout must hand back a screenshot of the page at the deadline"
    )


async def test_wait_for_selector_hidden_state_clears(sess: BrowserSession):
    await sess.navigate(_TEXT_PAGE)

    result = await sess.wait_for_selector("#spinner", state="hidden", timeout_ms=2000)

    assert result.success is True


async def test_wait_for_navigation_accepts_the_landed_url(sess: BrowserSession):
    await sess.navigate(_TEXT_PAGE)

    result = await sess.wait_for_navigation(url_contains="data:text/html")

    assert result.success is True
    assert "data:text/html" in (result.metadata or {})["wait"]["url"]


async def test_wait_for_network_idle_on_a_static_page(sess: BrowserSession):
    await sess.navigate(_TEXT_PAGE)

    result = await sess.wait_for_network_idle(idle_ms=200, timeout_ms=5000)

    assert result.success is True
    assert (result.metadata or {})["wait"]["condition"] == "network_idle"


async def test_artifact_capture_writes_all_three_kinds(sess: BrowserSession, tmp_path):
    await sess.navigate(_BUTTON_PAGE)

    for kind, suffix in (("screenshot", ".png"), ("pdf", ".pdf"), ("dom", ".html")):
        result = await sess.artifact(kind)

        assert result.success is True, f"{kind}: {result.error}"
        ref = (result.metadata or {})["artifact"]
        path = Path(str(ref["path"]))
        assert path.exists()
        assert path.suffix == suffix
        assert ref["size_bytes"] > 0
        assert path.parent.parent == tmp_path / "artifacts"

    dom_ref = (await sess.artifact("dom")).metadata or {}
    dom_path = Path(str(dom_ref["artifact"]["path"]))
    assert "Continue" in dom_path.read_text(encoding="utf-8")
