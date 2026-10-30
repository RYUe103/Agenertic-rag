from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import Iterator

from docx import Document
from docx.document import Document as DocumentObject
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.metadata import pdf_to_markdown
from app.ocr import OcrProvider
from app.mineru import MinerUClient
from app.qwenvl import QwenVLClient


DOCUMENT_TYPES = {
    ".pdf": ("pdf", "application/pdf"),
    ".docx": ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ".doc": ("doc", "application/msword"),
    ".txt": ("txt", "text/plain"),
}


def document_info(path: Path) -> tuple[str, str]:
    try:
        return DOCUMENT_TYPES[path.suffix.casefold()]
    except KeyError:
        raise ValueError(f"不支持的文件格式：{path.suffix or '无扩展名'}") from None


def document_to_markdown(
    path: Path, document_type: str, ocr: OcrProvider | None = None, mineru: MinerUClient | None = None,
    vision: QwenVLClient | None = None, artifact_dir: Path | None = None,
) -> tuple[str, dict[str, object]]:
    if document_type == "pdf":
        if vision is not None:
            return vision.parse(path, artifact_dir or path.parent)
        if mineru is not None:
            try:
                markdown, metadata = mineru.parse(path, artifact_dir or path.parent)
                return markdown, metadata
            except Exception as exc:
                if not mineru.fallback_on_error:
                    raise RuntimeError(f"MinerU 解析失败且已禁止本地回退：{exc}") from exc
                # Keep the local parser/OCR fallback available when MinerU is unavailable.
                fallback_markdown, empty_pages = pdf_to_markdown(path, ocr)
                metadata = {
                    "parser_engine": "local_fallback", "parser_requested": "mineru",
                    "pages_without_text": empty_pages, "mineru_error": str(exc)[:500],
                }
                return fallback_markdown, metadata
        markdown, empty_pages = pdf_to_markdown(path, ocr)
        return markdown, {"parser_engine": "local", "parser_requested": "local", "pages_without_text": empty_pages}
    if document_type == "docx":
        return docx_to_markdown(path), {}
    if document_type == "doc":
        return doc_to_markdown(path), {"converted_for_parsing": "docx"}
    if document_type == "txt":
        return txt_to_markdown(path), {"text_encoding": detect_text_encoding(path)}
    raise ValueError(f"不支持的文档类型：{document_type}")


def detect_text_encoding(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            data.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    return "utf-8"


def txt_to_markdown(path: Path) -> str:
    encoding = detect_text_encoding(path)
    text = path.read_bytes().decode(encoding, errors="replace")
    return "\n".join(
        f"<!-- line: {line_no} -->\n{line}" for line_no, line in enumerate(text.splitlines(), start=1)
    )


def _iter_blocks(document: DocumentObject) -> Iterator[Paragraph | Table]:
    for child in document.element.body.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, document)
        elif isinstance(child, CT_Tbl):
            yield Table(child, document)


def _heading_level(paragraph: Paragraph) -> int | None:
    style = paragraph.style.name if paragraph.style else ""
    match = re.search(r"(?:Heading|标题)\s*([1-6])", style, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _escape_table_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", "<br>").strip()


def docx_to_markdown(path: Path) -> str:
    document = Document(path)
    parts: list[str] = []
    paragraph_no = 0
    table_row_no = 0
    for block in _iter_blocks(document):
        if isinstance(block, Paragraph):
            paragraph_no += 1
            text = block.text.strip()
            if not text:
                continue
            level = _heading_level(block)
            prefix = f"{'#' * level} " if level else ""
            parts.append(f"<!-- paragraph: {paragraph_no} -->\n{prefix}{text}")
            continue

        rows = [[_escape_table_cell(cell.text) for cell in row.cells] for row in block.rows]
        rows = [row for row in rows if any(row)]
        if not rows:
            continue
        width = max(len(row) for row in rows)
        normalized = [row + [""] * (width - len(row)) for row in rows]
        for row_index, row in enumerate(normalized):
            table_row_no += 1
            parts.append(f"<!-- table-row: {table_row_no} -->")
            parts.append("| " + " | ".join(row) + " |")
            if row_index == 0:
                parts.append("| " + " | ".join("---" for _ in range(width)) + " |")

    for label, sections in (("页眉", document.sections), ("页脚", document.sections)):
        values: list[str] = []
        for section in sections:
            container = section.header if label == "页眉" else section.footer
            values.extend(paragraph.text.strip() for paragraph in container.paragraphs if paragraph.text.strip())
        for value in dict.fromkeys(values):
            paragraph_no += 1
            parts.append(f"<!-- paragraph: {paragraph_no} -->\n## {label}\n{value}")
    return "\n\n".join(parts)


def doc_to_markdown(path: Path) -> str:
    with tempfile.TemporaryDirectory(prefix="agentic-rag-doc-") as temp_dir:
        converted = Path(temp_dir) / f"{path.stem}.docx"
        try:
            subprocess.run(
                ["/usr/bin/textutil", "-convert", "docx", "-output", str(converted), str(path)],
                capture_output=True, text=True, timeout=60, check=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", "") or str(exc)
            raise RuntimeError(f"DOC 转换失败，需要 macOS textutil：{detail[:300]}") from exc
        if not converted.exists():
            raise RuntimeError("DOC 转换失败：未生成 DOCX 文件")
        return docx_to_markdown(converted)
