"""Tests for the CDP WebSocket client's connection lifecycle.

Everything here is about what happens when the socket goes away while work
is still outstanding: the client must not hold callers hostage for the full
CDP timeout, and it must not keep entries or tasks alive after close().
"""

from __future__ import annotations

import asyncio
import json

import pytest

from happyclaw_browser.cdp.client import CDPClient, CDPSessionError


class _FakeWS:
    """Minimal stand-in for a websockets ClientConnection."""

    def __init__(self, messages: tuple[str, ...] = (), close_raises: bool = False):
        self._messages = list(messages)
        self._close_raises = close_raises
        self.closed = False
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        if self._close_raises:
            raise RuntimeError("socket already dead")
        self.closed = True

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for message in self._messages:
            yield message
        await asyncio.Event().wait()


def _client_with(ws: _FakeWS, timeout: float = 30.0) -> CDPClient:
    client = CDPClient(timeout=timeout)
    client._ws = ws  # type: ignore[assignment]
    client._recv_task = asyncio.create_task(client._recv_loop())
    return client


async def _until(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0)


async def test_close_fails_in_flight_commands_instead_of_timing_out():
    """Closing the socket releases a blocked caller at once.

    Without this the caller waited out ``cdp_timeout`` (30 s here) on a
    connection that was already gone.
    """
    client = _client_with(_FakeWS(), timeout=30.0)
    pending = asyncio.create_task(client.send("Page.navigate", {"url": "https://x"}))
    await _until(lambda: bool(client._pending))

    await client.close()

    with pytest.raises(CDPSessionError, match="Connection closed"):
        await asyncio.wait_for(pending, timeout=2.0)
    assert client._pending == {}


async def test_cancelled_send_leaves_no_pending_entry():
    """A cancelled command must not keep its slot until the session dies."""
    client = _client_with(_FakeWS(), timeout=30.0)
    pending = asyncio.create_task(client.send("Runtime.evaluate"))
    await _until(lambda: bool(client._pending))

    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    assert client._pending == {}
    await client.close()


async def test_listener_coroutines_are_tracked_and_cancelled_on_close():
    """Event callbacks that return a coroutine are held so they cannot be
    garbage-collected mid-flight, and close() cancels the stragglers."""
    client = CDPClient()
    fired = asyncio.Event()

    async def slow_listener(_params):
        fired.set()
        await asyncio.sleep(60)

    client.on("Page.loadEventFired", slow_listener)
    client._ws = _FakeWS((json.dumps({"method": "Page.loadEventFired", "params": {}}),))
    client._recv_task = asyncio.create_task(client._recv_loop())

    await asyncio.wait_for(fired.wait(), timeout=2.0)
    assert len(client._callback_tasks) == 1

    await client.close()

    assert client._callback_tasks == set()


async def test_close_survives_a_socket_that_raises_on_close():
    client = _client_with(_FakeWS(close_raises=True))

    await client.close()

    assert client._ws is None
    assert client._pending == {}


async def test_close_is_idempotent():
    client = _client_with(_FakeWS())

    await client.close()
    await client.close()

    assert client._ws is None


async def test_recv_loop_failure_releases_waiters():
    """A dropped connection resolves in-flight commands with the cause."""
    client = CDPClient(timeout=30.0)

    class _DyingWS(_FakeWS):
        def __aiter__(self):
            return self._die()

        async def _die(self):
            raise ConnectionResetError("peer vanished")
            yield  # pragma: no cover

    client._ws = _DyingWS()  # type: ignore[assignment]
    pending = asyncio.create_task(client.send("Page.enable"))
    await _until(lambda: bool(client._pending))

    # Start reading only once the command is registered, so the failure
    # below actually has something to release.
    client._recv_task = asyncio.create_task(client._recv_loop())

    with pytest.raises(CDPSessionError, match="Connection lost"):
        await asyncio.wait_for(pending, timeout=2.0)

    await client.close()
