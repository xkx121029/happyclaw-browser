"""Unified artifact API: path convention, size ceiling, file references.

The fake client answers the CDP calls the capture path really issues, so the
path resolution, the JPEG downgrade and the ceiling check are the shipped
code. Real captures are covered in ``tests/integration/test_waits_real.py``.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from happyclaw_browser.artifacts import capture_artifact
from happyclaw_browser.dom.refs import RefCache
from happyclaw_browser.settings import HappyclawSettings


class _FakePage:
    """Minimal CDP page: screenshots, print-to-PDF and page HTML."""

    def __init__(
        self,
        *,
        html: str = "<html><body><button>ok</button></body></html>",
        png_bytes: bytes = b"png-payload",
        jpeg_bytes: bytes = b"jpeg",
    ) -> None:
        self.html = html
        self.png_bytes = png_bytes
        self.jpeg_bytes = jpeg_bytes
        self.commands: list[str] = []

    async def send(
        self, method: str, params: dict[str, Any] | None = None, **_kwargs: Any
    ) -> dict[str, Any]:
        self.commands.append(method)
        params = params or {}
        if method == "Page.captureScreenshot":
            payload = (
                self.jpeg_bytes if params.get("format") == "jpeg" else self.png_bytes
            )
            return {"data": base64.b64encode(payload).decode()}
        if method == "Page.getLayoutMetrics":
            return {"cssVisualViewport": {"clientWidth": 1280, "clientHeight": 720}}
        if method == "Page.printToPDF":
            return {"data": base64.b64encode(b"%PDF-1.4 fake").decode()}
        if method == "Runtime.evaluate":
            expression = str(params.get("expression", ""))
            if "outerHTML" in expression:
                return {"result": {"value": self.html}}
            return {
                "result": {
                    "value": json.dumps(
                        {"url": "https://app.example/inbox", "title": "Inbox"}
                    )
                }
            }
        raise AssertionError(f"unexpected CDP command {method}")


def _settings(tmp_path: Path, *, max_bytes: int = 16 * 1024 * 1024):
    return HappyclawSettings(
        artifacts_dir=tmp_path / "artifacts",
        screenshots_dir=tmp_path / "screenshots",
        artifact_max_bytes=max_bytes,
    )


async def test_dom_artifact_writes_a_file_and_returns_a_reference(tmp_path):
    page = _FakePage()
    cfg = _settings(tmp_path)

    result = await capture_artifact(
        page,  # type: ignore[arg-type]
        RefCache(),
        "dom",
        settings=cfg,
    )

    assert result.success is True
    ref = (result.metadata or {})["artifact"]
    path = Path(str(ref["path"]))
    assert path == Path(str(result.content))
    assert path.exists()
    assert path.parent == tmp_path / "artifacts" / "dom"
    assert path.suffix == ".html"
    assert ref["kind"] == "dom"
    assert ref["size_bytes"] == path.stat().st_size
    assert ref["url"] == "https://app.example/inbox"
    assert "button" in path.read_text(encoding="utf-8")


async def test_pdf_artifact_is_written_under_the_pdf_directory(tmp_path):
    page = _FakePage()
    cfg = _settings(tmp_path)

    result = await capture_artifact(
        page,  # type: ignore[arg-type]
        RefCache(),
        "pdf",
        settings=cfg,
    )

    assert result.success is True
    ref = (result.metadata or {})["artifact"]
    path = Path(str(ref["path"]))
    assert path.parent == tmp_path / "artifacts" / "pdf"
    assert path.read_bytes().startswith(b"%PDF")
    assert "Page.printToPDF" in page.commands


async def test_screenshot_artifact_reports_dimensions(tmp_path):
    page = _FakePage()
    cfg = _settings(tmp_path)

    result = await capture_artifact(
        page,  # type: ignore[arg-type]
        RefCache(),
        "screenshot",
        settings=cfg,
    )

    assert result.success is True
    ref = (result.metadata or {})["artifact"]
    assert ref["format"] == "png"
    assert ref["width"] == 1280
    assert ref["height"] == 720
    assert Path(str(ref["path"])).parent == tmp_path / "artifacts" / "screenshot"


async def test_artifact_over_the_ceiling_is_refused_not_written(tmp_path):
    page = _FakePage(html="<html>" + "x" * 5000 + "</html>")
    cfg = _settings(tmp_path, max_bytes=100)

    result = await capture_artifact(
        page,  # type: ignore[arg-type]
        RefCache(),
        "dom",
        settings=cfg,
    )

    assert result.success is False
    assert "ceiling" in (result.error or "")
    assert (result.metadata or {})["artifact"]["kind"] == "dom"
    assert list((tmp_path / "artifacts" / "dom").glob("*")) == []


async def test_oversized_screenshot_falls_back_to_jpeg(tmp_path):
    page = _FakePage(png_bytes=b"p" * 4000, jpeg_bytes=b"j" * 200)
    cfg = _settings(tmp_path, max_bytes=1000)

    result = await capture_artifact(
        page,  # type: ignore[arg-type]
        RefCache(),
        "screenshot",
        settings=cfg,
    )

    assert result.success is True
    ref = (result.metadata or {})["artifact"]
    assert ref["format"] == "jpeg"
    path = Path(str(ref["path"]))
    assert path.suffix == ".jpg"
    assert path.stat().st_size == 200
    assert [p for p in (tmp_path / "artifacts" / "screenshot").glob("*.png")] == []


async def test_explicit_relative_path_resolves_under_the_kind_directory(tmp_path):
    page = _FakePage()
    cfg = _settings(tmp_path)

    result = await capture_artifact(
        page,  # type: ignore[arg-type]
        RefCache(),
        "dom",
        path="reports/inbox.html",
        settings=cfg,
    )

    assert result.success is True
    assert Path(str(result.content)) == (
        tmp_path / "artifacts" / "dom" / "reports" / "inbox.html"
    )


async def test_unknown_kind_is_a_programming_error(tmp_path):
    page = _FakePage()

    with pytest.raises(ValueError, match="Unknown artifact kind"):
        await capture_artifact(
            page,  # type: ignore[arg-type]
            RefCache(),
            "video",
            settings=_settings(tmp_path),
        )
