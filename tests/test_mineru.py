from __future__ import annotations

import json

import httpx
import pytest

from app.document_parser import document_to_markdown
from app.mineru import MinerUClient, MinerUCloudClient, _content_list_to_markdown, _find_markdown
from app.retrieval import locate_exact_segment


def test_md_content_accepts_single_line_html() -> None:
    markdown, field = _find_markdown({"md_content": "<table><tr><td>会议</td></tr></table>"})
    assert markdown == "<table><tr><td>会议</td></tr></table>"
    assert field == "$.md_content"


def test_content_list_accepts_json_string() -> None:
    value = json.dumps([{"text": "第一段"}, {"content": "第二段"}], ensure_ascii=False)
    assert _content_list_to_markdown(value) == "第一段\n\n第二段"


def test_content_list_keeps_page_headings_and_tables() -> None:
    content = [{"page_idx": 0, "text": "概述", "text_level": 1},
               {"page_idx": 0, "text": "正文"},
               {"page_idx": 1, "table_body": "<table><tr><td>1</td></tr></table>"}]
    assert _find_markdown({"content_list": content}) == ("", "")
    assert _content_list_to_markdown(content) == "<!-- page: 1 -->\n\n# 概述\n\n正文\n\n<!-- page: 2 -->\n\n<table><tr><td>1</td></tr></table>"


def test_segment_location_requires_unique_exact_match() -> None:
    markdown = "<!-- page: 1 -->\n# 章节\n重复\n<!-- page: 2 -->\n重复\n唯一内容"
    assert locate_exact_segment(markdown, "重复") == (None, None)
    assert locate_exact_segment(markdown, "唯一内容") == (2, "章节")
    assert locate_exact_segment(markdown, "不存在") == (None, None)


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


def test_mineru_sends_non_pdf_with_real_filename_and_mime(tmp_path, monkeypatch) -> None:
    text = tmp_path / 'notes.txt'
    text.write_text('内容', encoding='utf-8')
    response = httpx.Response(200, request=httpx.Request('POST', 'http://mineru/file_parse'), json={'md_content': '解析结果'})
    captured = {}
    class FakeClient:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, *args, **kwargs):
            captured.update(kwargs)
            return response
    monkeypatch.setattr('app.mineru.httpx.Client', FakeClient)
    markdown, metadata = MinerUClient('http://mineru/file_parse').parse(text, tmp_path)
    assert markdown == '解析结果\n'
    filename, handle, mime = captured['files']['pdf_file']
    assert filename == 'notes.txt' and mime == 'text/plain'
    assert metadata['parser_file_type'] == 'txt'


def test_mineru_cloud_uploads_and_polls_v4_api(tmp_path, monkeypatch) -> None:
    pdf = tmp_path / 'source.pdf'
    pdf.write_bytes(b'pdf')
    responses = [
        httpx.Response(200, request=httpx.Request('POST', 'https://mineru.net/api/v4/file-urls/batch'), json={'data': {'batch_id': 'batch-1', 'file_urls': ['https://upload.test/file']}}),
        httpx.Response(200, request=httpx.Request('GET', 'https://mineru.net/api/v4/extract-results/batch/batch-1'), json={'data': {'extract_result': [{'state': 'done', 'md_content': '# 云端结果'}]}}),
    ]
    calls = []
    class FakeClient:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, *args, **kwargs): calls.append(('post', url, kwargs)); return responses[0]
        def put(self, url, *args, **kwargs): calls.append(('put', url, kwargs)); return httpx.Response(200, request=httpx.Request('PUT', url))
        def get(self, url, *args, **kwargs): calls.append(('get', url, kwargs)); return responses[1]
    monkeypatch.setattr('app.mineru.httpx.Client', FakeClient)
    markdown, metadata = MinerUCloudClient('https://mineru.net/api', 'test-key', poll_seconds=0.2).parse(pdf, tmp_path)
    assert markdown == '# 云端结果\n'
    assert metadata['parser_engine'] == 'mineru_cloud'
    assert calls[0][1].endswith('/v4/file-urls/batch')
    assert calls[1][0] == 'put' and calls[2][1].endswith('/batch-1')


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


def test_mineru_is_requested_for_office_and_text_files(tmp_path, monkeypatch) -> None:
    class FakeMinerU:
        fallback_on_error = False
        def parse(self, path, artifact_dir):
            return f'# MinerU {path.suffix}', {'parser_engine': 'mineru', 'parser_requested': 'mineru'}
    docx = tmp_path / 'sample.docx'
    docx.write_bytes(b'not locally parsed')
    text = tmp_path / 'sample.txt'
    text.write_text('local text', encoding='utf-8')
    assert document_to_markdown(docx, 'docx', mineru=FakeMinerU())[1]['parser_engine'] == 'mineru'
    assert document_to_markdown(text, 'txt', mineru=FakeMinerU())[0] == '# MinerU .txt'
