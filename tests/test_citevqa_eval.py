from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.evaluate_citevqa import evaluate_item, gold_pages, load_records


def test_citevqa_annotation_loading_and_page_extraction(tmp_path) -> None:
    path = tmp_path / "items.json"
    path.write_text(json.dumps([{"question": "问题", "answer": "答案", "evidence": [{"page": 3}]}], ensure_ascii=False), encoding="utf-8")
    rows = load_records(path)
    assert rows[0]["question"] == "问题"
    assert gold_pages(rows[0]) == {3}


def test_citevqa_official_field_names_are_supported() -> None:
    row = {"Question": "问题", "Standard_Answer": "答案", "Evidence": [{"source_pdf_name": "paper.pdf", "source_page_id": 4}]}
    assert gold_pages(row) == {4}


def test_citevqa_evaluator_scores_answer_document_and_page() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "answer": "答案", "sources": [{"filename": "paper.pdf", "pages": [3]}], "run_id": "run_1"
    }))
    with httpx.Client(transport=transport) as client:
        result = evaluate_item({"question": "问题", "answer": "答案", "pdf": "paper.pdf", "page": 3}, client, "http://test/ask")
    assert result["answer_exact"] is True
    assert result["document_recall"] is True
    assert result["page_recall"] is True
