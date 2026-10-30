from __future__ import annotations

from pathlib import Path

import fitz
import pytest
import httpx

from app.qwenvl import QwenVLClient, VisionParseError


def make_pdf(path: Path, pages: int) -> None:
    document = fitz.open()
    for number in range(1, pages + 1):
        page = document.new_page(width=200, height=200)
        page.insert_text((20, 40), f"page {number}")
    document.save(path)
    document.close()


def test_short_pdf_is_sent_once(tmp_path, monkeypatch) -> None:
    pdf = tmp_path / "short.pdf"
    make_pdf(pdf, 10)
    client = QwenVLClient("http://vl/v1", "key", "qwen-vl", page_threshold=10, batch_pages=3, dpi=72)
    calls: list[tuple[str, int]] = []

    def complete(prompt: str, images: list[Path]) -> str:
        calls.append((prompt, len(images)))
        return "```markdown\n# 完整文档\n```"

    monkeypatch.setattr(client, "_complete", complete)
    markdown, metadata = client.parse(pdf, tmp_path / "artifacts")
    assert calls == [(client.short_prompt, 10)]
    assert "# 完整文档" in markdown
    assert metadata["vision_batch_count"] == 1


def test_long_pdf_is_split_and_saved(tmp_path, monkeypatch) -> None:
    pdf = tmp_path / "long.pdf"
    make_pdf(pdf, 11)
    client = QwenVLClient("http://vl/v1", "key", "qwen-vl", page_threshold=10, batch_pages=3, dpi=72)
    calls: list[tuple[str, int]] = []

    def complete(prompt: str, images: list[Path]) -> str:
        calls.append((prompt, len(images)))
        return f"## 批次 {len(calls)}"

    monkeypatch.setattr(client, "_complete", complete)
    markdown, metadata = client.parse(pdf, tmp_path / "artifacts")
    assert [count for _, count in calls] == [3, 3, 3, 2]
    assert "第 1 至 3 页" in calls[0][0]
    assert "第 10 至 11 页" in calls[-1][0]
    assert metadata["vision_batch_count"] == 4
    assert len(list((tmp_path / "artifacts" / "vision-batches").glob("*.md"))) == 4
    assert "Qwen-VL pages: 10-11" in markdown


def test_compatible_request_and_truncation(tmp_path, monkeypatch) -> None:
    image = tmp_path / "page.jpg"
    image.write_bytes(b"jpeg-test")
    calls = []
    finish_reason = "stop"

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "choices": [{"finish_reason": finish_reason, "message": {"content": "```markdown\n# 标题\n```"}}],
        })

    monkeypatch.setattr("app.qwenvl.httpx.post", post)
    client = QwenVLClient("http://internal/model-api", "test-key", "qwen-vl")
    assert client._complete("提取正文", [image]) == "# 标题"
    assert calls[0][0] == "http://internal/model-api/chat/completions"
    content = calls[0][1]["json"]["messages"][0]["content"]
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    finish_reason = "length"
    with pytest.raises(RuntimeError, match="长度上限"):
        client._complete("提取正文", [image])


def test_empty_batch_identifies_failed_pages(tmp_path, monkeypatch) -> None:
    pdf = tmp_path / "empty.pdf"
    make_pdf(pdf, 11)
    client = QwenVLClient("http://vl/v1", "key", "qwen-vl", batch_pages=3, dpi=72)
    monkeypatch.setattr(client, "_complete", lambda prompt, images: "")
    with pytest.raises(VisionParseError) as error:
        client.parse(pdf, tmp_path / "artifacts")
    assert error.value.metadata["vision_failed_pages"] == [1, 3]


def test_vision_final_markdown_bypasses_rewrite(ingestion, sample_pdf) -> None:
    class Vision:
        def parse(self, path, artifact_dir):
            return "# 最终视觉结果\n\n会议事项。", {"parser_engine": "qwenvl", "vision_batch_count": 1}

    class RewriteMustNotRun:
        configured = True

        def rewrite_markdown(self, *args):
            raise AssertionError("视觉输出不应二次改写")

    ingestion.vision = Vision()
    ingestion.llm = RewriteMustNotRun()
    source, _ = ingestion.ingest(sample_pdf, rewrite_enabled=True)
    assert source.parse_status == "success"
    assert Path(source.local_md_path).read_text(encoding="utf-8").startswith("# 最终视觉结果")
    assert source.rewrite_status == "confirmed"
