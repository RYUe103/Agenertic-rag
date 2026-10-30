from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx


class MinerUClient:
    """Client for the internal MinerU PDF-to-Markdown service."""

    def __init__(self, url: str, timeout: float = 120, return_content_list: bool = False, return_images: bool = False, fallback_on_error: bool = True):
        if not url.strip():
            raise ValueError("MINERU_URL 不能为空")
        self.url = url.strip()
        self.timeout = timeout
        self.return_content_list = return_content_list
        self.return_images = return_images
        self.fallback_on_error = fallback_on_error

    def parse(self, pdf_path: Path, artifact_dir: Path | None = None) -> tuple[str, dict[str, Any]]:
        with pdf_path.open("rb") as handle, httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                self.url,
                files={"pdf_file": (pdf_path.name, handle, "application/pdf")},
                data={
                    "return_content_list": str(self.return_content_list).lower(),
                    "return_images": str(self.return_images).lower(),
                },
            )
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError(f"MinerU 返回的不是 JSON：{response.text[:300]!r}") from exc
        output_dir = artifact_dir or pdf_path.parent
        output_dir.mkdir(parents=True, exist_ok=True)
        response_path = output_dir / "mineru-response.json"
        response_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        markdown, response_field = _find_markdown(payload)
        if not markdown and isinstance(payload, dict):
            content_list = payload.get("content_list") or payload.get("contentList")
            markdown = _content_list_to_markdown(content_list)
            response_field = "content_list" if markdown else ""
        if not markdown:
            raise RuntimeError("MinerU 响应中没有找到 Markdown 内容")
        markdown = markdown.strip() + "\n"
        original_path = output_dir / "mineru-original.md"
        original_path.write_text(markdown, encoding="utf-8")
        return markdown, {
            "parser_engine": "mineru",
            "parser_requested": "mineru",
            "mineru_response_field": response_field,
            "mineru_markdown_characters": len(markdown),
            "mineru_content_list_returned": bool(
                isinstance(payload, dict) and ("content_list" in payload or "contentList" in payload)
            ),
            "mineru_original_path": str(original_path.resolve()),
            "mineru_response_path": str(response_path.resolve()),
        }


def _find_markdown(value: Any, path: str = "$") -> tuple[str, str]:
    if isinstance(value, dict):
        # Known response fields are authoritative even when Markdown consists
        # entirely of a single-line HTML table.
        for key in ("md_content", "markdown", "full_md", "md", "content", "text"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate, f"{path}.{key}"
        for key, candidate in value.items():
            found, found_path = _find_markdown(candidate, f"{path}.{key}")
            if found:
                return found, found_path
    if isinstance(value, list):
        for index, candidate in enumerate(value):
            found, found_path = _find_markdown(candidate, f"{path}[{index}]")
            if found:
                return found, found_path
    return "", ""


def _content_list_to_markdown(items: Any) -> str:
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except json.JSONDecodeError:
            return ""
    if not isinstance(items, list):
        return ""
    parts: list[str] = []
    for item in items:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = item.get("text") or item.get("content") or item.get("md")
            if isinstance(text, str) and text.strip():
                parts.append(text)
    return "\n\n".join(parts)
