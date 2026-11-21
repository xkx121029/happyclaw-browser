"""Measure the properties of octop-browser's hot paths that the driver owns.

Wall-clock timing is a poor witness on a busy or heavily-scanned host: a
single ``import websockets`` can cost seconds here, and Windows' ~15.6 ms
timer granularity swamps anything smaller. So this measures what is
attributable to the driver and reproduces exactly:

* how many CDP round trips an operation serializes,
* how many HTTP requests a re-attach issues,
* how long the poll loops wait before their first and second re-check,
* how many modules the top-level import pulls in,
* how quickly ``close()`` releases a caller blocked on a reply.

Run it against any revision:

    python scripts/bench_hot_paths.py
    python scripts/bench_hot_paths.py --json
    python scripts/bench_hot_paths.py --browser   # adds a real launch
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from octop_browser.cdp import launcher as launcher_mod  # noqa: E402

#: Simulated CDP round-trip time for the fakes below.
RTT_SECONDS = 0.02

_VERSION_BODY = b'{"webSocketDebuggerUrl":"ws://127.0.0.1/devtools/browser/bench"}'


class _RoundTripClient:
    """Charges one simulated round trip per command and records concurrency."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.in_flight = 0
        self.peak = 0

    async def _track(self, name: str) -> None:
        self.commands.append(name)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        await asyncio.sleep(RTT_SECONDS)
        self.in_flight -= 1

    async def connect(self, _ws_url: str) -> None:
        await self._track("connect")

    async def enable_domain(self, domain: str) -> None:
        await self._track(f"{domain}.enable")

    async def send(self, method: str, _params: Any = None, **_kw: Any) -> dict:
        await self._track(method)
        if method == "DOM.getDocument":
            return {"root": {"nodeId": 1, "nodeType": 9, "children": []}}
        if method == "Runtime.evaluate":
            return {"result": {"value": '{"url":"https://x","title":"t"}'}}
        return {}

    async def close(self) -> None:
        return None


def _waves(client: _RoundTripClient, skip: tuple[str, ...] = ()) -> int:
    """Round trips the caller waits through, given the observed concurrency."""
    counted = [c for c in client.commands if c not in skip]
    if not counted:
        return 0
    return math.ceil(len(counted) / max(client.peak, 1))


def _http_response(body: bytes) -> bytes:
    return (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\n\r\n"
        + body
    )


async def _free_port() -> int:
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    server.close()
    await server.wait_closed()
    return port


_REQUESTS = {"version": 0}


async def _cdp_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    await reader.readuntil(b"\r\n\r\n")
    _REQUESTS["version"] += 1
    writer.write(_http_response(_VERSION_BODY))
    await writer.drain()
    writer.close()


async def bench_launch_poll_schedule() -> list[dict[str, Any]]:
    """The intervals the launch poll waits before re-checking Chrome.

    The stand-in CDP endpoint answers with an empty payload for its first few
    requests and then succeeds, so the recorded sleeps are exactly the poll's
    own schedule. (A server that bound late would put its own sleep in the
    list instead.)
    """
    intervals: list[float] = []
    real_sleep = asyncio.sleep
    ready_after = 3
    seen = {"n": 0}

    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        await reader.readuntil(b"\r\n\r\n")
        seen["n"] += 1
        writer.write(_http_response(b"{}" if seen["n"] <= ready_after else _VERSION_BODY))
        await writer.drain()
        writer.close()

    async def _capture(delay: float) -> None:
        intervals.append(round(delay, 4))
        await real_sleep(0)

    port = await _free_port()
    server = await asyncio.start_server(_handler, "127.0.0.1", port)
    asyncio.sleep = _capture  # type: ignore[assignment]
    try:
        await launcher_mod._get_ws_url(port, retries=6, delay=0.25)
    finally:
        asyncio.sleep = real_sleep  # type: ignore[assignment]
        server.close()
        await server.wait_closed()
    return [
        {
            "name": "launch_poll_intervals_s",
            "detail": "wait before each re-check of Chrome's CDP port",
            "value": intervals[:3],
        }
    ]


async def bench_attach_probe_count() -> list[dict[str, Any]]:
    from octop_browser.profile import Profile

    port = await _free_port()
    server = await asyncio.start_server(_cdp_handler, "127.0.0.1", port)
    _REQUESTS["version"] = 0
    profile = Profile(
        name="attach-bench",
        data_dir=Path.home() / ".capy" / "work" / "bench-profiles" / "attach-bench",
        cdp_port=port,
    )
    try:
        await launcher_mod.launch_or_attach(profile, headless=True)
    finally:
        server.close()
        await server.wait_closed()
    return [
        {
            "name": "attach_version_requests",
            "detail": "/json/version requests issued to re-attach",
            "value": _REQUESTS["version"],
        }
    ]


async def bench_session_connect() -> list[dict[str, Any]]:
    from octop_browser.profile import Profile
    from octop_browser.session import _InternalCDPSession
    from octop_browser.settings import OctopSettings

    client = _RoundTripClient()
    cfg = OctopSettings(cdp_ws_url="ws://stub", idle_timeout_minutes=0)
    profile = Profile(name="bench", data_dir="/tmp/bench-profile", cdp_port=9222)
    internal = _InternalCDPSession(profile, cfg)
    internal._client = client  # type: ignore[assignment]
    await internal.connect()
    return [
        {
            "name": "connect_round_trips",
            "detail": f"{len(client.commands)} commands, peak {client.peak} at once",
            "value": _waves(client, skip=("connect",)),
        }
    ]


async def bench_dom_tree() -> list[dict[str, Any]]:
    from octop_browser.dom.refs import RefCache
    from octop_browser.session import BrowserSession
    from octop_browser.settings import OctopSettings

    client = _RoundTripClient()
    fake = SimpleNamespace(client=client, ref_cache=RefCache(), _cfg=OctopSettings())
    await BrowserSession(fake, "bench").dom_tree()  # type: ignore[arg-type]
    return [
        {
            "name": "dom_tree_round_trips",
            "detail": f"{len(client.commands)} commands, peak {client.peak} at once",
            "value": _waves(client),
        }
    ]


async def bench_wait_schedule() -> list[dict[str, Any]]:
    from octop_browser.dom.refs import RefCache
    from octop_browser.session import BrowserSession
    from octop_browser.settings import OctopSettings

    intervals: list[float] = []
    real_sleep = asyncio.sleep

    class _NeverReady:
        async def send(self, _method, _params=None, **_kw):
            return {"result": {"value": '{"url":"","text":"","selector":1}'}}

    async def _capture(delay: float) -> None:
        intervals.append(round(delay, 4))
        await real_sleep(0)

    fake = SimpleNamespace(
        client=_NeverReady(), ref_cache=RefCache(), _cfg=OctopSettings()
    )
    session = BrowserSession(fake, "bench")  # type: ignore[arg-type]
    asyncio.sleep = _capture  # type: ignore[assignment]
    try:
        await session.wait(text="ready", timeout_ms=120)
    finally:
        asyncio.sleep = real_sleep  # type: ignore[assignment]
    return [
        {
            "name": "wait_poll_intervals_s",
            "detail": "wait before each re-check of the page condition",
            "value": intervals[:3] or ["timeout fits in one chunk"],
        }
    ]


async def bench_close_release() -> list[dict[str, Any]]:
    """How long a caller blocked on a reply waits after close()."""
    from octop_browser.cdp.client import CDPClient, CDPSessionError

    class _SilentWS:
        async def send(self, _payload):
            return None

        async def close(self):
            return None

        def __aiter__(self):
            return self._iterate()

        async def _iterate(self):
            await asyncio.Event().wait()
            yield  # pragma: no cover

    client = CDPClient(timeout=5.0)
    client._ws = _SilentWS()  # type: ignore[assignment]
    client._recv_task = asyncio.create_task(client._recv_loop())
    pending = asyncio.create_task(client.send("Page.enable"))
    while not client._pending:
        await asyncio.sleep(0)

    loop = asyncio.get_running_loop()
    closed_at = loop.time()
    await client.close()
    try:
        await pending
    except CDPSessionError:
        pass
    return [
        {
            "name": "close_release_ms",
            "detail": "caller blocked on a reply, then close(); cdp_timeout=5000 ms",
            "value": round((loop.time() - closed_at) * 1000, 1),
        }
    ]


_PROBE = """
import sys
before = set(sys.modules)
import octop_browser  # noqa: F401
loaded = set(sys.modules) - before
third = set()
for name in loaded:
    root = name.split(".")[0]
    path = str(getattr(sys.modules.get(root), "__file__", "") or "")
    if "site-packages" in path or "dist-packages" in path:
        third.add(root)
print(len(loaded), len(third), ",".join(sorted(third)))
"""


def bench_import() -> list[dict[str, Any]]:
    out = subprocess.run(
        [sys.executable, "-c", _PROBE], check=True, capture_output=True, text=True
    ).stdout.split()
    return [
        {
            "name": "import_modules",
            "detail": "modules loaded by `import octop_browser`",
            "value": int(out[0]),
        },
        {
            "name": "import_thirdparty_roots",
            "detail": "third-party roots: " + (out[2] if len(out) > 2 else "(none)"),
            "value": int(out[1]),
        },
    ]


async def bench_real_browser() -> list[dict[str, Any]]:
    import time

    from octop_browser.cdp.launcher import terminate_browser
    from octop_browser.profile import ProfileManager
    from octop_browser.session import BrowserSession
    from octop_browser.settings import OctopSettings

    pm = ProfileManager(base_dir=Path.home() / ".capy" / "work" / "bench-profiles")
    cfg = OctopSettings(idle_timeout_minutes=0)
    out: list[dict[str, Any]] = []

    await terminate_browser(pm.get_or_create("bench"), cdp_host=cfg.cdp_host)
    start = time.perf_counter()
    session = await BrowserSession.create(
        profile="bench", headless=True, profile_manager=pm, settings=cfg
    )
    out.append(
        {
            "name": "browser_cold_start_ms",
            "detail": "launch + CDP attach + page setup",
            "value": round((time.perf_counter() - start) * 1000, 1),
        }
    )
    await session.close(kill=False)
    await asyncio.sleep(0.3)

    start = time.perf_counter()
    session = await BrowserSession.create(
        profile="bench", headless=True, profile_manager=pm, settings=cfg
    )
    out.append(
        {
            "name": "browser_warm_attach_ms",
            "detail": "re-attach to the running Chrome",
            "value": round((time.perf_counter() - start) * 1000, 1),
        }
    )
    await session.close(kill=True)
    await terminate_browser(pm.get_or_create("bench"), cdp_host=cfg.cdp_host)
    return out


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--browser", action="store_true")
    args = parser.parse_args()

    results: list[dict[str, Any]] = []
    results += await bench_launch_poll_schedule()
    results += await bench_attach_probe_count()
    results += await bench_session_connect()
    results += await bench_dom_tree()
    results += await bench_wait_schedule()
    results += await bench_close_release()
    results += bench_import()
    if args.browser:
        results += await bench_real_browser()

    if args.json:
        print(json.dumps(results, indent=2))
        return
    for r in results:
        print(f"{r['name']:<26} {str(r['value']):>30}  {r['detail']}")


if __name__ == "__main__":
    asyncio.run(main())
