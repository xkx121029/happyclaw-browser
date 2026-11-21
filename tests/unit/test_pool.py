"""Session pool semantics: reuse, queueing, idle reclaim, crash rebuild.

These use a session *factory* — the pool is the object under test, and what
it does with sessions (lease counts, kill-on-idle, rebuild-on-death) is what
each test asserts. Real-browser proof of the same behaviour lives in
``tests/integration/test_pool_real.py``.
"""

from __future__ import annotations

import asyncio

import pytest

from happyclaw_browser.pool import PoolExhaustedError, SessionPool
from happyclaw_browser.tool_interface import browser_tool


class FakeSession:
    """Stands in for a BrowserSession; records how it was closed."""

    def __init__(self, profile: str, *, alive: bool = True) -> None:
        self.profile = profile
        self.alive = alive
        self.closed: list[bool] = []

    async def close(self, *, kill: bool = False) -> None:
        self.closed.append(kill)
        self.alive = False

    @property
    def is_alive(self) -> bool:
        return self.alive


def _factory(created: list[FakeSession], *, alive: bool = True):
    async def factory(profile, *, headless=None, mode=None, urls=None):
        session = FakeSession(profile, alive=alive)
        created.append(session)
        return session

    return factory


def _pool(**kwargs) -> SessionPool:
    prefs = {
        "max_sessions": 2,
        "idle_timeout_seconds": 0.0,
        "reap_interval_seconds": 3600.0,
    }
    prefs.update(kwargs)
    return SessionPool(**prefs)  # type: ignore[arg-type]


async def test_same_profile_reuses_one_session():
    created: list[FakeSession] = []
    pool = _pool(session_factory=_factory(created))

    first = await pool.acquire("work")
    await pool.release("work")
    second = await pool.acquire("work")

    assert second is first
    assert len(created) == 1, "a repeated task must not pay a second cold start"
    assert pool.stats()["live_sessions"] == 1
    await pool.shutdown()


async def test_full_pool_queues_instead_of_failing():
    created: list[FakeSession] = []
    pool = _pool(max_sessions=1, session_factory=_factory(created))
    await pool.acquire("a")

    queued = asyncio.create_task(pool.acquire("b"))
    await asyncio.sleep(0.1)
    assert not queued.done(), "the second profile must wait for a free slot"
    assert pool.stats()["queued"] == 1

    await pool.close("a")
    session = await asyncio.wait_for(queued, timeout=5)

    assert session.profile == "b"
    assert pool.stats()["queued"] == 0
    await pool.shutdown()


async def test_acquire_timeout_reports_capacity_instead_of_hanging():
    created: list[FakeSession] = []
    pool = _pool(
        max_sessions=1,
        acquire_timeout=0.1,
        session_factory=_factory(created),
    )
    await pool.acquire("a")

    with pytest.raises(PoolExhaustedError) as excinfo:
        await pool.acquire("b")

    message = str(excinfo.value)
    assert "max=1" in message
    assert "BROWSER_USE_POOL_MAX_SESSIONS" in message
    await pool.shutdown()


async def test_idle_reclaim_kills_the_browser_and_frees_the_slot():
    created: list[FakeSession] = []
    pool = _pool(
        max_sessions=1,
        idle_timeout_seconds=0.05,
        reap_interval_seconds=0.01,
        session_factory=_factory(created),
    )
    session = await pool.acquire("a")
    await pool.release("a")

    for _ in range(50):
        if pool.stats()["live_sessions"] == 0:
            break
        await asyncio.sleep(0.02)

    assert session.closed == [True], "reclaim must terminate the browser (kill=True)"
    assert pool.stats()["live_sessions"] == 0
    assert pool.stats()["reclaimed"] == 1

    replacement = await pool.acquire("b")
    assert replacement.profile == "b", "the reclaimed slot must be reusable"
    await pool.shutdown()


async def test_leased_session_is_not_reclaimed():
    created: list[FakeSession] = []
    pool = _pool(
        max_sessions=1,
        idle_timeout_seconds=0.05,
        reap_interval_seconds=0.01,
        session_factory=_factory(created),
    )
    session = await pool.acquire("a")

    await asyncio.sleep(0.2)

    assert session.closed == [], "a session in use must never be reaped"
    await pool.shutdown()


async def test_dead_session_is_rebuilt_instead_of_handed_back():
    created: list[FakeSession] = []
    pool = _pool(session_factory=_factory(created))

    first = await pool.acquire("a")
    first.alive = False
    await pool.release("a")
    second = await pool.acquire("a")

    assert second is not first
    assert first.closed == [False], "the corpse is disconnected, not killed"
    assert pool.stats()["rebuilt"] == 1
    assert pool.stats()["live_sessions"] == 1
    await pool.shutdown()


async def test_stats_name_every_live_profile():
    created: list[FakeSession] = []
    pool = _pool(session_factory=_factory(created))
    await pool.acquire("one")
    await pool.acquire("two")

    stats = pool.stats()

    assert stats["live_sessions"] == 2
    assert {p["profile"] for p in stats["profiles"]} == {"one", "two"}
    await pool.shutdown()


async def test_pool_status_action_never_boots_a_browser(monkeypatch):
    """A status probe must not pay for a cold start."""
    from happyclaw_browser import tool_interface as ti

    async def explode(*_args, **_kwargs):
        raise AssertionError("pool_status must not create a session")

    monkeypatch.setattr(ti, "_get_or_create_session", explode)
    result = await browser_tool(action="pool_status")

    assert result.success is True
    assert result.metadata is not None
    assert "max_sessions" in result.metadata["pool"]
