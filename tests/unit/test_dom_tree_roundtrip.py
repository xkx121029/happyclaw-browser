"""``dom_tree`` needs the DOM and the page's url/title.

Both are independent CDP reads, so a serialized implementation pays two round
trips for one action the agent performs on every step.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from octop_browser.dom.refs import RefCache
from octop_browser.session import BrowserSession
from octop_browser.settings import OctopSettings

_DOM = {"root": {"nodeId": 1, "nodeType": 9, "children": []}}


class _ConcurrencyClient:
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

    async def send(self, method: str, params=None, *, track_activity: bool = True):
        await self._track(method)
        if method == "DOM.getDocument":
            return _DOM
        if method == "Runtime.evaluate":
            return {"result": {"value": '{"url":"https://x","title":"t"}'}}
        return {}


def _session(client) -> BrowserSession:
    fake = SimpleNamespace(
        client=client, ref_cache=RefCache(), _cfg=OctopSettings()
    )
    return BrowserSession(fake, "bench")  # type: ignore[arg-type]


async def test_dom_tree_fetches_dom_and_page_info_together():
    client = _ConcurrencyClient()

    result = await _session(client).dom_tree()

    assert result.success
    assert client.commands == ["DOM.getDocument", "Runtime.evaluate"]
    assert client.peak == 2, "the DOM and the title are independent reads"


async def test_dom_tree_still_reports_a_failure_from_either_read():
    class _Failing(_ConcurrencyClient):
        async def send(self, method, params=None, *, track_activity=True):
            if method == "DOM.getDocument":
                raise RuntimeError("DOM domain unavailable")
            return await super().send(method, params, track_activity=track_activity)

    result = await _session(_Failing()).dom_tree()

    assert not result.success
    assert "DOM domain unavailable" in (result.error or "")
