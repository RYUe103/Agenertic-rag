#!/usr/bin/env python3
"""Run a CiteVQA-style evaluation against the running Agent API.

The evaluator intentionally does not download or fabricate dataset files. Point
it at the annotation file and a knowledge base that already contains the
corresponding PDFs parsed by MinerU.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx


def first(value: dict[str, Any], names: tuple[str, ...], default: Any = None) -> Any:
    for name in names:
        if name in value and value[name] not in (None, ""):
            return value[name]
    return default


def load_records(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("data", "questions", "items", "annotations"):
            values = payload.get(key)
            if isinstance(values, list):
                return [item for item in values if isinstance(item, dict)]
    raise ValueError("无法识别标注文件格式：需要 JSON 数组、JSONL 或包含 data/questions/items 的对象")


def text_value(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return " / ".join(text_value(item) for item in value if text_value(item))
    if isinstance(value, dict):
        for key in ("text", "answer", "value", "content"):
            if key in value:
                return text_value(value[key])
    return str(value).strip() if value is not None else ""


def normalize(value: str) -> str:
    return "".join(re.findall(r"[\w\u3400-\u9fff]+", value.casefold()))


def f1_score(expected: str, actual: str) -> float:
    left, right = set(normalize(expected)), set(normalize(actual))
    if not left or not right:
        return 1.0 if left == right else 0.0
    common = len(left & right)
    precision, recall = common / len(right), common / len(left)
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def gold_pages(item: dict[str, Any]) -> set[int]:
    values: list[Any] = []
    for key in ("page", "pages", "page_id", "evidence_pages"):
        if key in item:
            values.append(item[key])
    evidence = first(item, ("evidence", "Evidence", "citations", "grounding"), [])
    if isinstance(evidence, list):
        values.extend(evidence)
    pages: set[int] = set()
    for value in values:
        if isinstance(value, dict):
            value = first(value, ("page", "pages", "page_id", "source_page_id"), None)
        if isinstance(value, list):
            values.extend(value)
            continue
        try:
            page = int(value)
            if page > 0:
                pages.add(page)
        except (TypeError, ValueError):
            continue
    return pages


def evaluate_item(item: dict[str, Any], client: httpx.Client, endpoint: str) -> dict[str, Any]:
    question = text_value(first(item, ("question", "Question", "query", "prompt"), ""))
    expected = text_value(first(item, ("answer", "answers", "gold_answer", "standard_answer", "Standard Answer"), ""))
    expected_file = text_value(first(item, ("filename", "file_name", "pdf", "document", "paper"), ""))
    evidence = first(item, ("evidence", "Evidence", "citations", "grounding"), [])
    if not expected_file and isinstance(evidence, list):
        expected_file = text_value(next((entry.get("source_pdf_name") for entry in evidence if isinstance(entry, dict) and entry.get("source_pdf_name")), ""))
    if not question:
        raise ValueError("标注缺少问题字段")
    started = time.monotonic()
    response = client.post(endpoint, json={"question": question, "conversation_id": "citevqa-eval"})
    elapsed = round((time.monotonic() - started) * 1000)
    response.raise_for_status()
    payload = response.json()
    actual = text_value(payload.get("answer"))
    sources = payload.get("sources") or []
    source_names = [text_value(source.get("filename")) for source in sources if isinstance(source, dict)]
    returned_pages = {int(page) for source in sources if isinstance(source, dict) for page in (source.get("pages") or []) if str(page).isdigit()}
    expected_pages = gold_pages(item)
    return {
        "question": question,
        "expected_answer": expected,
        "answer": actual,
        "answer_exact": bool(expected) and normalize(expected) == normalize(actual),
        "answer_f1": f1_score(expected, actual) if expected else None,
        "expected_file": expected_file,
        "source_filenames": source_names,
        "document_recall": bool(expected_file) and any(expected_file.casefold() in name.casefold() or name.casefold() in expected_file.casefold() for name in source_names),
        "expected_pages": sorted(expected_pages),
        "returned_pages": sorted(returned_pages),
        "page_recall": bool(expected_pages & returned_pages) if expected_pages else None,
        "elapsed_ms": elapsed,
        "run_id": payload.get("run_id"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--annotations", type=Path, required=True, help="CiteVQA JSON/JSONL 标注文件")
    parser.add_argument("--endpoint", default="http://127.0.0.1:8765/api/agents/@kb:default-kb/ask")
    parser.add_argument("--bearer", default="", help="外部 API Key；不传则使用本地会话/禁用认证环境")
    parser.add_argument("--limit", type=int, default=0, help="仅评测前 N 题，0 表示全部")
    parser.add_argument("--output", type=Path, default=Path("data/citevqa/results.jsonl"))
    args = parser.parse_args()
    records = load_records(args.annotations)
    if args.limit > 0:
        records = records[:args.limit]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": f"Bearer {args.bearer}"} if args.bearer else {}
    results: list[dict[str, Any]] = []
    failures = 0
    with httpx.Client(timeout=180, headers=headers) as client, args.output.open("w", encoding="utf-8") as output:
        for index, item in enumerate(records, 1):
            try:
                result = evaluate_item(item, client, args.endpoint)
            except Exception as exc:
                failures += 1
                result = {"error": type(exc).__name__, "message": str(exc)[:500]}
            result["index"] = index
            output.write(json.dumps(result, ensure_ascii=False) + "\n")
            output.flush()
            results.append(result)
            print(f"[{index}/{len(records)}] {'失败' if 'error' in result else '完成'}", file=sys.stderr)
    scored = [item for item in results if "error" not in item]
    def average(key: str) -> float | None:
        values = [float(item[key]) for item in scored if item.get(key) is not None]
        return round(sum(values) / len(values), 4) if values else None
    summary = {"total": len(records), "scored": len(scored), "failures": failures,
               "answer_exact": average("answer_exact"), "answer_f1": average("answer_f1"),
               "document_recall": average("document_recall"), "page_recall": average("page_recall"),
               "avg_elapsed_ms": average("elapsed_ms"), "results": str(args.output)}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if failures and not scored else 0


if __name__ == "__main__":
    raise SystemExit(main())
