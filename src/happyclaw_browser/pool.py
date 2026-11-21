"""Bounded, profile-keyed pool of live browser sessions.

Why this exists: one browser per task means every repeat of the same job
pays a cold start (a fresh profile, a Chrome boot, a CDP handshake) and
leaves a stray Chrome behind when the task forgets to close it. The pool:

- **reuses** the session for a profile instead of relaunching it;
- **bounds** how many browsers may be live (``pool_max_sessions``) and makes
  the overflow *wait* for a slot rather than fail, because a self-hosted
  user running two tasks at once should not see a hard error;
- **reclaims** a session that has sat unused for the idle timeout, killing
  its Chrome so no orphan process and no held debug port is left;
- **rebuilds** a session whose browser died, instead of handing the caller a
  corpse and letting it fail one action later.

Sessions are created one at a time — the creation path holds the pool's
condition, so N simultaneous first calls produce one Chrome, not N. That
serialisation is deliberate: concurrent cold starts on one machine are the
most reliable way to make every one of them slow.

The pool never logs in on its own behalf; ``profile`` still means the same
Chrome user-data-dir, so login persistence is unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from happyclaw_browser.mode import BrowserMode

if TYPE_CHECKING:
    from happyclaw_browser.profile import ProfileManager
    from happyclaw_browser.session import BrowserSession
    from happyclaw_browser.settings import HappyclawSettings

logger = logging.getLogger(__name__)

SessionFactory = Callable[..., Awaitable["BrowserSession"]]

#: How often the reaper looks for idle sessions. Cheap: it only touches
#: bookkeeping until something is actually reclaimable.
DEFAULT_REAP_INTERVAL_SECONDS = 5.0


class PoolExhaustedError(RuntimeError):
    """Every slot stayed busy for longer than the configured acquire timeout."""


class SessionPool:
    """Keep a bounded set of browser sessions alive, keyed by profile name."""

    def __init__(
        self,
        *,
        max_sessions: int | None = None,
        acquire_timeout: float | None = None,
        idle_timeout_seconds: float | None = None,
        settings: HappyclawSettings | None = None,
        profile_manager: ProfileManager | None = None,
        session_factory: SessionFactory | None = None,
        reap_interval_seconds: float = DEFAULT_REAP_INTERVAL_SECONDS,
    ) -> None:
        """Create a pool; every argument is optional and resolved lazily.

        Args:
            max_sessions: Live-session ceiling. Defaults to
                ``settings.pool_max_sessions``.
            acquire_timeout: Seconds to wait for a free slot before raising
                :class:`PoolExhaustedError`. ``0`` (default) waits forever.
            idle_timeout_seconds: Reclaim a session unused for this long.
                Defaults to ``settings.idle_timeout_minutes``; ``0`` disables
                reclaiming.
            settings: Settings object, read lazily so importing this module
                stays free.
            profile_manager: Manager backing profile creation.
            session_factory: Replacement for :meth:`_create`, used by tests to
                exercise pool semantics without a browser.
            reap_interval_seconds: Reaper tick.
        """
        self._max_sessions = max_sessions
        self._acquire_timeout = acquire_timeout
        self._idle_override = idle_timeout_seconds
        self._settings = settings
        self._profile_manager = profile_manager
        self._factory = session_factory
        self._reap_interval = reap_interval_seconds

        self._sessions: dict[str, BrowserSession] = {}
        self._inflight: dict[str, int] = {}
        self._last_used: dict[str, float] = {}
        self._creating = 0
        self._queued = 0
        self._reclaimed = 0
        self._rebuilt = 0
        self._condition = asyncio.Condition()
        self._reaper: asyncio.Task[None] | None = None
        self._closed = False

    # ------------------------------------------------------------------
    # Lookup surface
    # ------------------------------------------------------------------

    @property
    def sessions(self) -> dict[str, BrowserSession]:
        """Live sessions by profile. The dict *is* the pool's storage."""
        return self._sessions

    @property
    def max_sessions(self) -> int:
        configured = self._max_sessions
        if configured is None:
            configured = self._cfg().pool_max_sessions
        return max(int(configured), 1)

    def stats(self) -> dict[str, Any]:
        """Return counters that explain what the pool is doing right now."""
        now = time.monotonic()
        return {
            "max_sessions": self.max_sessions,
            "live_sessions": len(self._sessions),
            "creating": self._creating,
            "queued": self._queued,
            "inflight": sum(self._inflight.values()),
            "idle_timeout_seconds": self._idle_timeout_seconds(),
            "reclaimed": self._reclaimed,
            "rebuilt": self._rebuilt,
            "profiles": [
                {
                    "profile": name,
                    "inflight": self._inflight.get(name, 0),
                    "idle_seconds": round(now - self._last_used.get(name, now), 1),
                }
                for name in self._sessions
            ],
        }

    # ------------------------------------------------------------------
    # Acquisition
    # ------------------------------------------------------------------

    async def acquire(
        self,
        profile: str = "default",
        *,
        headless: bool | None = None,
        mode: BrowserMode | None = None,
        urls: list[str] | None = None,
    ) -> "BrowserSession":
        """Return the live session for *profile*, creating it if needed.

        Waits for a pool slot when every slot is taken; raises
        :class:`PoolExhaustedError` only if ``acquire_timeout`` elapsed first.
        """
        self._closed = False
        async with self._condition:
            existing = self._sessions.get(profile)
            if existing is not None:
                if self._healthy(existing):
                    self._mark_used(profile)
                    self._ensure_reaper()
                    return existing
                logger.info(
                    "Pooled browser session for %r is gone; rebuilding", profile
                )
                self._rebuilt += 1
                await self._close_locked(profile, kill=False)

            await self._await_slot_locked()
            self._creating += 1
            # Creation stays inside the lock on purpose: two simultaneous cold
            # starts on one machine halve each other's throughput and can race
            # for the same debug port. Boot is a one-time cost per profile.
            try:
                session = await self._create(
                    profile, headless=headless, mode=mode, urls=urls
                )
            finally:
                self._creating -= 1
                self._condition.notify_all()

            self._sessions[profile] = session
            self._inflight[profile] = 0
            self._mark_used(profile)
            self._ensure_reaper()
            return session

    async def release(self, profile: str) -> None:
        """Report that the caller finished using *profile*'s session."""
        async with self._condition:
            remaining = self._inflight.get(profile, 0) - 1
            if remaining > 0:
                self._inflight[profile] = remaining
            else:
                self._inflight.pop(profile, None)
            if profile in self._sessions:
                self._last_used[profile] = time.monotonic()

    async def close(self, profile: str, *, kill: bool = False) -> bool:
        """Drop *profile*'s session, optionally terminating its browser."""
        async with self._condition:
            return await self._close_locked(profile, kill=kill)

    async def close_all(self, *, kill: bool = True) -> list[str]:
        """Drop every session; returns the profiles that were closed."""
        async with self._condition:
            names = list(self._sessions)
            for name in names:
                await self._close_locked(name, kill=kill)
            return names

    async def shutdown(self, *, kill: bool = True) -> None:
        """Stop the reaper and close everything (used by tests and app exit)."""
        self._closed = True
        reaper = self._reaper
        self._reaper = None
        if reaper is not None and not reaper.done():
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper
        await self.close_all(kill=kill)

    # ------------------------------------------------------------------
    # Idle reclaim
    # ------------------------------------------------------------------

    async def reap_idle(self, *, now: float | None = None) -> list[str]:
        """Close sessions that have been idle past the timeout.

        ``kill=True`` so the browser is terminated, not just disconnected:
        a disconnected Chrome keeps its profile lock and its debug port, and
        the next launch of that profile then fails. Returns the reclaimed
        profile names.
        """
        timeout = self._idle_timeout_seconds()
        if timeout <= 0:
            return []
        moment = time.monotonic() if now is None else now
        async with self._condition:
            victims = [
                name
                for name in self._sessions
                if not self._inflight.get(name)
                and moment - self._last_used.get(name, moment) >= timeout
            ]
            for name in victims:
                logger.info(
                    "Reclaiming idle browser session %r after %.0fs", name, timeout
                )
                # Counted before the close: the session leaves the pool as soon
                # as it is popped, and a counter that lags the state it
                # describes is a trap for whoever watches these numbers.
                self._reclaimed += 1
                await self._close_locked(name, kill=True)
            return victims

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _cfg(self) -> HappyclawSettings:
        if self._settings is not None:
            return self._settings
        from happyclaw_browser.settings import settings as live  # noqa: PLC0415

        return live

    def _idle_timeout_seconds(self) -> float:
        if self._idle_override is not None:
            return max(float(self._idle_override), 0.0)
        return max(float(self._cfg().idle_timeout_minutes), 0.0) * 60.0

    def _acquire_timeout_seconds(self) -> float:
        if self._acquire_timeout is not None:
            return max(float(self._acquire_timeout), 0.0)
        return max(float(self._cfg().pool_acquire_timeout), 0.0)

    def _mark_used(self, profile: str) -> None:
        self._inflight[profile] = self._inflight.get(profile, 0) + 1
        self._last_used[profile] = time.monotonic()

    @staticmethod
    def _healthy(session: "BrowserSession") -> bool:
        probe: Any = getattr(session, "is_alive", None)
        if probe is None:
            return True
        try:
            return bool(probe() if callable(probe) else probe)
        except Exception:  # pylint: disable=broad-except
            return False

    async def _await_slot_locked(self) -> None:
        timeout = self._acquire_timeout_seconds()
        while len(self._sessions) + self._creating >= self.max_sessions:
            self._queued += 1
            try:
                if timeout > 0:
                    await asyncio.wait_for(self._condition.wait(), timeout=timeout)
                else:
                    await self._condition.wait()
            except TimeoutError:
                raise PoolExhaustedError(
                    f"No browser session slot freed within {timeout:.1f}s "
                    f"(max={self.max_sessions}, live={len(self._sessions)}, "
                    f"inflight={sum(self._inflight.values())}). Raise "
                    "BROWSER_USE_POOL_MAX_SESSIONS or close a session."
                ) from None
            finally:
                self._queued -= 1

    async def _close_locked(self, profile: str, *, kill: bool) -> bool:
        session = self._sessions.pop(profile, None)
        self._inflight.pop(profile, None)
        self._last_used.pop(profile, None)
        try:
            if session is not None:
                await session.close(kill=kill)
        except Exception:  # pylint: disable=broad-except
            logger.debug("Closing pooled session for %r raised", profile, exc_info=True)
        finally:
            self._condition.notify_all()
        return session is not None

    def _ensure_reaper(self) -> None:
        if self._idle_timeout_seconds() <= 0:
            return
        if self._reaper is not None and not self._reaper.done():
            return
        self._reaper = asyncio.create_task(
            self._reap_loop(), name="happyclaw-browser-pool-reaper"
        )

    async def _reap_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(self._reap_interval)
                await self.reap_idle()
        except asyncio.CancelledError:
            raise
        except Exception:  # pylint: disable=broad-except
            logger.exception("Browser session pool reaper failed")

    async def _create(
        self,
        profile: str,
        *,
        headless: bool | None,
        mode: BrowserMode | None,
        urls: list[str] | None,
    ) -> "BrowserSession":
        if self._factory is not None:
            return await self._factory(profile, headless=headless, mode=mode, urls=urls)
        from happyclaw_browser.session import BrowserSession  # noqa: PLC0415

        return await BrowserSession.create(
            profile=profile,
            headless=headless,
            mode=mode,
            urls=urls,
            profile_manager=self._profile_manager,
            settings=self._settings,
            enable_idle_reaper=False,
        )


_default_pool: SessionPool | None = None


def default_pool() -> SessionPool:
    """Return the process-wide pool used by ``browser_tool()``."""
    global _default_pool
    if _default_pool is None:
        _default_pool = SessionPool()
    return _default_pool


def reset_default_pool() -> None:
    """Forget the process-wide pool. Intended for tests."""
    global _default_pool
    _default_pool = None
