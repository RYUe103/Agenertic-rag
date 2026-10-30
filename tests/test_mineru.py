from __future__ import annotations

import json

import httpx
import pytest

from app.document_parser import document_to_markdown
from app.mineru import MinerUClient, _content_list_to_markdown, _find_markdown


def test_md_content_accepts_single_line_html() -> None:
    markdown, field = _find_markdown({"md_content": "<table><tr><td>会议</td></tr></table>"})
    assert markdown == "<table><tr><td>会议</td></tr></table>"
    assert field == "$.md_content"


def test_content_list_accepts_json_string() -> None:
    value = json.dumps([{"text": "第一段"}, {"content": "第二段"}], ensure_ascii=False)
    assert _content_list_to_markdown(value) == "第一段\n\n第二段"


def test_parse_persists_original_response(tmp_path, monkeypatch) -> None:
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"pdf")
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "http://mineru/pdf_parse"),
        json={"md_content": "<table><tr><td>会议</td></tr></table>"},
    )

    class FakeClient:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, *args, **kwargs): return response

    monkeypatch.setattr("app.mineru.httpx.Client", FakeClient)
    markdown, metadata = MinerUClient("http://mineru/pdf_parse").parse(pdf, tmp_path)

    assert markdown.startswith("<table>")
    assert metadata["parser_engine"] == "mineru"
    assert metadata["mineru_response_field"] == "$.md_content"
    assert (tmp_path / "mineru-original.md").read_text(encoding="utf-8") == markdown
    assert json.loads((tmp_path / "mineru-response.json").read_text(encoding="utf-8"))["md_content"]


def test_strict_mineru_does_not_fallback(sample_pdf, monkeypatch) -> None:
    client = MinerUClient("http://mineru/pdf_parse", fallback_on_error=False)
    monkeypatch.setattr(client, "parse", lambda *args: (_ for _ in ()).throw(RuntimeError("timeout")))
    with pytest.raises(RuntimeError, match="禁止本地回退"):
        document_to_markdown(sample_pdf, "pdf", mineru=client, artifact_dir=sample_pdf.parent)


def test_default_mineru_records_local_fallback(sample_pdf, monkeypatch) -> None:
    client = MinerUClient("http://mineru/pdf_parse", fallback_on_error=True)
    monkeypatch.setattr(client, "parse", lambda *args: (_ for _ in ()).throw(RuntimeError("timeout")))
    _, metadata = document_to_markdown(sample_pdf, "pdf", mineru=client, artifact_dir=sample_pdf.parent)
    assert metadata["parser_engine"] == "local_fallback"
    assert metadata["mineru_error"] == "timeout"
