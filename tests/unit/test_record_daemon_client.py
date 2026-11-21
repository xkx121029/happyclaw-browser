"""Tests for the record daemon client helpers."""

from __future__ import annotations

import pytest

from happyclaw_browser.record import daemon


async def test_send_request_reports_missing_daemon(monkeypatch, tmp_path):
    """A missing socket must yield an actionable message, not a raw errno."""
    monkeypatch.setattr(daemon, "socket_path", lambda: tmp_path / "absent.sock")

    result = await daemon.send_request({"command": "status"})

    assert result["ok"] is False
    assert "daemon-start" in result["error"]
    assert "Errno" not in result["error"]


async def test_is_running_false_when_socket_absent(monkeypatch, tmp_path):
    """``is_running`` still resolves to False now that send_request returns a dict."""
    monkeypatch.setattr(daemon, "socket_path", lambda: tmp_path / "absent.sock")

    assert await daemon.is_running() is False


async def test_daemon_run_refuses_a_platform_without_unix_sockets(monkeypatch):
    """The server side must fail with a sentence, not an AttributeError."""
    import asyncio

    monkeypatch.delattr(asyncio, "start_unix_server", raising=False)

    with pytest.raises(RuntimeError, match="Unix domain sockets"):
        await daemon.RecordDaemon().run()
