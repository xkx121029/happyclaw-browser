"""Unified artifact capture: screenshot, PDF and DOM snapshot.

One entry point, one naming convention, one size ceiling, and a file
reference back to the caller instead of raw bytes. A base64 payload is
expensive to push through an agent toolchain and useless to render; what
callers actually need is "this file exists, here is where, here is how big".

Layout::

    <artifacts_dir>/<kind>/happyclaw-<kind>-<UTC timestamp>-<seq>.<ext>

``kind`` is one of ``screenshot`` / ``pdf`` / ``dom``, so a caller can point
a renderer or a cleanup job at a single directory and stop growing the
state directory without bound. ``max_bytes`` is a hard ceiling: an artifact
larger than the cap is refused with its real size, never silently truncated
and never handed back over budget.
"""

from __future__ import annotations

import base64
import itertools
import json
import time
from pathlib import Path

from happyclaw_browser.actions import capture
from happyclaw_browser.cdp.client import CDPClient
from happyclaw_browser.dom.refs import RefCache
from happyclaw_browser.models import ActionMetrics, ArtifactRef, ToolResult
from happyclaw_browser.settings import HappyclawSettings
from happyclaw_browser.settings import settings as _default_settings

ARTIFACT_KINDS = ("screenshot", "pdf", "dom")

#: Suffix / MIME-ish label per produced artifact.
_SUFFIX = {"screenshot": ".png", "pdf": ".pdf", "dom": ".html"}

_sequence = itertools.count()


def _resolve_target(
    cfg: HappyclawSettings,
    kind: str,
    path: str | Path | None,
    suffix: str,
) -> Path:
    """Return the file this artifact will occupy.

    An explicit path is honoured (relative paths resolve under the kind's
    directory); otherwise the caller gets a unique, sortable name.
    """
    base = Path(cfg.artifacts_dir) / kind
    if path is not None:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = base / candidate
        candidate.parent.mkdir(parents=True, exist_ok=True)
        return candidate
    base.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return base / f"happyclaw-{kind}-{stamp}-{next(_sequence):04d}{suffix}"


def _over_cap(actual: int, cap: int) -> bool:
    return cap > 0 and actual > cap


def _refused(
    *,
    kind: str,
    size: int,
    cap: int,
    action: str,
    extra: str = "",
) -> ToolResult:
    """Report an artifact that would exceed the ceiling instead of writing it."""
    detail = (
        f"Artifact {kind} would be {size} bytes, over the {cap}-byte ceiling"
        f"{extra}. Nothing was written. Raise BROWSER_USE_ARTIFACT_MAX_BYTES "
        "or pass a larger max_bytes."
    )
    return ToolResult(
        success=False,
        content="",
        error=detail,
        metrics=ActionMetrics(
            action=action, duration_ms=0, dom_nodes_scanned=0, estimated_tokens=0
        ),
        metadata={
            "artifact": {"kind": kind, "size_bytes": size, "max_bytes": cap},
        },
    )


async def _page_context(client: CDPClient) -> dict[str, str]:
    try:
        response = await client.send(
            "Runtime.evaluate",
            {
                "expression": (
                    "JSON.stringify({url: location.href, title: document.title})"
                ),
                "returnByValue": True,
            },
        )
        raw = response.get("result", {}).get("value", "{}")
        data = json.loads(raw) if isinstance(raw, str) else {}
    except Exception:  # pylint: disable=broad-except
        return {"url": "", "title": ""}
    return {"url": str(data.get("url", "")), "title": str(data.get("title", ""))}


async def capture_artifact(
    client: CDPClient,
    ref_cache: RefCache,
    kind: str,
    *,
    path: str | Path | None = None,
    max_bytes: int | None = None,
    element_ref: str | None = None,
    full_page: bool = False,
    print_background: bool = True,
    settings: HappyclawSettings | None = None,
) -> ToolResult:
    """Write one artifact and return a reference to it.

    Args:
        client: Connected CDP client for the active page.
        ref_cache: Ref cache, used by ``element_ref``.
        kind: ``"screenshot"`` (PNG), ``"pdf"`` (print-to-PDF) or ``"dom"``
            (``document.documentElement.outerHTML``).
        path: Optional destination. Absolute paths are used verbatim;
            relative ones resolve under the kind's artifact directory.
        max_bytes: Ceiling for this artifact. Defaults to
            ``settings.artifact_max_bytes``; ``0`` disables the check.
        element_ref: Screenshot only — clip to one element's box.
        full_page: Screenshot only — capture the whole scrollable page.
        print_background: PDF only — keep CSS backgrounds.
        settings: Settings override, defaults to the module singleton.

    Returns:
        :class:`ToolResult` whose ``content`` is the absolute path written and
        whose ``metadata["artifact"]`` is an :class:`ArtifactRef`. A capture
        that would exceed the ceiling fails with its real size and writes
        nothing.
    """
    if kind not in ARTIFACT_KINDS:
        raise ValueError(f"Unknown artifact kind {kind!r}. Valid: {ARTIFACT_KINDS}")
    cfg = settings or _default_settings
    cap = cfg.artifact_max_bytes if max_bytes is None else int(max_bytes)
    start = time.monotonic()

    if kind == "screenshot":
        target = _resolve_target(cfg, kind, path, _SUFFIX[kind])
        built = await capture.screenshot(
            client,
            ref_cache,
            element_ref=element_ref,
            full_page=full_page,
            path=target,
            settings=cfg,
        )
        if not built.success:
            return built
        size = target.stat().st_size
        image_format = "png"
        if _over_cap(size, cap):
            # A full-page PNG of a long page is mostly blank pixels; JPEG at
            # moderate quality usually lands far under the same ceiling. Do
            # that once before refusing, so the user keeps a usable image.
            target.unlink(missing_ok=True)
            target = _resolve_target(cfg, kind, path, ".jpg")
            built = await capture.screenshot(
                client,
                ref_cache,
                element_ref=element_ref,
                full_page=full_page,
                path=target,
                settings=cfg,
                image_format="jpeg",
                quality=60,
            )
            if not built.success:
                return built
            size = target.stat().st_size
            image_format = "jpeg"
            if _over_cap(size, cap):
                target.unlink(missing_ok=True)
                return _refused(
                    kind=kind,
                    size=size,
                    cap=cap,
                    action="artifact",
                    extra=" even after downgrading to JPEG",
                )
        context = await _page_context(client)
        raw_width = built.metadata.get("width") if built.metadata else None
        raw_height = built.metadata.get("height") if built.metadata else None
        return _artifact_result(
            kind=kind,
            target=target,
            size=size,
            cap=cap,
            start=start,
            context=context,
            image_format=image_format,
            width=raw_width if isinstance(raw_width, int) else 0,
            height=raw_height if isinstance(raw_height, int) else 0,
        )

    if kind == "pdf":
        target = _resolve_target(cfg, kind, path, _SUFFIX[kind])
        response = await client.send(
            "Page.printToPDF",
            {"printBackground": print_background, "preferCSSPageSize": True},
        )
        payload = base64.b64decode(str(response.get("data", "")))
        if not payload:
            return ToolResult(
                success=False,
                content="",
                error="Page.printToPDF returned no data",
                metrics=ActionMetrics(
                    action="artifact",
                    duration_ms=int((time.monotonic() - start) * 1000),
                    dom_nodes_scanned=0,
                    estimated_tokens=0,
                ),
            )
        if _over_cap(len(payload), cap):
            return _refused(kind=kind, size=len(payload), cap=cap, action="artifact")
        target.write_bytes(payload)
        context = await _page_context(client)
        return _artifact_result(
            kind=kind,
            target=target,
            size=len(payload),
            cap=cap,
            start=start,
            context=context,
            image_format="pdf",
        )

    target = _resolve_target(cfg, kind, path, _SUFFIX[kind])
    response = await client.send(
        "Runtime.evaluate",
        {
            "expression": (
                "document.documentElement ? document.documentElement.outerHTML : ''"
            ),
            "returnByValue": True,
        },
    )
    html = str(response.get("result", {}).get("value", ""))
    encoded = html.encode("utf-8")
    if _over_cap(len(encoded), cap):
        return _refused(kind=kind, size=len(encoded), cap=cap, action="artifact")
    target.write_bytes(encoded)
    context = await _page_context(client)
    return _artifact_result(
        kind=kind,
        target=target,
        size=len(encoded),
        cap=cap,
        start=start,
        context=context,
        image_format="html",
    )


def _artifact_result(
    *,
    kind: str,
    target: Path,
    size: int,
    cap: int,
    start: float,
    context: dict[str, str],
    image_format: str,
    width: int = 0,
    height: int = 0,
) -> ToolResult:
    ref = ArtifactRef(
        kind=kind,
        path=str(target),
        format=image_format,
        size_bytes=size,
        max_bytes=cap,
        url=context.get("url", ""),
        title=context.get("title", ""),
        width=width,
        height=height,
    )
    return ToolResult(
        success=True,
        content=str(target),
        metrics=ActionMetrics(
            action="artifact",
            duration_ms=int((time.monotonic() - start) * 1000),
            dom_nodes_scanned=0,
            estimated_tokens=0,
            screenshot_size_kb=size // 1024 if kind == "screenshot" else 0,
        ),
        metadata={"artifact": ref.model_dump()},
    )
