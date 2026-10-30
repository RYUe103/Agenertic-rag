from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import Any

import httpx

from app.schemas import VectorHit


class DifyClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        dataset_id: str,
        timeout: float = 30,
        search_method: str = "semantic_search",
        chunking_mode: str = "automatic",
        chunk_separator: str = "\n__SPLIT__\n",
        chunk_max_tokens: int = 800,
    ):
        # The configured base URL is the complete Dify API base.  Keep its path
        # exactly as entered; callers may use either /datasets or /v1/datasets.
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.dataset_id = dataset_id
        self.timeout = timeout
        self.search_method = search_method
        self.chunking_mode = chunking_mode
        self.chunk_separator = chunk_separator
        self.chunk_max_tokens = chunk_max_tokens

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        transport = httpx.HTTPTransport(retries=3)
        with httpx.Client(timeout=self.timeout, transport=transport) as client:
            response = client.request(method, f"{self.base_url}{path}", headers=self.headers, **kwargs)
            response.raise_for_status()
            if response.status_code == 204:
                return {}
            try:
                return response.json()
            except ValueError as exc:
                preview = " ".join(response.text[:300].split())
                raise RuntimeError(
                    f"Dify API 返回的不是 JSON：HTTP {response.status_code}，URL={response.url}，响应={preview!r}"
                ) from exc

    def upload_document(self, file_path: Path, name: str) -> str:
        data = {"data": json.dumps(self._process_rule(), ensure_ascii=False)}
        mime_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        with file_path.open("rb") as handle:
            result = self._request(
                "POST",
                f"/datasets/{self.dataset_id}/document/create-by-file",
                data=data,
                files={"file": (name, handle, mime_type)},
            )
        document = result.get("document") or {}
        document_id = document.get("id")
        if not document_id:
            raise RuntimeError("Dify 上传响应中缺少 document.id")
        return str(document_id)

    def _process_rule(self) -> dict[str, Any]:
        if self.chunking_mode == "custom":
            return {
                "indexing_technique": "high_quality",
                "process_rule": {"mode": "custom", "rules": {
                    "pre_processing_rules": [],
                    "segmentation": {"separator": self.chunk_separator, "max_tokens": self.chunk_max_tokens},
                }},
            }
        return {"indexing_technique": "high_quality", "process_rule": {"mode": "automatic"}}

    def document_status(self, document_id: str) -> str:
        document = self.document_info(document_id)
        return str(document.get("indexing_status", "unknown")) if document else "not_found"

    def document_info(self, document_id: str) -> dict[str, Any] | None:
        for page in range(1, 101):
            documents = self.list_documents(page=page, limit=100)
            match = next((item for item in documents if item.get("id") == document_id), None)
            if match:
                return match
            if len(documents) < 100:
                break
        return None

    def retry_document(self, document_id: str, file_path: Path, name: str) -> bool:
        data = {"data": json.dumps({"name": name, **self._process_rule()}, ensure_ascii=False)}
        mime_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        try:
            with file_path.open("rb") as handle:
                self._request(
                    "POST",
                    f"/datasets/{self.dataset_id}/documents/{document_id}/update-by-file",
                    data=data,
                    files={"file": (name, handle, mime_type)},
                )
            return True
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 400 and "Document is not available" in exc.response.text:
                return False
            raise

    def delete_document(self, document_id: str) -> None:
        try:
            self._request("DELETE", f"/datasets/{self.dataset_id}/documents/{document_id}")
        except httpx.HTTPStatusError as exc:
            # Deletion is idempotent: a document removed directly in Dify is
            # already gone and should not block local cleanup.
            if exc.response.status_code != 404:
                raise

    def list_documents(self, keyword: str = "", page: int = 1, limit: int = 20) -> list[dict[str, Any]]:
        result = self._request(
            "GET",
            f"/datasets/{self.dataset_id}/documents",
            params={"keyword": keyword, "page": page, "limit": limit},
        )
        return list(result.get("data") or [])

    def retrieve(self, query: str, top_k: int = 10, metadata_filtering_conditions: dict[str, Any] | None = None) -> list[VectorHit]:
        retrieval_model: dict[str, Any] = {
            "search_method": self.search_method,
            "reranking_enable": False,
            "top_k": top_k,
            "score_threshold_enabled": False,
        }
        if metadata_filtering_conditions:
            retrieval_model["metadata_filtering_conditions"] = metadata_filtering_conditions
        result = self._request(
            "POST",
            f"/datasets/{self.dataset_id}/retrieve",
            json={
                "query": query,
                "retrieval_model": retrieval_model,
            },
        )
        hits: list[VectorHit] = []
        for record in result.get("records") or []:
            segment = record.get("segment") or {}
            document = segment.get("document") or {}
            hits.append(
                VectorHit(
                    document_id=document.get("id"),
                    document_name=document.get("name"),
                    content=segment.get("content", ""),
                    score=record.get("score"),
                    segment_id=segment.get("id"),
                )
            )
        return hits[:top_k]

    def update_document_metadata(self, document_id: str, metadata_list: list[dict[str, Any]]) -> dict[str, Any]:
        return self._request(
            "POST", f"/datasets/{self.dataset_id}/documents/metadata",
            json={"document_id": document_id, "metadata_list": metadata_list},
        )

    def create_metadata_field(self, name: str, value_type: str = "string") -> dict[str, Any]:
        return self._request(
            "POST", f"/datasets/{self.dataset_id}/metadata",
            json={"name": name, "type": "number" if value_type == "number" else "string"},
        )

    def list_metadata_fields(self) -> list[dict[str, Any]]:
        result = self._request("GET", f"/datasets/{self.dataset_id}/metadata")
        return list(result.get("data") or result.get("metadata") or [])

    def get_segments(
        self, document_id: str, keyword: str = "", page: int = 1, limit: int = 20
    ) -> list[dict[str, Any]]:
        result = self._request(
            "GET",
            f"/datasets/{self.dataset_id}/documents/{document_id}/segments",
            params={"keyword": keyword, "page": page, "limit": limit},
        )
        return list(result.get("data") or [])


class MockDifyClient:
    dataset_id = "mock-dataset"

    def upload_document(self, file_path: Path, name: str) -> str:
        return f"mock-doc-{file_path.parent.name}"

    def document_status(self, document_id: str) -> str:
        return "completed"

    def document_info(self, document_id: str) -> dict[str, Any]:
        return {"id": document_id, "indexing_status": "completed", "error": None}

    def retry_document(self, document_id: str, file_path: Path, name: str) -> bool:
        return True

    def delete_document(self, document_id: str) -> None:
        return None

    def list_documents(self, keyword: str = "", page: int = 1, limit: int = 20) -> list[dict[str, Any]]:
        return []

    def retrieve(self, query: str, top_k: int = 10, metadata_filtering_conditions: dict[str, Any] | None = None) -> list[VectorHit]:
        return []

    def get_segments(
        self, document_id: str, keyword: str = "", page: int = 1, limit: int = 20
    ) -> list[dict[str, Any]]:
        return []
