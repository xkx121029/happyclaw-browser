"""One session create() should cost one CDP round trip for page setup.

``Page.enable``, ``DOM.enable``, ``Runtime.enable`` and the viewport override
are four independent commands; issuing them one after another makes the
caller wait four round trips for four answers nobody reads.
"""

from __future__ import annotations

import asyncio

from octop_browser.profile import Profile
from octop_browser.session import _InternalCDPSession
from octop_browser.settings import OctopSettings


class _ConcurrencyClient:
    """Records peak concurrency and charges a fixed RTT per command."""

    def __init__(self, rtt: float = 0.02) -> None:
        self.rtt = rtt
        self.in_flight = 0
        self.peak = 0
        self.commands: list[str] = []

    async def _track(self, name: str) -> None:
        self.commands.append(name)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        await asyncio.sleep(self.rtt)
        self.in_flight -= 1

    async def enable_domain(self, domain: str) -> None:
        await self._track(f"{domain}.enable")

    async def send(self, method: str, params=None, *, track_activity: bool = True):
        await self._track(method)
        return {}


def _internal(client) -> _InternalCDPSession:
    cfg = OctopSettings(idle_timeout_minutes=0)
    profile = Profile(name="bench", data_dir="/tmp/bench-profile", cdp_port=9222)
    internal = _InternalCDPSession(profile, cfg)
    internal._client = client  # type: ignore[assignment]
    return internal


async def test_prime_page_issues_its_four_commands_concurrently():
    client = _ConcurrencyClient()

    await _internal(client)._prime_page()

    assert client.commands == [
        "Page.enable",
        "DOM.enable",
        "Runtime.enable",
        "Emulation.setDeviceMetricsOverride",
    ]
    assert client.peak == 4, "the four setup commands must share one round trip"


async def test_prime_page_still_raises_when_a_domain_fails():
    class _Broken(_ConcurrencyClient):
        async def enable_domain(self, domain: str) -> None:
            await self._track(f"{domain}.enable")
            if domain == "DOM":
                raise RuntimeError("DOM domain unavailable")

    client = _Broken()

    try:
        await _internal(client)._prime_page()
    except RuntimeError as exc:
        assert "DOM domain unavailable" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a failed domain enable must not be swallowed")
