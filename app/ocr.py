from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Protocol


class OcrProvider(Protocol):
    def extract(self, image_path: Path, page: int) -> str | None: ...


class NoopOcrProvider:
    def extract(self, image_path: Path, page: int) -> str | None:
        return None


class CommandOcrProvider:
    """Runs a configured argv list plus image path and page number; stdout is OCR text."""

    def __init__(self, command: list[str], timeout: float = 120):
        if not command:
            raise ValueError("OCR_COMMAND 不能为空")
        self.command = command
        self.timeout = timeout

    def extract(self, image_path: Path, page: int) -> str | None:
        completed = subprocess.run(
            [*self.command, str(image_path), str(page)],
            capture_output=True,
            text=True,
            timeout=self.timeout,
            check=True,
        )
        return completed.stdout.strip() or None

