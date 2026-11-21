"""Reliable wait primitives that explain themselves when they time out.

The original ``BrowserSession.wait()`` reports failure as a bare
``"Timeout waiting for condition"`` string, which leaves the caller guessing:
was the page slow, blank, stuck behind a cookie banner, or gone? These
primitives capture the page state at the moment the deadline expires — URL,
title, document readiness, how many interactive elements actually rendered,
which network requests were still in flight, and a screenshot path — and put
it in ``ToolResult.metadata["wait"]``, keeping a one-line summary in
``error`` so a log line is still readable.

Three conditions cover almost every "did it work yet" question:

- :func:`wait_for_selector` — attached / visible / hidden / detached.
- :func:`wait_for_navigation` — a real main-frame navigation completed.
- :func:`wait_for_network_idle` — nothing has been in flight for ``idle_ms``.

Every poll is one ``Runtime.evaluate`` round trip. The interval starts at
20 ms and doubles to 250 ms, so a condition that flips immediately is noticed
immediately and a slow page costs a bounded number of evaluates.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from happyclaw_browser.actions import capture
from happyclaw_browser.cdp.client import CDPClient
from happyclaw_browser.dom.refs import RefCache
from happyclaw_browser.models import ActionMetrics, ToolResult
from happyclaw_browser.settings import HappyclawSettings
from happyclaw_browser.settings import settings as _default_settings

_POLL_START_SECONDS = 0.02
_POLL_MAX_SECONDS = 0.25

#: What a human could click or type into. The single most useful number for
#: "did the page actually render?" — 0 means a blank shell, a login wall that
#: never painted, or an error page.
_INTERACTIVE_SELECTOR = (
    "a[href],button,input,select,textarea,"
    "[role=button],[role=link],[role=checkbox],[onclick]"
)

_DIAGNOSTIC_TEMPLATE = """
(() => {
  const sel = __SELECTOR__;
  const nodes = sel ? document.querySelectorAll(sel) : [];
  let visible = 0;
  for (const el of nodes) {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    if ((r.width || r.height) && cs.visibility !== 'hidden' &&
        cs.display !== 'none' && Number(cs.opacity) !== 0) { visible++; }
  }
  return JSON.stringify({
    url: location.href,
    title: document.title || '',
    ready_state: document.readyState,
    interactive: document.querySelectorAll(__INTERACTIVE__).length,
    matches: sel ? nodes.length : -1,
    visible: visible
  });
})()
"""

_SELECTOR_STATES = ("attached", "visible", "hidden", "detached")


class _Deadline:
    """Backoff-aware polling clock."""

    def __init__(self, timeout_ms: int) -> None:
        self._start = time.monotonic()
        self._deadline = self._start + max(int(timeout_ms), 0) / 1000.0
        self._interval = _POLL_START_SECONDS

    @property
    def remaining(self) -> float:
        return self._deadline - time.monotonic()

    @property
    def expired(self) -> bool:
        return self.remaining <= 0

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._start) * 1000)

    async def pause(self) -> None:
        await asyncio.sleep(min(self._interval, max(self.remaining, 0)))
        self._interval = min(self._interval * 2, _POLL_MAX_SECONDS)


def _diagnostic_expression(selector: str | None) -> str:
    return _DIAGNOSTIC_TEMPLATE.replace(
        "__SELECTOR__", json.dumps(selector) if selector else "null"
    ).replace("__INTERACTIVE__", json.dumps(_INTERACTIVE_SELECTOR))


async def page_diagnostics(
    client: CDPClient, selector: str | None = None
) -> dict[str, Any]:
    """Return the page's current state, tolerating a dead or navigated page.

    Callers use this both while waiting and when reporting a failure, so it
    never raises: an unreadable page yields ``{"error": "..."}`` and the
    caller reports what it does know.
    """
    try:
        response = await client.send(
            "Runtime.evaluate",
            {
                "expression": _diagnostic_expression(selector),
                "returnByValue": True,
            },
        )
        raw = response.get("result", {}).get("value", "{}")
        data = json.loads(raw) if isinstance(raw, str) else {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # pylint: disable=broad-except
        return {"error": str(exc)}


async def _timeout_screenshot(
    client: CDPClient,
    ref_cache: RefCache | None,
    cfg: HappyclawSettings,
    *,
    label: str,
) -> str | None:
    """Capture the page at the moment of failure; ``None`` when impossible.

    A screenshot taken at the deadline is usually the entire answer for a
    human ("the cookie banner covered the button"), so this is worth one
    extra CDP round trip — but it must never mask the timeout itself.
    """
    if ref_cache is None:
        return None
    target = Path(cfg.screenshots_dir) / f"wait-timeout-{label}-{int(time.time())}.png"
    try:
        result = await capture.screenshot(
            client,
            ref_cache,
            path=target,
            settings=cfg,
        )
    except Exception:  # pylint: disable=broad-except
        return None
    return str(result.content) if result.success else None


def _as_int(value: Any, default: int) -> int:
    """Coerce a diagnostic field, keeping 0 distinct from "absent"."""
    return value if isinstance(value, int) else default


def _hint(
    *,
    condition: str,
    diagnostics: dict[str, Any],
    selector: str | None,
    state: str | None,
) -> str:
    """Explain the most likely reason this condition never held."""
    if "error" in diagnostics:
        return (
            "the page could not be inspected at all — the tab may have "
            "navigated away, crashed, or been closed"
        )
    ready_state = str(diagnostics.get("ready_state") or "")
    interactive = _as_int(diagnostics.get("interactive"), 0)
    matches = _as_int(diagnostics.get("matches"), -1)
    visible = _as_int(diagnostics.get("visible"), -1)
    blank = (
        "the page rendered no interactive elements at all — it may be blank, "
        "blocked by a bot wall, or serving an error page"
    )
    if ready_state and ready_state != "complete":
        return f"the document was still loading (readyState={ready_state})"
    if selector and matches == 0:
        # What the caller asked about comes first; a page with nothing to
        # click on is the explanation, not the headline.
        if interactive == 0:
            return f"the selector {selector!r} matched no element, and {blank}"
        return f"the selector {selector!r} matched no element"
    if interactive == 0:
        return blank
    if selector and state == "visible" and matches > 0 and visible == 0:
        return (
            f"{selector!r} matched {matches} element(s) but none was visible "
            "(zero size, display:none, hidden, or transparent)"
        )
    if condition == "network_idle":
        return (
            "requests were still in flight for the whole window — a long "
            "poll, a websocket, or blocked analytics keeps the page busy"
        )
    if condition == "navigation":
        return "no main-frame navigation completed inside the window"
    return "the condition never became true"


def _failure(
    *,
    action: str,
    condition: str,
    deadline: _Deadline,
    timeout_ms: int,
    diagnostics: dict[str, Any],
    selector: str | None = None,
    state: str | None = None,
    screenshot: str | None = None,
    extra: dict[str, Any] | None = None,
) -> ToolResult:
    """Build a timeout result whose payload answers "why didn't it happen"."""
    payload: dict[str, Any] = {
        "condition": condition,
        "selector": selector,
        "state": state,
        "timeout_ms": int(timeout_ms),
        "elapsed_ms": deadline.elapsed_ms,
        "url": diagnostics.get("url", ""),
        "title": diagnostics.get("title", ""),
        "ready_state": diagnostics.get("ready_state", ""),
        "interactive_elements": _as_int(diagnostics.get("interactive"), 0),
        "selector_matches": _as_int(diagnostics.get("matches"), -1),
        "selector_visible": _as_int(diagnostics.get("visible"), -1),
        "screenshot": screenshot,
        "hint": _hint(
            condition=condition,
            diagnostics=diagnostics,
            selector=selector,
            state=state,
        ),
    }
    if extra:
        payload.update(extra)
    what = f"selector {selector!r} to become {state}" if selector else condition
    message = (
        f"Timeout after {int(timeout_ms)}ms waiting for {what}. "
        f"url={payload['url'] or '(unknown)'} "
        f"interactive_elements={payload['interactive_elements']} "
        f"ready_state={payload['ready_state'] or '(unknown)'} "
        f"screenshot={screenshot or '(none)'} — {payload['hint']}"
    )
    return ToolResult(
        success=False,
        content="",
        error=message,
        metrics=ActionMetrics(
            action=action,
            duration_ms=deadline.elapsed_ms,
            dom_nodes_scanned=0,
            estimated_tokens=0,
        ),
        metadata={"wait": payload},
    )


def _success(
    *,
    action: str,
    content: str,
    deadline: _Deadline,
    payload: dict[str, Any],
) -> ToolResult:
    return ToolResult(
        success=True,
        content=content,
        metrics=ActionMetrics(
            action=action,
            duration_ms=deadline.elapsed_ms,
            dom_nodes_scanned=0,
            estimated_tokens=0,
        ),
        metadata={"wait": {**payload, "elapsed_ms": deadline.elapsed_ms}},
    )


async def wait_for_selector(
    client: CDPClient,
    selector: str,
    *,
    timeout_ms: int = 10000,
    state: str = "visible",
    ref_cache: RefCache | None = None,
    settings: HappyclawSettings | None = None,
    screenshot_on_timeout: bool = True,
) -> ToolResult:
    """Wait for ``selector`` to become attached / visible / hidden / detached.

    ``state="visible"`` is the default because "it exists in the DOM" is
    almost never what an agent means: pages routinely ship hidden dropdowns
    and pre-rendered panels that a bare ``querySelector`` would accept while
    the thing the user wants to click is still off-screen.
    """
    if state not in _SELECTOR_STATES:
        raise ValueError(f"Unknown state {state!r}. Valid: {list(_SELECTOR_STATES)}")
    cfg = settings or _default_settings
    deadline = _Deadline(timeout_ms)
    diagnostics: dict[str, Any] = {}

    while True:
        diagnostics = await page_diagnostics(client, selector)
        if "error" not in diagnostics:
            matches = int(diagnostics.get("matches") or 0)
            visible = int(diagnostics.get("visible") or 0)
            satisfied = {
                "attached": matches > 0,
                "visible": visible > 0,
                "hidden": visible == 0,
                "detached": matches == 0,
            }[state]
            if satisfied:
                return _success(
                    action="wait_for_selector",
                    content=f"Selector {selector!r} is {state}",
                    deadline=deadline,
                    payload={
                        "condition": "selector",
                        "selector": selector,
                        "state": state,
                        "url": diagnostics.get("url", ""),
                        "interactive_elements": int(
                            diagnostics.get("interactive") or 0
                        ),
                    },
                )
        if deadline.expired:
            screenshot = (
                await _timeout_screenshot(
                    client,
                    ref_cache,
                    cfg,
                    label=f"selector-{abs(hash(selector)) % 10000}",
                )
                if screenshot_on_timeout
                else None
            )
            return _failure(
                action="wait_for_selector",
                condition="selector",
                deadline=deadline,
                timeout_ms=timeout_ms,
                diagnostics=diagnostics,
                selector=selector,
                state=state,
                screenshot=screenshot,
            )
        await deadline.pause()


async def wait_for_navigation(
    client: CDPClient,
    *,
    url_contains: str | None = None,
    timeout_ms: int = 30000,
    ref_cache: RefCache | None = None,
    settings: HappyclawSettings | None = None,
    screenshot_on_timeout: bool = True,
) -> ToolResult:
    """Wait for a main-frame navigation to finish.

    Success means a real ``Page.frameNavigated`` for the main frame *and*
    ``document.readyState == "complete"` — a URL that changed while the
    spinner is still turning is not a loaded page. When ``url_contains`` is
    given it also has to match the landed URL; when the page already sits on
    that URL the wait returns immediately, so retries are idempotent.
    """
    cfg = settings or _default_settings
    deadline = _Deadline(timeout_ms)
    navigations: list[str] = []

    def _on_navigated(params: dict[str, Any]) -> None:
        frame = params.get("frame") or {}
        if isinstance(frame, dict) and not frame.get("parentId"):
            navigations.append(str(frame.get("url", "")))

    client.on("Page.frameNavigated", _on_navigated)
    diagnostics: dict[str, Any] = {}
    try:
        while True:
            diagnostics = await page_diagnostics(client, None)
            current = str(diagnostics.get("url") or "")
            loaded = str(diagnostics.get("ready_state") or "") == "complete"
            # With ``url_contains`` the landed URL is the evidence, which also
            # makes a retry of an already-finished navigation return at once.
            # Without it, only a real main-frame navigation event counts.
            landed = url_contains in current if url_contains else bool(navigations)
            if loaded and landed:
                if ref_cache is not None:
                    # Refs are node ids from the pre-navigation DOM.
                    ref_cache.invalidate()
                return _success(
                    action="wait_for_navigation",
                    content=f"Navigation complete: {current}",
                    deadline=deadline,
                    payload={
                        "condition": "navigation",
                        "url": current,
                        "title": diagnostics.get("title", ""),
                        "navigations": len(navigations),
                        "interactive_elements": int(
                            diagnostics.get("interactive") or 0
                        ),
                    },
                )
            if deadline.expired:
                screenshot = (
                    await _timeout_screenshot(
                        client, ref_cache, cfg, label="navigation"
                    )
                    if screenshot_on_timeout
                    else None
                )
                return _failure(
                    action="wait_for_navigation",
                    condition="navigation",
                    deadline=deadline,
                    timeout_ms=timeout_ms,
                    diagnostics=diagnostics,
                    screenshot=screenshot,
                    extra={
                        "url_contains": url_contains,
                        "navigations_seen": len(navigations),
                    },
                )
            await deadline.pause()
    finally:
        client.off("Page.frameNavigated", _on_navigated)


async def wait_for_network_idle(
    client: CDPClient,
    *,
    idle_ms: int = 500,
    timeout_ms: int = 30000,
    ref_cache: RefCache | None = None,
    settings: HappyclawSettings | None = None,
    screenshot_on_timeout: bool = True,
) -> ToolResult:
    """Wait until no request has been in flight for ``idle_ms``.

    Bounded by ``timeout_ms`` as well, because plenty of real pages never go
    idle: a websocket, a long-poll, or a blocked analytics beacon keeps a
    request open forever. The timeout report names the requests that were
    still open, which is normally the difference between "the app is slow"
    and "google-analytics is unreachable from this box".
    """
    cfg = settings or _default_settings
    deadline = _Deadline(timeout_ms)
    pending: dict[str, str] = {}
    last_event = time.monotonic()

    def _on_request(params: dict[str, Any]) -> None:
        nonlocal last_event
        last_event = time.monotonic()
        request_id = params.get("requestId")
        if isinstance(request_id, str):
            request = params.get("request") or {}
            url = request.get("url") if isinstance(request, dict) else ""
            pending[request_id] = str(url or "")

    def _on_done(params: dict[str, Any]) -> None:
        nonlocal last_event
        last_event = time.monotonic()
        request_id = params.get("requestId")
        if isinstance(request_id, str):
            pending.pop(request_id, None)

    client.on("Network.requestWillBeSent", _on_request)
    client.on("Network.loadingFinished", _on_done)
    client.on("Network.loadingFailed", _on_done)
    try:
        await client.send("Network.enable")
        diagnostics: dict[str, Any] = {}
        while True:
            diagnostics = await page_diagnostics(client, None)
            loaded = str(diagnostics.get("ready_state") or "") == "complete"
            quiet_for = time.monotonic() - last_event
            if loaded and not pending and quiet_for * 1000 >= idle_ms:
                return _success(
                    action="wait_for_network_idle",
                    content=f"Network idle for {int(quiet_for * 1000)}ms",
                    deadline=deadline,
                    payload={
                        "condition": "network_idle",
                        "url": diagnostics.get("url", ""),
                        "title": diagnostics.get("title", ""),
                        "idle_ms": int(idle_ms),
                        "interactive_elements": int(
                            diagnostics.get("interactive") or 0
                        ),
                    },
                )
            if deadline.expired:
                still_open = [url for url in pending.values() if url][:5]
                screenshot = (
                    await _timeout_screenshot(
                        client, ref_cache, cfg, label="network-idle"
                    )
                    if screenshot_on_timeout
                    else None
                )
                return _failure(
                    action="wait_for_network_idle",
                    condition="network_idle",
                    deadline=deadline,
                    timeout_ms=timeout_ms,
                    diagnostics=diagnostics,
                    screenshot=screenshot,
                    extra={
                        "idle_ms": int(idle_ms),
                        "pending_requests": len(pending),
                        "pending_urls": still_open,
                    },
                )
            await deadline.pause()
    finally:
        client.off("Network.requestWillBeSent", _on_request)
        client.off("Network.loadingFinished", _on_done)
        client.off("Network.loadingFailed", _on_done)
