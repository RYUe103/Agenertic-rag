from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pymupdf
import pytest
from docx import Document

from app.clients.dify import MockDifyClient
from app.clients.pdf_platform import MockPdfPlatformClient
from app.db import SourceRepository
from app.ingestion import IngestionService


@pytest.fixture
def sample_pdf(tmp_path: Path) -> Path:
    path = tmp_path / "GB-T 1234-2024 管道要求.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "GB/T 1234-2024\nPipeline standard\nTemperature: -20 C to 50 C\nPublish: 2024-01-02")
    document.new_page()
    document.save(path)
    document.close()
    return path


@pytest.fixture
def sample_docx(tmp_path: Path) -> Path:
    path = tmp_path / "活动预算方案.docx"
    document = Document()
    document.add_heading("活动预算方案", level=1)
    document.add_paragraph("本次活动预算总额为 12000 元。")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "项目"
    table.rows[0].cells[1].text = "金额"
    row = table.add_row().cells
    row[0].text = "场地"
    row[1].text = "5000 元"
    document.save(path)
    return path


@pytest.fixture
def sample_doc(sample_docx: Path, tmp_path: Path) -> Path:
    if not shutil.which("textutil"):
        pytest.skip("DOC 解析测试需要 macOS textutil")
    path = tmp_path / "旧版活动方案.doc"
    subprocess.run(
        ["textutil", "-convert", "doc", "-output", str(path), str(sample_docx)],
        capture_output=True, text=True, check=True,
    )
    return path


@pytest.fixture
def sample_txt(tmp_path: Path) -> Path:
    path = tmp_path / "预算说明.txt"
    path.write_bytes("预算说明\n交通费用：800 元\n总计：800 元".encode("gb18030"))
    return path


@pytest.fixture
def repository(tmp_path: Path) -> SourceRepository:
    return SourceRepository(tmp_path / "data" / "sources.db")


@pytest.fixture
def ingestion(repository: SourceRepository, tmp_path: Path) -> IngestionService:
    return IngestionService(repository, tmp_path / "data" / "files", MockDifyClient(), MockPdfPlatformClient())
