from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx


class DifyWorkflowClient:
    def __init__(self, base_url: str, api_key: str, app_id: str = "", timeout: float = 60, markdown_input: str = "markdown", file_input: str = "file", output_field: str = "markdown"):
        self.base_url = base_url.rstrip("/").removesuffix("/v1")
        self.api_key = api_key
        self.app_id = app_id
        self.timeout = timeout
        self.markdown_input = markdown_input
        self.file_input = file_input
        self.output_field = output_field

    def run(self, source_path: Path, markdown: str, filename: str, file_input: str, markdown_input: str, output_field: str) -> tuple[str, dict[str, Any]]:
        # Dify file inputs are represented by the uploaded file object in workflow inputs.
        with source_path.open("rb") as handle:
            upload = httpx.post(
                f"{self.base_url}/v1/files/upload",
                headers={"Authorization": f"Bearer {self.api_key}"},
                data={"user": "agentic-rag"},
                files={"file": (filename, handle, "application/octet-stream")},
                timeout=self.timeout,
            )
        upload.raise_for_status()
        file_obj = upload.json()
        inputs: dict[str, Any] = {markdown_input or self.markdown_input: markdown, file_input or self.file_input: file_obj}
        response = httpx.post(
            f"{self.base_url}/v1/workflows/run",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={"inputs": inputs, "response_mode": "blocking", "user": "agentic-rag"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        result = response.json()
        outputs = ((result.get("data") or {}).get("outputs") or result.get("outputs") or {})
        value = outputs.get(output_field or self.output_field) if isinstance(outputs, dict) else None
        if isinstance(value, dict):
            text = value.get("markdown") or value.get("text")
        else:
            text = value
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError(f"Workflow 输出中没有有效的 {output_field} Markdown")
        return text.strip(), result
