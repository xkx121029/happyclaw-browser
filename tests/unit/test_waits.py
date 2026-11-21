"""Wait primitives: what a timeout actually tells the caller.

The fake client below is a scripted CDP page — the wait loop, its backoff,
its success conditions and the diagnostic payload it builds are all the real
ones. Real-browser coverage of the same primitives lives in
``tests/integration/test_waits_real.py``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from happyclaw_browser.waits import (
    wait_for_navigation,
    wait_for_network_idle,
    wait_for_selector,
)


class _ScriptedClient:
    """Answers ``Runtime.evaluate`` from a state list; records listeners."""

    def __init__(self, states: list[dict[str, Any]], *, pending: int = 0) -> None:
        self.states = states
        self.pending = pending
        self.evaluations = 0
        self.listeners: dict[str, list[Any]] = {}
        self.commands: list[str] = []

    async def send(
        self, method: str, params: dict[str, Any] | None = None, **_kwargs: Any
    ) -> dict[str, Any]:
        self.commands.append(method)
        if method == "Runtime.evaluate":
            index = min(self.evaluations, len(self.states) - 1)
            self.evaluations += 1
            return {"result": {"value": json.dumps(self.states[index])}}
        if method == "Network.enable":
            for callback in self.listeners.get("Network.requestWillBeSent", []):
                for index in range(self.pending):
                    callback(
                        {
                            "requestId": f"r{index}",
                            "request": {
                                "url": (
                                    "https://tracker.example/collect"
                                    if index == 0
                                    else "https://api.example/poll"
                                )
                            },
                            "__client": self,
                        }
                    )
        return {}

    def on(self, event: str, callback: Any) -> None:
        self.listeners.setdefault(event, []).append(callback)

    def off(self, event: str, callback: Any) -> None:
        listeners = self.listeners.get(event, [])
        if callback in listeners:
            listeners.remove(callback)


def _state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "url": "https://app.example/login",
        "title": "Sign in",
        "ready_state": "complete",
        "interactive": 4,
        "matches": 0,
        "visible": 0,
    }
    state.update(overrides)
    return state


async def test_selector_timeout_explains_itself():
    client = _ScriptedClient([_state(matches=0)])

    result = await wait_for_selector(
        client,  # type: ignore[arg-type]
        "#submit",
        timeout_ms=60,
        screenshot_on_timeout=False,
    )

    assert result.success is False
    payload = (result.metadata or {})["wait"]
    assert payload["condition"] == "selector"
    assert payload["selector"] == "#submit"
    assert payload["url"] == "https://app.example/login"
    assert payload["interactive_elements"] == 4
    assert payload["selector_matches"] == 0
    assert "matched no element" in payload["hint"]
    assert "https://app.example/login" in (result.error or "")
    assert "interactive_elements=4" in (result.error or "")


async def test_selector_timeout_names_an_invisible_match():
    client = _ScriptedClient([_state(matches=1, visible=0)])

    result = await wait_for_selector(
        client,  # type: ignore[arg-type]
        ".cookie-ok",
        timeout_ms=60,
        screenshot_on_timeout=False,
    )

    payload = (result.metadata or {})["wait"]
    assert payload["selector_matches"] == 1
    assert payload["selector_visible"] == 0
    assert "none was visible" in payload["hint"]


async def test_selector_timeout_on_a_blank_page_says_so():
    client = _ScriptedClient([_state(interactive=0)])

    result = await wait_for_selector(
        client,  # type: ignore[arg-type]
        "#app",
        timeout_ms=60,
        screenshot_on_timeout=False,
    )

    assert "no interactive elements" in (result.metadata or {})["wait"]["hint"]


async def test_selector_succeeds_as_soon_as_it_is_visible():
    client = _ScriptedClient([_state(matches=1, visible=1)])

    result = await wait_for_selector(
        client,  # type: ignore[arg-type]
        "#submit",
        timeout_ms=2000,
        screenshot_on_timeout=False,
    )

    assert result.success is True
    assert client.evaluations == 1, "an already-true condition returns at once"
    assert (result.metadata or {})["wait"]["selector"] == "#submit"


async def test_selector_hidden_state_is_satisfied_when_not_visible():
    client = _ScriptedClient([_state(matches=1, visible=0)])

    result = await wait_for_selector(
        client,  # type: ignore[arg-type]
        ".spinner",
        state="hidden",
        timeout_ms=2000,
        screenshot_on_timeout=False,
    )

    assert result.success is True


async def test_selector_rejects_an_unknown_state():
    client = _ScriptedClient([_state()])

    with pytest.raises(ValueError, match="Unknown state"):
        await wait_for_selector(
            client,  # type: ignore[arg-type]
            "#submit",
            state="shiny",
        )


async def test_navigation_timeout_reports_readiness_and_seen_navigations():
    client = _ScriptedClient([_state(ready_state="loading")])

    result = await wait_for_navigation(
        client,  # type: ignore[arg-type]
        timeout_ms=60,
        screenshot_on_timeout=False,
    )

    payload = (result.metadata or {})["wait"]
    assert result.success is False
    assert payload["condition"] == "navigation"
    assert payload["ready_state"] == "loading"
    assert payload["navigations_seen"] == 0
    assert "still loading" in payload["hint"]


async def test_navigation_succeeds_when_the_target_url_already_landed():
    client = _ScriptedClient([_state(url="https://app.example/dashboard")])

    result = await wait_for_navigation(
        client,  # type: ignore[arg-type]
        url_contains="/dashboard",
        timeout_ms=2000,
        screenshot_on_timeout=False,
    )

    assert result.success is True
    assert "/dashboard" in (result.metadata or {})["wait"]["url"]


async def test_network_idle_returns_when_nothing_is_in_flight():
    client = _ScriptedClient([_state()])

    result = await wait_for_network_idle(
        client,  # type: ignore[arg-type]
        idle_ms=10,
        timeout_ms=2000,
        screenshot_on_timeout=False,
    )

    assert result.success is True
    assert (result.metadata or {})["wait"]["condition"] == "network_idle"
    assert "Network.enable" in client.commands


async def test_network_idle_timeout_names_the_requests_still_open():
    client = _ScriptedClient([_state()], pending=2)

    result = await wait_for_network_idle(
        client,  # type: ignore[arg-type]
        idle_ms=50,
        timeout_ms=80,
        screenshot_on_timeout=False,
    )

    payload = (result.metadata or {})["wait"]
    assert result.success is False
    assert payload["pending_requests"] == 2
    assert "https://tracker.example/collect" in payload["pending_urls"]
    assert "still in flight" in payload["hint"]


async def test_wait_removes_every_network_listener_afterwards():
    client = _ScriptedClient([_state()])

    await wait_for_network_idle(
        client,  # type: ignore[arg-type]
        idle_ms=10,
        timeout_ms=2000,
        screenshot_on_timeout=False,
    )

    assert all(not listeners for listeners in client.listeners.values())
