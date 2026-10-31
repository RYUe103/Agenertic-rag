from __future__ import annotations

import json
import mimetypes
import time
import zipfile
import io
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

    def parse(self, file_path: Path, artifact_dir: Path | None = None) -> tuple[str, dict[str, Any]]:
        mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        with file_path.open("rb") as handle, httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                self.url,
                # MinerU deployments commonly retain the historical pdf_file
                # field while accepting Office/text files by filename. Keep
                # that field for compatibility and send the actual MIME type.
                files={"pdf_file": (file_path.name, handle, mime_type)},
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
        output_dir = artifact_dir or file_path.parent
        output_dir.mkdir(parents=True, exist_ok=True)
        response_path = output_dir / "mineru-response.json"
        response_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if isinstance(payload, dict):
            content_list = payload.get('content_list') or payload.get('contentList')
            if isinstance(content_list, list):
                (output_dir / 'mineru-content-list.json').write_text(json.dumps(content_list, ensure_ascii=False), encoding='utf-8')

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
            "parser_file_type": file_path.suffix.casefold().lstrip('.') or "unknown",
            "parser_requested": "mineru",
            "mineru_response_field": response_field,
            "mineru_markdown_characters": len(markdown),
            "mineru_content_list_returned": bool(
                isinstance(payload, dict) and ("content_list" in payload or "contentList" in payload)
            ),
            "mineru_original_path": str(original_path.resolve()),
            "mineru_response_path": str(response_path.resolve()),
        }


class MinerUCloudClient:
    """Adapter for the official MinerU cloud v4 asynchronous API.

    It keeps the same parse() contract as the local HTTP adapter. The cloud
    service uploads a file to a presigned URL, then exposes an asynchronous
    extraction result; this adapter polls until the Markdown result is ready.
    """

    def __init__(self, base_url: str, api_key: str, timeout: float = 600, poll_seconds: float = 2.0, fallback_on_error: bool = False):
        if not base_url.strip() or not api_key.strip():
            raise ValueError("MinerU 云端模式需要 MINERU_URL 和 MINERU_API_KEY")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.timeout = max(float(timeout), 30.0)
        self.poll_seconds = max(float(poll_seconds), 0.2)
        self.fallback_on_error = fallback_on_error

    def parse(self, file_path: Path, artifact_dir: Path | None = None) -> tuple[str, dict[str, Any]]:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        started = time.monotonic()
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{self.base_url}/v4/file-urls/batch",
                headers={**headers, "Content-Type": "application/json"},
                json={"files": [{"name": file_path.name}], "model_version": "vlm"},
            )
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            batch_id = str((data or {}).get("batch_id") or (payload or {}).get("batch_id") or "")
            urls = (data or {}).get("file_urls") or (payload or {}).get("file_urls") or []
            if not batch_id or not urls:
                raise RuntimeError("MinerU 云端接口未返回 batch_id 或上传地址")
            with file_path.open("rb") as handle:
                # The returned URL is a signed object-store request. Adding a
                # Content-Type header changes the signed request and can cause
                # a 403, so upload the bytes without extra headers.
                upload = client.put(str(urls[0]), content=handle.read())
                upload.raise_for_status()
            result_payload: dict[str, Any] | None = None
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                result = client.get(f"{self.base_url}/v4/extract-results/batch/{batch_id}", headers=headers)
                result.raise_for_status()
                result_payload = result.json()
                if _cloud_result_ready(result_payload):
                    break
                time.sleep(self.poll_seconds)
            if not result_payload or not _cloud_result_ready(result_payload):
                raise TimeoutError("MinerU 云端解析超时")
        markdown = _cloud_markdown(result_payload, file_path, artifact_dir or file_path.parent)
        if not markdown:
            raise RuntimeError("MinerU 云端结果中没有找到 Markdown 内容")
        output_dir = artifact_dir or file_path.parent
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "mineru-response.json").write_text(json.dumps(result_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        original_path = output_dir / "mineru-original.md"
        original_path.write_text(markdown.strip() + "\n", encoding="utf-8")
        return markdown.strip() + "\n", {"parser_engine": "mineru_cloud", "parser_requested": "mineru_cloud",
            "mineru_batch_id": _cloud_batch_id(result_payload), "mineru_elapsed_ms": round((time.monotonic() - started) * 1000),
            "mineru_original_path": str(original_path.resolve()), "mineru_response_path": str((output_dir / "mineru-response.json").resolve())}


def _cloud_batch_id(payload: Any) -> str:
    data = payload.get("data") if isinstance(payload, dict) else None
    return str((data or {}).get("batch_id") or (payload or {}).get("batch_id") or "")


def _cloud_results(payload: Any) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    values = (data or {}).get("extract_result") or (data or {}).get("extract_results") or (payload or {}).get("extract_result") or []
    return [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []


def _cloud_result_ready(payload: Any) -> bool:
    results = _cloud_results(payload)
    if not results:
        return False
    statuses = {str(item.get("state") or item.get("status") or "").casefold() for item in results}
    return bool(statuses & {"done", "success", "completed", "failed", "error"})


def _cloud_markdown(payload: Any, file_path: Path, artifact_dir: Path | None = None) -> str:
    for item in _cloud_results(payload):
        for key in ("md_content", "markdown", "md", "text"):
            if isinstance(item.get(key), str) and item[key].strip():
                return item[key]
        for key in ("full_md_url", "md_url", "download_url", "full_zip_url"):
            value = item.get(key)
            if not isinstance(value, str) or not value:
                continue
            response = httpx.get(value, timeout=120)
            response.raise_for_status()
            if key.endswith("zip_url") or response.content[:2] == b"PK":
                if artifact_dir:
                    artifact_dir.mkdir(parents=True, exist_ok=True)
                    (artifact_dir / 'mineru-result.zip').write_bytes(response.content)
                with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                    lists = [name for name in archive.namelist() if name.endswith('_content_list.json')]
                    if artifact_dir and len(lists) == 1:
                        (artifact_dir / 'mineru-content-list.json').write_bytes(archive.read(lists[0]))
                    candidates = [name for name in archive.namelist() if name.casefold().endswith(".md")]
                    if candidates:
                        return archive.read(candidates[0]).decode("utf-8", errors="replace")
            return response.text
    return ""


def _find_markdown(value: Any, path: str = "$") -> tuple[str, str]:
    if isinstance(value, dict):
        # Known response fields are authoritative even when Markdown consists
        # entirely of a single-line HTML table.
        for key in ("md_content", "markdown", "full_md", "md", "content", "text"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate, f"{path}.{key}"
        for key, candidate in value.items():
            if key in {"content_list", "contentList"}:
                continue
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
    page: int | None = None
    for item in items:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = item.get("text") or item.get("table_body") or item.get("content") or item.get("md")
            if isinstance(text, str) and text.strip():
                page_index = item.get("page_idx")
                if isinstance(page_index, int) and not isinstance(page_index, bool) and page_index >= 0 and page_index + 1 != page:
                    page = page_index + 1
                    parts.append(f"<!-- page: {page} -->")
                level = item.get("text_level")
                if isinstance(level, int) and not isinstance(level, bool) and 1 <= level <= 6 and not text.lstrip().startswith("#"):
                    text = f"{'#' * level} {text}"
                parts.append(text)
    return "\n\n".join(parts)
