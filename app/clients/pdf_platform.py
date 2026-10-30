from __future__ import annotations

import json
import mimetypes
import subprocess
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import httpx

from app.schemas import PdfUploadResult


class PdfPlatformClient(Protocol):
    def upload(self, file_path: str, filename: str | None = None, user: str | None = None) -> PdfUploadResult: ...

    def refresh_url(self, file_id: str) -> str: ...


class MockPdfPlatformClient:
    def upload(self, file_path: str, filename: str | None = None, user: str | None = None) -> PdfUploadResult:
        file_id = f"mock-{Path(file_path).parent.name}"
        return PdfUploadResult(file_id=file_id, url=f"/api/files/{file_id}")

    def refresh_url(self, file_id: str) -> str:
        return f"/api/files/{quote(file_id)}"


class HttpPdfPlatformClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 30):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def upload(self, file_path: str, filename: str | None = None, user: str | None = None) -> PdfUploadResult:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        if user:
            headers["X-User"] = user
        upload_name = filename or Path(file_path).name
        mime_type = mimetypes.guess_type(upload_name)[0] or "application/octet-stream"
        with Path(file_path).open("rb") as handle, httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{self.base_url}/upload", headers=headers, files={"file": (upload_name, handle, mime_type)}
            )
            response.raise_for_status()
        result = response.json()
        return PdfUploadResult(file_id=str(result["file_id"]), url=str(result["url"]))

    def refresh_url(self, file_id: str) -> str:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        response = httpx.get(f"{self.base_url}/files/{quote(file_id)}/url", headers=headers, timeout=self.timeout)
        response.raise_for_status()
        return str(response.json()["url"])


class CommandPdfPlatformClient:
    def __init__(self, command: list[str], url_template: str = "", timeout: float = 120):
        if not command:
            raise ValueError("PDF_PLATFORM_COMMAND 不能为空")
        self.command = command
        self.url_template = url_template
        self.timeout = timeout

    def upload(self, file_path: str, filename: str | None = None, user: str | None = None) -> PdfUploadResult:
        completed = subprocess.run(
            [*self.command, file_path], capture_output=True, text=True, timeout=self.timeout, check=True,
            env={**__import__("os").environ, **({"X_USER": user} if user else {})},
        )
        result = json.loads(completed.stdout)
        file_id = str(result["file_id"])
        url = str(result.get("url") or self.refresh_url(file_id))
        return PdfUploadResult(file_id=file_id, url=url)

    def refresh_url(self, file_id: str) -> str:
        if not self.url_template:
            raise RuntimeError("命令行适配器未返回 URL，且未配置 PDF_PLATFORM_URL_TEMPLATE")
        return self.url_template.format(file_id=quote(file_id))
