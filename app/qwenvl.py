from __future__ import annotations

import base64
import re
from pathlib import Path

import pymupdf as fitz
import httpx


DEFAULT_SHORT_PROMPT = """你会一次看到完整 PDF 的全部页面。阅读所有页面并直接生成最终 Markdown。
按页面顺序理解全文，保留标题层级、日期、编号、人员、金额、术语、决议、待办和表格。
不得遗漏或猜测。无法辨认的内容标记为 [无法辨认]。只输出 Markdown。"""

DEFAULT_LONG_PROMPT = """你正在处理长 PDF 的第 {{page_start}} 至 {{page_end}} 页。
只输出当前批次可直接合并的 Markdown 片段，不重复生成整份文档的总结、封面或目录。
保留标题层级、编号、日期、人员、金额、术语和表格，并衔接上下文提示。
无法辨认的内容标记为 [无法辨认]。只输出 Markdown。"""


class VisionParseError(RuntimeError):
    def __init__(self, message: str, metadata: dict[str, object]):
        super().__init__(message)
        self.metadata = metadata


class QwenVLClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 180,
        short_prompt: str = DEFAULT_SHORT_PROMPT,
        long_prompt: str = DEFAULT_LONG_PROMPT,
        page_threshold: int = 10,
        batch_pages: int = 3,
        dpi: int = 200,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.short_prompt = short_prompt.strip() or DEFAULT_SHORT_PROMPT
        self.long_prompt = long_prompt.strip() or DEFAULT_LONG_PROMPT
        self.page_threshold = max(1, page_threshold)
        self.batch_pages = max(1, batch_pages)
        self.dpi = min(max(dpi, 72), 400)

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def parse(self, pdf_path: Path, artifact_dir: Path) -> tuple[str, dict[str, object]]:
        if not self.configured:
            raise RuntimeError("Qwen-VL 配置不完整：请填写 Base URL、API Key 和模型名称")
        rendered_dir = artifact_dir / "rendered"
        batches_dir = artifact_dir / "vision-batches"
        rendered_dir.mkdir(parents=True, exist_ok=True)
        batches_dir.mkdir(parents=True, exist_ok=True)
        pages = self._render(pdf_path, rendered_dir)
        if not pages:
            raise RuntimeError("PDF 没有可渲染页面")

        short_document = len(pages) <= self.page_threshold
        ranges = [(0, len(pages))] if short_document else [
            (start, min(start + self.batch_pages, len(pages)))
            for start in range(0, len(pages), self.batch_pages)
        ]
        parts: list[str] = []
        batch_files: list[str] = []
        for batch_no, (start, end) in enumerate(ranges, start=1):
            prompt = self.short_prompt if short_document else self._long_prompt(start + 1, end, len(pages))
            if not short_document and parts:
                prompt += "\n\n上一批输出末尾（仅作标题、术语和表格衔接参考，不要重复输出）：\n" + parts[-1][-2000:]
            try:
                markdown = self._complete(prompt, pages[start:end])
                if not markdown.strip():
                    raise RuntimeError("模型返回空内容")
            except Exception as exc:
                metadata = {
                    "parser_engine": "qwenvl", "page_count": len(pages),
                    "vision_batch_count": len(ranges), "vision_failed_batches": [batch_no],
                    "vision_failed_pages": [start + 1, end], "vision_model": self.model,
                }
                raise VisionParseError(f"Qwen-VL 第 {batch_no} 批（第 {start + 1}-{end} 页）失败：{exc}", metadata) from exc
            batch_path = batches_dir / f"batch-{batch_no:04d}-pages-{start + 1}-{end}.md"
            batch_path.write_text(markdown, encoding="utf-8")
            batch_files.append(str(batch_path.resolve()))
            parts.append(f"<!-- Qwen-VL pages: {start + 1}-{end} -->\n\n{markdown}")
        return "\n\n".join(parts).strip(), {
            "parser_engine": "qwenvl",
            "page_count": len(pages),
            "vision_batch_count": len(ranges),
            "vision_failed_batches": [],
            "vision_batch_files": batch_files,
            "vision_dpi": self.dpi,
            "vision_model": self.model,
        }

    def _render(self, pdf_path: Path, output_dir: Path) -> list[Path]:
        result: list[Path] = []
        scale = self.dpi / 72
        with fitz.open(pdf_path) as document:
            for index, page in enumerate(document, start=1):
                output = output_dir / f"page-{index:04d}.jpg"
                pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                pixmap.save(output, jpg_quality=90)
                result.append(output)
        return result

    def _complete(self, prompt: str, images: list[Path]) -> str:
        content: list[dict[str, object]] = [{"type": "text", "text": prompt}]
        for image in images:
            encoded = base64.b64encode(image.read_bytes()).decode("ascii")
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}})
        endpoint = self.base_url if self.base_url.endswith("/chat/completions") else f"{self.base_url}/chat/completions"
        response = httpx.post(
            endpoint,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={"model": self.model, "messages": [{"role": "user", "content": content}], "temperature": 0},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        try:
            if payload["choices"][0].get("finish_reason") == "length":
                raise RuntimeError("模型输出达到长度上限，请减小每批页数或提高模型服务的输出长度限制")
            text = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("Qwen-VL 响应缺少 choices[0].message.content") from exc
        if isinstance(text, list):
            text = "\n".join(str(item.get("text", "")) for item in text if isinstance(item, dict))
        return self._strip_fence(str(text))

    def _long_prompt(self, start: int, end: int, total: int) -> str:
        return (self.long_prompt
                .replace("{{page_start}}", str(start))
                .replace("{{page_end}}", str(end))
                .replace("{{page_total}}", str(total)))

    @staticmethod
    def _strip_fence(value: str) -> str:
        value = value.strip()
        match = re.fullmatch(r"```(?:markdown|md)?\s*\n?(.*?)\n?```", value, re.DOTALL | re.IGNORECASE)
        return match.group(1).strip() if match else value
