from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="重新调用 MinerU 并核对现有 extracted.md")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="项目根目录")
    parser.add_argument("--url", default="", help="MinerU接口；默认读取.env中的MINERU_URL")
    parser.add_argument("--limit", type=int, default=20, help="最多检查多少份；0表示全部")
    parser.add_argument("--timeout", type=float, default=0, help="单份超时秒数；默认读取.env")
    parser.add_argument("--delay", type=float, default=0.2, help="请求之间暂停秒数")
    parser.add_argument("--report", type=Path, default=Path("mineru-audit.csv"))
    parser.add_argument("--resume", action="store_true", help="跳过报告中已有source_id")
    return parser.parse_args()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


def sources(root: Path) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for db_path in root.glob("data/**/sources.db"):
        with sqlite3.connect(db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """SELECT source_id,filename,created_at,local_source_path,local_extracted_md_path
                   FROM sources WHERE document_type = 'pdf' ORDER BY created_at DESC"""
            ).fetchall()
        for row in rows:
            item = {key: str(row[key] or "") for key in row.keys()}
            item["database"] = str(db_path.resolve())
            result.append(item)
    result.sort(key=lambda item: item["created_at"], reverse=True)
    return result


def existing_ids(report: Path) -> set[str]:
    if not report.exists():
        return set()
    with report.open(encoding="utf-8-sig", newline="") as handle:
        return {row["source_id"] for row in csv.DictReader(handle) if row.get("source_id")}


def mineru_markdown(url: str, pdf_path: Path, timeout: float) -> tuple[str, str]:
    with pdf_path.open("rb") as handle:
        response = httpx.post(
            url,
            files={"pdf_file": (pdf_path.name, handle, "application/pdf")},
            data={"return_content_list": "false", "return_images": "false"},
            timeout=timeout,
        )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("md_content"), str):
        keys = list(payload) if isinstance(payload, dict) else type(payload).__name__
        raise RuntimeError(f"响应缺少字符串md_content；顶层={keys}")
    return payload["md_content"], ",".join(payload)


def main() -> None:
    args = parse_args()
    root = args.root.resolve()
    env = dotenv_values(root / ".env")
    url = args.url.strip() or str(env.get("MINERU_URL") or "").strip()
    timeout = args.timeout or float(env.get("MINERU_TIMEOUT_SECONDS") or 120)
    if not url:
        raise SystemExit("请通过--url或.env中的MINERU_URL配置接口")

    report = args.report if args.report.is_absolute() else root / args.report
    report.parent.mkdir(parents=True, exist_ok=True)
    done = existing_ids(report) if args.resume else set()
    candidates = [item for item in sources(root) if item["source_id"] not in done]
    if args.limit > 0:
        candidates = candidates[: args.limit]

    fields = [
        "source_id", "filename", "created_at", "status", "same", "local_chars",
        "mineru_chars", "local_sha256", "mineru_sha256", "response_fields", "error",
        "local_source_path", "local_extracted_md_path", "database",
    ]
    append = args.resume and report.exists()
    counts = {"same": 0, "different": 0, "error": 0, "missing": 0}
    with report.open("a" if append else "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not append:
            writer.writeheader()
        for index, item in enumerate(candidates, start=1):
            row = {**item, "status": "", "same": "", "local_chars": "", "mineru_chars": "",
                   "local_sha256": "", "mineru_sha256": "", "response_fields": "", "error": ""}
            pdf_path = Path(item["local_source_path"])
            extracted_path = Path(item["local_extracted_md_path"])
            try:
                if not pdf_path.is_file() or not extracted_path.is_file():
                    raise FileNotFoundError("原PDF或extracted.md不存在")
                local = extracted_path.read_text(encoding="utf-8", errors="replace")
                markdown, response_fields = mineru_markdown(url, pdf_path, timeout)
                same = local.strip() == markdown.strip()
                status = "same" if same else "different"
                row.update({
                    "status": status, "same": str(same).lower(), "local_chars": len(local),
                    "mineru_chars": len(markdown), "local_sha256": sha256_text(local),
                    "mineru_sha256": sha256_text(markdown), "response_fields": response_fields,
                })
                counts[status] += 1
            except FileNotFoundError as exc:
                row.update({"status": "missing", "error": str(exc)})
                counts["missing"] += 1
            except Exception as exc:
                row.update({"status": "error", "error": str(exc)[:500]})
                counts["error"] += 1
            writer.writerow(row)
            handle.flush()
            print(f"[{index}/{len(candidates)}] {row['status']:9} {item['source_id']} {item['filename']}")
            if args.delay > 0 and index < len(candidates):
                time.sleep(args.delay)

    print("\n汇总：", json.dumps(counts, ensure_ascii=False))
    print("报告：", report)


if __name__ == "__main__":
    main()
