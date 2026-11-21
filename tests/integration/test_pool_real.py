"""Idle reclaim must kill the real Chrome, not just the socket.

"Leaves no orphan process" is asserted three ways: the pool reports the slot
free, the CDP port becomes bindable again, and the pid the launcher recorded
is no longer alive. A disconnect-only implementation passes the first and
fails the other two.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from happyclaw_browser.pool import SessionPool
from happyclaw_browser.profile import ProfileManager, port_available, process_alive
from happyclaw_browser.session import BrowserSession
from happyclaw_browser.settings import HappyclawSettings


def _settings(tmp_path: Path) -> HappyclawSettings:
    return HappyclawSettings(
        profiles_dir=tmp_path / "profiles",
        screenshots_dir=tmp_path / "screenshots",
        artifacts_dir=tmp_path / "artifacts",
        cdp_port_start=19800,
        cdp_port_scan_limit=60,
    )


async def _wait_for(predicate, *, attempts: int = 100, delay: float = 0.1) -> bool:
    for _ in range(attempts):
        if predicate():
            return True
        await asyncio.sleep(delay)
    return predicate()


async def test_idle_reclaim_stops_the_real_browser(tmp_path: Path):
    cfg = _settings(tmp_path)
    manager = ProfileManager(base_dir=tmp_path / "profiles", settings=cfg)

    async def factory(profile, *, headless=None, mode=None, urls=None):
        return await BrowserSession.create(
            profile=profile,
            headless=True,
            profile_manager=manager,
            settings=cfg,
            enable_idle_reaper=False,
        )

    pool = SessionPool(
        max_sessions=1,
        idle_timeout_seconds=0.5,
        reap_interval_seconds=0.1,
        settings=cfg,
        profile_manager=manager,
        session_factory=factory,
    )
    try:
        session = await pool.acquire("idle")
        await session.navigate("data:text/html,<h1>pool</h1>")
        recorded_pid = manager.get_or_create("idle").load_pid()
        cdp_port = session.cdp_port
        assert recorded_pid is not None, "the launcher must record the pid it spawned"
        assert process_alive(recorded_pid)
        await pool.release("idle")

        assert await _wait_for(lambda: pool.stats()["reclaimed"] == 1)
        assert pool.stats()["live_sessions"] == 0

        assert await _wait_for(lambda: port_available(cdp_port)), (
            "the debug port must be released, or the next launch of this "
            "profile cannot bind it"
        )
        assert await _wait_for(lambda: not process_alive(recorded_pid)), (
            "the reclaimed browser must not outlive its session"
        )
    finally:
        await pool.shutdown()
