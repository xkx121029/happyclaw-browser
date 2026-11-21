"""``wait()`` polls for a page condition.

The old loop re-checked every 250 ms flat, so a condition that flipped almost
immediately still cost most of a tick before anything noticed. The interval
now starts at 20 ms and doubles to the old 250 ms ceiling.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from octop_browser.dom.refs import RefCache
from octop_browser.session import BrowserSession
from octop_browser.settings import OctopSettings


class _GatedClient:
    """Reports the condition as true only from the *ready_on*th evaluate."""

    def __init__(self, ready_on: float) -> None:
        self.ready_on = ready_on
        self.calls = 0
        self.sleeps: list[float] = []

    async def send(self, _method, _params=None, **_kwargs):
        self.calls += 1
        ready = self.calls >= self.ready_on
        return {
            "result": {
                "value": '{"url":"https://x","text":%s,"selector":1}'
                % ('"ready"' if ready else '""')
            }
        }


def _session(client) -> BrowserSession:
    fake = SimpleNamespace(
        client=client, ref_cache=RefCache(), _cfg=OctopSettings()
    )
    return BrowserSession(fake, "bench")  # type: ignore[arg-type]


def _recording_sleep(client: _GatedClient):
    async def _sleep(delay: float) -> None:
        client.sleeps.append(delay)

    return _sleep


async def test_wait_backs_off_and_notices_a_fast_flip(monkeypatch):
    client = _GatedClient(ready_on=3)
    monkeypatch.setattr("octop_browser.session.asyncio.sleep", _recording_sleep(client))

    result = await _session(client).wait(text="ready", timeout_ms=5000)

    assert result.success
    assert client.sleeps == [pytest.approx(0.02), pytest.approx(0.04)]


async def test_wait_reports_a_timeout_when_the_condition_never_holds(monkeypatch):
    client = _GatedClient(ready_on=float("inf"))
    monkeypatch.setattr("octop_browser.session.asyncio.sleep", _recording_sleep(client))

    result = await _session(client).wait(text="ready", timeout_ms=50)

    assert not result.success
    assert "Timeout" in (result.error or "")
    assert client.sleeps, "a timed-out wait must still have polled"
    assert client.sleeps[0] == pytest.approx(0.02)
    assert max(client.sleeps) <= 0.25, "the interval must stay capped"


async def test_wait_returns_at_once_when_the_condition_already_holds():
    client = _GatedClient(ready_on=1)

    result = await _session(client).wait(text="ready", timeout_ms=1000)

    assert result.success
    assert client.calls == 1
