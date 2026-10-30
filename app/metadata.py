from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path

import pymupdf

from app.ocr import OcrProvider
from app.utils import normalize_identifier


STANDARD_NO = re.compile(
    r"\b((?:GB(?:/T)?|GBZ|GJB|HB|JB(?:/T)?|NB(?:/T)?|DL(?:/T)?|SY(?:/T)?|ISO|IEC|EN|ASTM|JU|NIST)(?![A-Z])\s*[A-Z]*\s*[-/]?\s*\d+(?:\s*[./—–-]\s*\d+)*)\b",
    re.IGNORECASE,
)
DATE_PATTERNS = {
    "publish_date": re.compile(r"(?:发布|发布日期)\s*[：:]?\s*(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})日?"),
    "effective_date": re.compile(r"(?:实施|实施日期)\s*[：:]?\s*(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})日?"),
}


@dataclass
class ExtractedMetadata:
    standard_no: str | None = None
    normalized_standard_no: str | None = None
    standard_title: str | None = None
    standard_year: int | None = None
    publish_date: str | None = None
    effective_date: str | None = None
    status: str | None = None
    replaces: str | None = None
    replaced_by: str | None = None

    def as_dict(self) -> dict[str, str | int | None]:
        return asdict(self)


def _iso_date(match: re.Match[str]) -> str:
    return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"


def extract_metadata(filename: str, text: str) -> ExtractedMetadata:
    sample = f"{Path(filename).stem}\n{text[:20000]}"
    match = STANDARD_NO.search(sample)
    standard_no = re.sub(r"\s+", " ", match.group(1)).strip() if match else None
    normalized = normalize_identifier(standard_no)
    year_match = re.search(r"(?:[-—–]|\s)(19\d{2}|20\d{2})\b", standard_no or "")
    year = int(year_match.group(1)) if year_match else None
    dates: dict[str, str | None] = {}
    for field, pattern in DATE_PATTERNS.items():
        date_match = pattern.search(sample)
        dates[field] = _iso_date(date_match) if date_match else None
    replaces_match = re.search(r"(?:代替|替代)\s*[：:]?\s*([^\n，。;；]{3,80})", sample)
    replaced_by_match = re.search(r"(?:被|由)\s*([^\n，。;；]{3,80})\s*(?:代替|替代)", sample)
    status = "abolished" if re.search(r"废止|作废", sample[:5000]) else "active" if re.search(r"现行", sample[:5000]) else "unknown"
    title = None
    lines = [line.strip() for line in sample.splitlines() if line.strip()]
    for line in lines[:80]:
        if 4 <= len(line) <= 100 and line != standard_no and not DATE_PATTERNS["publish_date"].search(line):
            if re.search(r"标准|规范|规程|要求|导则|技术条件|说明书", line):
                title = line
                break
    return ExtractedMetadata(
        standard_no=standard_no,
        normalized_standard_no=normalized,
        standard_title=title,
        standard_year=year,
        publish_date=dates["publish_date"],
        effective_date=dates["effective_date"],
        status=status,
        replaces=replaces_match.group(1).strip() if replaces_match else None,
        replaced_by=replaced_by_match.group(1).strip() if replaced_by_match else None,
    )


def pdf_to_markdown(pdf_path: Path, ocr: OcrProvider | None = None) -> tuple[str, list[int]]:
    pages: list[str] = []
    empty_pages: list[int] = []
    with pymupdf.open(pdf_path) as document:
        for page_no, page in enumerate(document, start=1):
            text = page.get_text("text").strip()
            if not text:
                if ocr:
                    with tempfile.TemporaryDirectory(prefix="agentic-rag-ocr-") as temp_dir:
                        image_path = Path(temp_dir) / f"page-{page_no}.png"
                        page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False).save(image_path)
                        text = ocr.extract(image_path, page_no) or ""
                if not text:
                    empty_pages.append(page_no)
                    text = "[本页未提取到文本；可能为扫描页，尚未配置 OCR 或 OCR 未识别到文字]"
            pages.append(f"<!-- page: {page_no} -->\n\n{text}\n")
    return "\n".join(pages), empty_pages
