"""Stateless browser_tool() entry point for AI frameworks.

Sessions are kept by the process-wide :class:`~happyclaw_browser.pool.SessionPool`
rather than by a plain dict, so repeated calls for a profile re-attach to the
browser it already has instead of paying a cold start, a full pool queues
instead of failing, idle browsers are reclaimed, and a session whose Chrome
died is rebuilt before the caller sees an error.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from happyclaw_browser.cdp.client import CDPSessionError
from happyclaw_browser.mode import BrowserMode
from happyclaw_browser.models import ActionMetrics, ToolResult
from happyclaw_browser.pool import SessionPool
from happyclaw_browser.profile import ProfileManager
from happyclaw_browser.session import BrowserSession
from happyclaw_browser.settings import settings

logger = logging.getLogger(__name__)

_pool = SessionPool()
#: Live sessions by profile. Same dict the pool stores in, so the idle
#: controller's ``_drop_session`` and direct inspection both stay honest.
_registry: dict[str, BrowserSession] = _pool.sessions
_global_hooks: dict[str, list[Any]] = {}

_KNOWN_ACTIONS = frozenset(
    [
        "navigate",
        "dom_tree",
        "screenshot",
        "click",
        "type",
        "fill",
        "press",
        "wait",
        "wait_for_selector",
        "wait_for_navigation",
        "wait_for_network_idle",
        "artifact",
        "session_info",
        "pool_status",
        "select",
        "scroll",
        "hover",
        "eval_js",
        "go_back",
        "go_forward",
        "reload",
        "new_tab",
        "switch_tab",
        "close_tab",
        "list_tabs",
        "close_session",
    ]
)

#: Actions answered without touching a browser. Creating one to answer a
#: status question would be a cold start per probe.
_STATUS_ACTIONS = frozenset(["session_info", "pool_status"])


def _metrics(action: str, *, duration_ms: int = 0, tokens: int = 0) -> ActionMetrics:
    return ActionMetrics(
        action=action,
        duration_ms=duration_ms,
        dom_nodes_scanned=0,
        estimated_tokens=tokens,
    )


def _error_result(action: str, message: str) -> ToolResult:
    return ToolResult(
        success=False,
        content="",
        error=message,
        metrics=_metrics(action),
    )


async def _get_or_create_session(
    profile: str,
    headless: bool | None = None,
    mode: BrowserMode | None = None,
    urls: list[str] | None = None,
) -> BrowserSession:
    return await _pool.acquire(profile, headless=headless, mode=mode, urls=urls)


async def _drop_session(profile: str) -> None:
    try:
        await _pool.close(profile)
    except Exception:  # pylint: disable=broad-except
        logger.debug(
            "Failed to close stale browser session for %s", profile, exc_info=True
        )


def _is_disconnected_error(exc: Exception) -> bool:
    if isinstance(exc, CDPSessionError):
        return (
            "not connected" in str(exc).lower() or "connection lost" in str(exc).lower()
        )
    return False


def _session_info(profile: str) -> ToolResult:
    session = _registry.get(profile)
    if session is None:
        payload: dict[str, Any] = {"profile": profile, "live": False}
    else:
        payload = {
            "profile": profile,
            "live": bool(getattr(session, "is_alive", True)),
            "cdp_port": getattr(session, "cdp_port", None),
        }
    return ToolResult(
        success=True,
        content=json.dumps(payload, ensure_ascii=False),
        metrics=_metrics("session_info"),
        metadata={"session": payload},
    )


def _pool_status() -> ToolResult:
    stats = _pool.stats()
    return ToolResult(
        success=True,
        content=json.dumps(stats, ensure_ascii=False),
        metrics=_metrics("pool_status"),
        metadata={"pool": stats},
    )


async def browser_tool(
    action: str,
    profile: str = "default",
    headless: bool | None = None,
    mode: BrowserMode | None = None,
    urls: list[str] | None = None,
    **kwargs: Any,
) -> ToolResult:
    """
    Stateless entry point for AI frameworks.

    Routes to the correct :class:`BrowserSession` by ``profile`` name, taking
    a lease from the session pool and creating the session lazily on first
    use.

    Args:
        action: One of the supported action strings.
        profile: Profile name (maps to a Chrome user-data-dir).
        headless: Legacy boolean override (only honored on first call per
            profile). Prefer ``mode``.
        mode: ``"auto"`` | ``"headed"`` | ``"headless"``. Defaults to the
            ``BROWSER_USE_MODE`` environment variable, then ``"auto"``.
            Only honored on first call per profile.
        urls: Optional list of URLs to open as separate tabs on first call
            per profile. Each URL gets its own browser tab.
        **kwargs: Action-specific parameters.

    Returns:
        A :class:`ToolResult`.
    """
    if action not in _KNOWN_ACTIONS:
        return _error_result(
            action, f"Unknown action '{action}'. Valid: {sorted(_KNOWN_ACTIONS)}"
        )

    if action in _STATUS_ACTIONS:
        if kwargs:
            return _error_result(
                action, f"'{action}' takes no parameters beyond profile"
            )
        return _pool_status() if action == "pool_status" else _session_info(profile)

    if action == "close_session":
        kill = bool(kwargs.pop("kill", False))
        if kwargs:
            return _error_result(
                action,
                f"Invalid parameters for 'close_session': {sorted(kwargs)}",
            )
        closed = await _pool.close(profile, kill=kill)
        if not closed and kill and not settings.cdp_ws_url:
            from happyclaw_browser.cdp.launcher import (  # noqa: PLC0415
                terminate_browser,
            )

            managed_profile = ProfileManager(
                base_dir=settings.profiles_dir
            ).get_or_create(profile)
            await terminate_browser(
                managed_profile,
                cdp_host=settings.cdp_host,
            )
        return ToolResult(
            success=True,
            content=f"Session '{profile}' closed.",
            metrics=_metrics("close_session"),
        )

    try:
        sess = await _get_or_create_session(
            profile, headless=headless, mode=mode, urls=urls
        )
    except Exception as exc:  # pylint: disable=broad-except
        return _error_result(action, f"Failed to create session: {exc}")

    # If urls were provided but the session already existed (so urls were
    # not applied during creation), open each URL as a new tab now.
    if urls and profile in _registry:
        for i, url in enumerate(urls):
            if i == 0:
                await sess.navigate(url)
            else:
                await sess.new_tab(url=url, force_new=True)

    try:
        method = getattr(sess, action)
        result: ToolResult = await method(**kwargs)
        return result
    except TypeError as exc:
        return _error_result(action, f"Invalid parameters for '{action}': {exc}")
    except Exception as exc:  # pylint: disable=broad-except
        if _is_disconnected_error(exc):
            logger.info(
                "browser_tool reconnecting stale session for action=%s profile=%s",
                action,
                profile,
            )
            await _drop_session(profile)
            try:
                fresh = await _get_or_create_session(
                    profile, headless=headless, mode=mode, urls=urls
                )
                method = getattr(fresh, action)
                retry_result: ToolResult = await method(**kwargs)
                return retry_result
            except TypeError as retry_exc:
                return _error_result(
                    action, f"Invalid parameters for '{action}': {retry_exc}"
                )
            except Exception as retry_exc:  # pylint: disable=broad-except
                logger.exception(
                    "browser_tool retry failed for action=%s profile=%s",
                    action,
                    profile,
                )
                return _error_result(action, str(retry_exc))
            finally:
                await _pool.release(profile)
        logger.exception("browser_tool error for action=%s profile=%s", action, profile)
        return _error_result(action, str(exc))
    finally:
        await _pool.release(profile)
