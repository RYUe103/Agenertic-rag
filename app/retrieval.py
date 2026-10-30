from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Iterable

from app.db import SourceRepository
from app.schemas import Evidence, Source
from app.utils import normalize_filename, normalize_identifier


PAGE_MARKER = re.compile(r"<!-- page: (\d+) -->")
PARAGRAPH_MARKER = re.compile(r"<!-- paragraph: (\d+) -->")
TEXT_LINE_MARKER = re.compile(r"<!-- line: (\d+) -->")
TABLE_ROW_MARKER = re.compile(r"<!-- table-row: (\d+) -->")
MARKDOWN_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*$")
CHINESE_HEADING = re.compile(r"^([一二三四五六七八九十百]+、\s*.+)$")
NUMBERED_HEADING = re.compile(r"^(\d+(?:\.\d+)*\.?)(?:\s+(.+))?$")


class RetrievalTools:
    def __init__(self, repository: SourceRepository, dify: object, grep_timeout: float = 5.0):
        self.repository = repository
        self.dify = dify
        self.grep_timeout = grep_timeout

    def find_files(
        self,
        query: str,
        limit: int = 20,
        source_ids: list[str] | None = None,
    ) -> list[Source]:
        normalized_query = normalize_filename(query)
        query_identifier = normalize_identifier(query) or ""
        query_identifiers = {
            normalize_identifier(token) or ""
            for token in re.findall(r"[A-Za-z0-9]+(?:[-_/][A-Za-z0-9]+)+", query)
        }
        query_identifiers.discard("")
        exact: list[Source] = []
        candidates = self._sources(source_ids)
        for source in candidates:
            filename_identifier = next(
                (normalize_identifier(token) or "" for token in re.findall(
                    r"[A-Za-z0-9]+(?:[-_/][A-Za-z0-9]+)+", Path(source.filename).stem
                )),
                "",
            )
            standard_identifier = normalize_identifier(source.normalized_standard_no) or ""
            if (
                (normalized_query and normalized_query in source.normalized_filename)
                or (query_identifier and query_identifier in filename_identifier)
                or bool(query_identifiers & {filename_identifier, standard_identifier})
            ):
                exact.append(source)
        if exact:
            return exact[:min(max(limit, 1), 5000)]
        terms = {query.casefold(), normalize_filename(query)}
        normalized_id = normalize_identifier(query)
        if normalized_id:
            terms.add(normalized_id)
        terms.update(token.casefold() for token in re.findall(r"[\w./-]{2,}", query))
        found = self.repository.search([term for term in terms if term], 10000)
        if source_ids is not None:
            allowed = set(source_ids)
            found = [source for source in found if source.source_id in allowed]
        return found[:min(max(limit, 1), 5000)]

    def grep_files(
        self,
        keywords: list[str],
        source_ids: list[str] | None = None,
        ignore_case: bool = True,
        regex: bool = False,
        limit: int = 100,
        offset: int = 0,
        include_document_fields: bool = False,
        match_mode: str = "any",
        search_scope: str = "all",
        keyword_groups: list[list[str]] | None = None,
    ) -> list[Evidence]:
        if not keywords:
            return []
        if keyword_groups:
            candidates = self.keyword_search(" ".join(keywords), limit=100000, source_ids=source_ids,
                search_terms=keywords, match_mode=match_mode, search_scope=search_scope, keyword_groups=keyword_groups)
            if include_document_fields:
                return candidates[offset:offset + limit]
            source_ids = [item.source_id for item in candidates]
            keywords = list(dict.fromkeys(word for group in keyword_groups for word in group))
        sources = self._sources(source_ids)
        paths = [source.local_md_path for source in sources if Path(source.local_md_path).exists()]
        matches = (
            self._rg(keywords, paths, ignore_case, regex)
            if paths and shutil.which("rg")
            else self._python_search(keywords, paths, ignore_case, regex) if paths else []
        )
        matches = self._balance_matches(matches, keywords, ignore_case, regex)
        source_by_path = {str(Path(item.local_md_path).resolve()): item for item in sources}
        evidences: list[Evidence] = []
        line_cache: dict[str, list[str]] = {}
        covered_windows: dict[str, list[tuple[int, int]]] = {}
        for path, line_no, content in matches:
            resolved_path = str(Path(path).resolve())
            source = source_by_path.get(resolved_path)
            if source is None:
                continue
            if resolved_path not in line_cache:
                line_cache[resolved_path] = Path(path).read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
            lines = line_cache[resolved_path]
            start = max(1, line_no - 12)
            end = min(len(lines), line_no + 12)
            windows = covered_windows.setdefault(resolved_path, [])
            if any(existing_start <= line_no <= existing_end for existing_start, existing_end in windows):
                continue
            windows.append((start, end))
            context = "\n".join(lines[start - 1:end]).strip()
            evidences.append(self._evidence(
                source, context or content.strip(), "local_grep", start, end,
                self._page_at(lines, line_no), self._location_at(lines, line_no),
            ))
        if include_document_fields:
            return self._document_candidate_evidences(
                sources, keywords, evidences, ignore_case, limit, offset,
                match_mode=match_mode, search_scope=search_scope,
            )
        return evidences[offset: offset + min(max(limit, 1), 500)]

    def keyword_search(
        self,
        query: str,
        sort_field: str = "",
        direction: str = "desc",
        limit: int = 3,
        source_ids: list[str] | None = None,
        search_terms: list[str] | None = None,
        match_mode: str = "all",
        search_scope: str = "all",
        keyword_groups: list[list[str]] | None = None,
    ) -> list[Evidence]:
        """Find documents by filename/metadata/body and rank the matched files."""
        phrase = str(query or "").strip()
        if not phrase:
            return []
        sources = self._sources(source_ids)
        raw_terms = search_terms if search_terms else [phrase]
        needles = list(dict.fromkeys(
            str(value).strip().casefold() for value in raw_terms if str(value).strip()
        ))
        matches: list[tuple[Source, list[str], str | None]] = []
        for source in sources:
            scopes: list[str] = []
            filename = source.filename.casefold()
            title_values = [str(source.standard_title or "")]
            for key, raw in (source.custom_metadata or {}).items():
                if re.search(r"标题|主题|title|topic", str(key), re.IGNORECASE):
                    value = raw.value if hasattr(raw, "value") else raw.get("value") if isinstance(raw, dict) else raw
                    title_values.append(str(value or ""))
            title = " ".join(title_values).casefold()
            path = Path(source.local_md_path)
            body = path.read_text(encoding="utf-8", errors="replace").casefold() if path.exists() else ""
            values = {"filename": filename, "title": title, "body": body}
            scopes_to_check = ["filename", "title", "body"] if search_scope == "all" else [search_scope]
            if any(scope not in values for scope in scopes_to_check):
                scopes_to_check = ["filename", "title", "body"]
            matches_needles = all if str(match_mode).casefold() == "all" else any
            groups = keyword_groups or [[needle] for needle in needles]
            if not matches_needles(
                any(str(needle).casefold() in values[scope] for needle in group for scope in scopes_to_check)
                for group in groups
            ):
                continue
            matched_needles = [str(word).casefold() for group in groups for word in group]
            needles = matched_needles
            if any(needle in filename for needle in needles) and "filename" in scopes_to_check:
                scopes.append("文件名")
            if any(needle in title for needle in needles) and "title" in scopes_to_check:
                scopes.append("标题/主题")
            metadata_hit = False
            for key, raw in (source.custom_metadata or {}).items():
                value = getattr(raw, "value", raw.get("value") if isinstance(raw, dict) else raw)
                if any(needle in str(value or "").casefold() for needle in needles):
                    metadata_hit = True
                    scopes.append(f"元数据:{key}")
            body_excerpt: str | None = None
            path = Path(source.local_md_path)
            if path.exists():
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    if "body" in scopes_to_check and any(needle in line.casefold() for needle in needles):
                        body_excerpt = line.strip()
                        break
            if body_excerpt is not None:
                scopes.append("正文")
            if scopes:
                matches.append((source, list(dict.fromkeys(scopes)), body_excerpt))

        def unwrap(raw: object) -> object:
            return getattr(raw, "value", raw.get("value") if isinstance(raw, dict) else raw)

        def date_key(source: Source) -> tuple[int, str]:
            raw = unwrap((source.custom_metadata or {}).get(sort_field)) if sort_field else None
            text = str(raw or "").strip()
            match = re.search(r"(\d{4})\D?(\d{1,2})\D?(\d{1,2})", text)
            return (1, f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}") if match else (0, "")

        descending = str(direction or "desc").casefold() != "asc"
        populated = [item for item in matches if date_key(item[0])[0]]
        missing = [item for item in matches if not date_key(item[0])[0]]
        populated.sort(key=lambda item: date_key(item[0])[1], reverse=descending)
        matches = [*populated, *missing]
        results: list[Evidence] = []
        for source, scopes, excerpt in matches[:min(max(limit, 1), 100000)]:
            results.append(self._evidence(
                source,
                f"文件名={source.filename}；命中范围={','.join(scopes)}；"
                f"{sort_field or '排序字段'}={unwrap((source.custom_metadata or {}).get(sort_field)) if sort_field else '未指定'}"
                + (f"；正文命中={excerpt}" if excerpt else ""),
                "keyword_ranked", score=float(len(scopes)),
            ))
        return results

    def _document_candidate_evidences(
        self,
        sources: list[Source],
        keywords: list[str],
        body_hits: list[Evidence],
        ignore_case: bool,
        limit: int,
        offset: int,
        match_mode: str = "any",
        search_scope: str = "all",
    ) -> list[Evidence]:
        normalized_keywords = [word.casefold() if ignore_case else word for word in keywords[:20] if word]
        body_by_source: dict[str, list[Evidence]] = {}
        for item in body_hits:
            body_by_source.setdefault(item.source_id, []).append(item)

        candidates: list[tuple[int, Source, list[str], Evidence | None]] = []
        for source in sources:
            filename = source.filename.casefold() if ignore_case else source.filename
            title_parts = [str(source.standard_title or "")]
            for key, raw in (source.custom_metadata or {}).items():
                if re.search(r"标题|主题|title|topic", str(key), re.IGNORECASE):
                    value = raw.value if hasattr(raw, "value") else raw.get("value") if isinstance(raw, dict) else raw
                    title_parts.append(str(value or ""))
            title = " ".join(title_parts)
            title = title.casefold() if ignore_case else title
            path = Path(source.local_md_path)
            body = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
            body = body.casefold() if ignore_case else body
            values = {"filename": filename, "title": title, "body": body}
            scopes_to_check = ["filename", "title", "body"] if search_scope == "all" else [search_scope]
            if any(scope not in values for scope in scopes_to_check):
                scopes_to_check = ["filename", "title", "body"]
            if str(match_mode).casefold() == "all" and not all(
                any(keyword in values[scope] for scope in scopes_to_check)
                for keyword in normalized_keywords
            ):
                continue
            scopes: list[str] = []
            if "filename" in scopes_to_check and any(keyword in filename for keyword in normalized_keywords):
                scopes.append("文件名")
            if "title" in scopes_to_check and any(keyword in title for keyword in normalized_keywords):
                title_keys = [str(key) for key in (source.custom_metadata or {}) if re.search(r"标题|主题|title|topic", str(key), re.IGNORECASE)]
                scopes.append("元数据:" + ",".join(title_keys) if title_keys else "标题/主题")

            first_body_hit = (body_by_source.get(source.source_id) or [None])[0]
            if "body" in scopes_to_check and first_body_hit:
                scopes.append("正文")
            if scopes:
                candidates.append((len(scopes), source, scopes, first_body_hit))

        candidates.sort(key=lambda item: (-item[0], item[1].filename.casefold()))
        results: list[Evidence] = []
        for score, source, scopes, body_hit in candidates:
            excerpt = f"；正文命中={body_hit.content.strip()}" if body_hit else ""
            results.append(self._evidence(
                source,
                f"文件名={source.filename}；命中范围={','.join(scopes)}{excerpt}",
                "document_candidate",
                line_start=body_hit.line_start if body_hit else None,
                line_end=body_hit.line_end if body_hit else None,
                page=body_hit.page if body_hit else None,
                location=body_hit.location if body_hit else None,
                score=float(score),
            ))
        bounded = min(max(limit, 1), 100000)
        return results[offset: offset + bounded]

    @staticmethod
    def _balance_matches(matches: list[tuple[str, int, str]], keywords: list[str], ignore_case: bool, regex: bool) -> list[tuple[str, int, str]]:
        flags = re.IGNORECASE if ignore_case else 0
        patterns = [re.compile(keyword if regex else re.escape(keyword), flags) for keyword in keywords[:20]]
        buckets: list[list[tuple[str, int, str]]] = [[] for _ in patterns]
        for match in matches:
            for index, pattern in enumerate(patterns):
                if pattern.search(match[2]):
                    buckets[index].append(match)
        for index, bucket in enumerate(buckets):
            keyword = keywords[index]
            if keyword.isascii() and keyword.replace("-", "").isalnum():
                exact_word = re.compile(rf"\b{re.escape(keyword)}\b", flags)
                expected = keyword.casefold() if ignore_case else keyword
                bucket.sort(key=lambda item: (
                    0 if ((item[2].strip().casefold() if ignore_case else item[2].strip()) == expected) else
                    1 if exact_word.search(item[2]) else 2,
                    item[1],
                ))
        balanced: list[tuple[str, int, str]] = []
        seen: set[tuple[str, int]] = set()
        depth = 0
        while any(depth < len(bucket) for bucket in buckets):
            for bucket in buckets:
                if depth < len(bucket):
                    match = bucket[depth]
                    key = (match[0], match[1])
                    if key not in seen:
                        seen.add(key)
                        balanced.append(match)
            depth += 1
        return balanced

    def _rg(self, keywords: list[str], paths: list[str], ignore_case: bool, regex: bool) -> list[tuple[str, int, str]]:
        command = ["rg", "--json", "--line-number", "--color", "never"]
        if ignore_case:
            command.append("--ignore-case")
        if not regex:
            command.append("--fixed-strings")
        for keyword in keywords[:20]:
            command.extend(["-e", keyword])
        command.extend(paths)
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=self.grep_timeout, check=False)
        except (subprocess.TimeoutExpired, OSError):
            return self._python_search(keywords, paths, ignore_case, regex)
        matches: list[tuple[str, int, str]] = []
        for raw in completed.stdout.splitlines():
            record = json.loads(raw)
            if record.get("type") != "match":
                continue
            data = record["data"]
            matches.append((data["path"]["text"], int(data["line_number"]), data["lines"]["text"]))
        return matches

    def _python_search(self, keywords: list[str], paths: list[str], ignore_case: bool, regex: bool) -> list[tuple[str, int, str]]:
        flags = re.IGNORECASE if ignore_case else 0
        patterns = [re.compile(word if regex else re.escape(word), flags) for word in keywords[:20]]
        matches: list[tuple[str, int, str]] = []
        for path in paths:
            for line_no, line in enumerate(Path(path).read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
                if any(pattern.search(line) for pattern in patterns):
                    matches.append((path, line_no, line))
        return matches

    def read_file_context(
        self, source_id: str, page: int | None = None, line: int | None = None, before: int = 12, after: int = 12, max_chars: int = 6000
    ) -> Evidence:
        source = self.repository.get(source_id)
        lines = Path(source.local_md_path).read_text(encoding="utf-8", errors="replace").splitlines()
        if page is not None:
            starts = [(index, int(match.group(1))) for index, value in enumerate(lines) if (match := PAGE_MARKER.search(value))]
            selected = next((index for index, value in starts if value == page), None)
            if selected is None:
                raise ValueError(f"页码 {page} 不存在")
            next_start = next((index for index, value in starts if index > selected), len(lines))
            start, end = selected, next_start
        else:
            center = max(1, line or 1) - 1
            start, end = max(0, center - before), min(len(lines), center + after + 1)
        content = "\n".join(lines[start:end])[:max_chars]
        return self._evidence(
            source, content, "local_context", start + 1, min(end, len(lines)),
            page or self._page_at(lines, start + 1), self._location_at(lines, start + 1),
        )

    def read_full_document(self, source_id: str, max_chars: int = 30000) -> Evidence:
        """Read the complete local Markdown for an already identified source."""
        source = self.repository.get(source_id)
        path = Path(source.local_md_path)
        if not path.exists():
            raise FileNotFoundError(f"本地 Markdown 不存在：{path}")
        bounded = min(max(int(max_chars), 1000), 100000)
        content = path.read_text(encoding="utf-8", errors="replace")
        total_chars = len(content)
        if total_chars > bounded:
            content = content[:bounded].rstrip() + f"\n\n[文档内容已截断，原文共 {total_chars} 个字符]"
        line_count = max(1, len(content.splitlines()))
        return self._evidence(source, content, "local_full_document", 1, line_count, None, "全文")

    def search_within_document(self, source_id: str, query: str, limit: int = 30) -> list[Evidence]:
        keywords = self._query_terms(query)
        return self.grep_files(keywords, [source_id], True, False, min(max(limit, 1), 100))

    def metadata_search(
        self,
        query: str = "",
        year: int | None = None,
        status: str | None = None,
        publish_from: str | None = None,
        publish_to: str | None = None,
        effective_from: str | None = None,
        effective_to: str | None = None,
        custom_filters: dict[str, object] | None = None,
        custom_date_ranges: dict[str, object] | None = None,
        custom_sort: dict[str, object] | None = None,
        limit: int = 20,
        source_ids: list[str] | None = None,
    ) -> list[Source]:
        candidates = self._sources(source_ids)
        if query.strip():
            matching_ids = {item.source_id for item in self.find_files(query, 10000)}
            candidates = [item for item in candidates if item.source_id in matching_ids]
        expected_status = status.casefold().strip() if status else None
        filters = custom_filters if isinstance(custom_filters, dict) else {}
        date_ranges = custom_date_ranges if isinstance(custom_date_ranges, dict) else {}

        def unwrap(value: object) -> object:
            if hasattr(value, "value"):
                return getattr(value, "value")
            if isinstance(value, dict) and "value" in value:
                return value.get("value")
            return value

        def equal_value(actual: object, expected: object) -> bool:
            actual = unwrap(actual)
            expected = unwrap(expected)
            if actual is None or expected is None:
                return False
            if isinstance(actual, list):
                return any(equal_value(item, expected) for item in actual)
            if isinstance(expected, list):
                return any(equal_value(actual, item) for item in expected)
            if isinstance(actual, bool) or isinstance(expected, bool):
                return str(actual).strip().casefold() == str(expected).strip().casefold()
            if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
                return actual == expected
            actual_text = str(actual).strip().casefold()
            expected_text = str(expected).strip().casefold()
            return actual_text == expected_text or expected_text in actual_text

        def normalized_date(value: object) -> str | None:
            text = str(unwrap(value) or "").strip()
            match = re.search(r"(\d{4})(?:\D+(\d{1,2}))?(?:\D+(\d{1,2}))?", text)
            if not match:
                return None
            year_value = int(match.group(1))
            month_value = int(match.group(2) or 1)
            day_value = int(match.group(3) or 1)
            if not (1 <= month_value <= 12 and 1 <= day_value <= 31):
                return None
            return f"{year_value:04d}-{month_value:02d}-{day_value:02d}"

        def matches(source: Source) -> bool:
            if not (
                (year is None or source.standard_year == year)
                and (expected_status is None or (source.status or "").casefold() == expected_status)
                and (publish_from is None or bool(source.publish_date and source.publish_date >= publish_from))
                and (publish_to is None or bool(source.publish_date and source.publish_date <= publish_to))
                and (effective_from is None or bool(source.effective_date and source.effective_date >= effective_from))
                and (effective_to is None or bool(source.effective_date and source.effective_date <= effective_to))
            ):
                return False
            for key, expected in filters.items():
                if not isinstance(key, str) or not key.strip():
                    continue
                metadata = source.custom_metadata or {}
                if key not in metadata or not equal_value(metadata.get(key), expected):
                    return False
            for key, raw_range in date_ranges.items():
                if not isinstance(key, str) or not isinstance(raw_range, dict):
                    continue
                actual = normalized_date((source.custom_metadata or {}).get(key))
                lower = normalized_date(raw_range.get("from"))
                upper = normalized_date(raw_range.get("to"))
                if actual is None or (lower and actual < lower) or (upper and actual > upper):
                    return False
            return True

        results = [source for source in candidates if matches(source)]
        sort = custom_sort if isinstance(custom_sort, dict) else {}
        sort_field = str(sort.get("field") or "").strip()
        if sort_field:
            descending = str(sort.get("direction") or "desc").casefold() != "asc"

            def sortable_value(value: object) -> tuple[int, object]:
                if isinstance(value, bool):
                    return (0, int(value))
                if isinstance(value, (int, float)):
                    return (1, float(value))
                text = str(value).strip()
                date_match = re.fullmatch(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日)?", text)
                if date_match:
                    year, month, day = (int(part) for part in date_match.groups())
                    return (3, f"{year:04d}-{month:02d}-{day:02d}")
                return (2, text.casefold())

            def sort_value(source: Source) -> object | None:
                raw = (source.custom_metadata or {}).get(sort_field)
                value = unwrap(raw)
                if value is None or value == "":
                    return None
                return value

            populated = [source for source in results if sort_value(source) is not None]
            missing = [source for source in results if sort_value(source) is None]
            populated.sort(key=lambda source: sortable_value(sort_value(source)), reverse=descending)
            results = [*populated, *missing]
        return results[:min(max(limit, 1), 5000)]

    def read_pages(self, source_id: str, pages: list[int], max_chars_per_page: int = 6000) -> list[Evidence]:
        source = self.repository.get(source_id)
        if source.document_type != "pdf":
            raise ValueError("read_pages 只适用于 PDF；Word/TXT 请使用文档内检索或上下文读取")
        unique_pages = list(dict.fromkeys(page for page in pages if page > 0))[:20]
        return [self.read_file_context(source_id, page=page, max_chars=max_chars_per_page) for page in unique_pages]

    def get_adjacent_chunks(
        self, source_id: str, segment_id: str, before: int = 1, after: int = 1
    ) -> list[Evidence]:
        source = self.repository.get(source_id)
        if not source.dify_document_id or not segment_id:
            return []
        segments: list[dict[str, object]] = []
        page_size = 100
        for page in range(1, 101):
            batch = self.dify.get_segments(source.dify_document_id, "", page, page_size)
            segments.extend(batch)
            target = next((index for index, item in enumerate(segments) if str(item.get("id")) == segment_id), None)
            has_requested_after = target is not None and target + min(max(after, 0), 5) < len(segments)
            if len(batch) < page_size or has_requested_after:
                break
        target = next((index for index, item in enumerate(segments) if str(item.get("id")) == segment_id), None)
        if target is None:
            return []
        start = max(0, target - min(max(before, 0), 5))
        end = min(len(segments), target + min(max(after, 0), 5) + 1)
        return [
            self._evidence(
                source,
                str(segment.get("content") or ""),
                "vector_adjacent",
                segment_id=str(segment.get("id") or "") or None,
            )
            for segment in segments[start:end]
            if segment.get("content")
        ]

    def get_document_outline(self, source_id: str, max_items: int = 100) -> list[Evidence]:
        source = self.repository.get(source_id)
        lines = Path(source.local_md_path).read_text(encoding="utf-8", errors="replace").splitlines()
        items: list[tuple[str, int | None, int]] = []
        seen_titles: set[str] = set()
        page: int | None = None
        in_contents = False
        index = 0
        item_limit = min(max(max_items, 1), 300)
        while index < len(lines):
            raw = lines[index]
            marker = PAGE_MARKER.search(raw)
            if marker:
                page = int(marker.group(1))
                in_contents = False
                index += 1
                continue
            value = raw.strip()
            if not value:
                index += 1
                continue
            if value.casefold() in {"table of contents", "contents", "目录"}:
                in_contents = True
                index += 1
                continue
            title: str | None = None
            item_page = page
            consumed = 1
            if in_contents:
                numbered = NUMBERED_HEADING.match(value)
                following = lines[index + 1].strip() if index + 1 < len(lines) else ""
                after_following = lines[index + 2].strip() if index + 2 < len(lines) else ""
                if numbered and not numbered.group(2) and following and not following.isdigit() and after_following.isdigit():
                    title = f"{numbered.group(1)} {following}"
                    item_page = int(after_following)
                    consumed = 3
                elif following.isdigit():
                    title = value
                    item_page = int(following)
                    consumed = 2
            else:
                heading = MARKDOWN_HEADING.match(value) or CHINESE_HEADING.match(value)
                title = heading.group(1).strip() if heading else None
                numbered = NUMBERED_HEADING.match(value)
                if title is None and numbered and numbered.group(2):
                    title = value
            if title and len(title) <= 180:
                normalized_title = re.sub(r"\s+", " ", title).casefold()
                if normalized_title not in seen_titles:
                    seen_titles.add(normalized_title)
                    items.append((title, item_page, index + 1))
            index += consumed
            if len(items) >= item_limit:
                break
        return [
            self._evidence(
                source,
                "\n".join(f"- {title}（PDF 第 {item_page} 页）" if item_page else f"- {title}" for title, item_page, _ in items),
                "document_outline",
                line_start=items[0][2] if items else None,
                line_end=items[-1][2] if items else None,
            )
        ] if items else []

    def compare_document_metadata(self, source_ids: list[str]) -> list[Evidence]:
        return [
            self._evidence(source, self._metadata_text(source), "metadata_comparison")
            for source in self._sources(list(dict.fromkeys(source_ids))[:20])
        ]

    def vector_search(
        self,
        query: str,
        top_k: int = 10,
        metadata_filtering_conditions: dict[str, object] | None = None,
        source_ids: list[str] | None = None,
    ) -> list[Evidence]:
        evidences: list[Evidence] = []
        requested_top_k = min(max(top_k, 1), 50)
        retrieval_top_k = 50 if source_ids is not None else requested_top_k
        try:
            hits = self.dify.retrieve(query, retrieval_top_k, metadata_filtering_conditions)
        except TypeError:
            hits = self.dify.retrieve(query, retrieval_top_k)
        allowed = set(source_ids) if source_ids is not None else None
        for hit in hits:
            source = self.repository.by_dify_document(hit.document_id) if hit.document_id else None
            if source is None and hit.document_name:
                candidates = self.find_files(hit.document_name, 1)
                source = candidates[0] if candidates else None
            if source and (allowed is None or source.source_id in allowed):
                evidences.append(self._evidence(
                    source, hit.content, "vector", score=hit.score, segment_id=hit.segment_id
                ))
        return evidences[:requested_top_k]

    def dify_metadata_search(
        self,
        query: str,
        conditions: dict[str, object],
        top_k: int = 10,
        source_ids: list[str] | None = None,
    ) -> list[Evidence]:
        """Semantic retrieval constrained by Dify's document metadata filter."""
        evidences: list[Evidence] = []
        try:
            hits = self.dify.retrieve(query, min(max(top_k, 1), 50), conditions)
        except TypeError:
            hits = []
        allowed = set(source_ids) if source_ids is not None else None
        for hit in hits:
            source = self.repository.by_dify_document(hit.document_id) if hit.document_id else None
            if source is None and hit.document_name:
                candidates = self.find_files(hit.document_name, 1)
                source = candidates[0] if candidates else None
            if source and (allowed is None or source.source_id in allowed):
                evidences.append(self._evidence(source, hit.content, "vector_metadata", score=hit.score, segment_id=hit.segment_id))
        return evidences

    def get_source(self, source_id: str) -> Source:
        return self.repository.get(source_id)

    def list_latest_standards(self, topic: str = "", category: str = "", exclude_abolished: bool = True, limit: int = 20) -> list[Source]:
        candidates = self.find_files(" ".join(filter(None, [topic, category])), 100) if topic or category else self.repository.list(100)
        if exclude_abolished:
            candidates = [item for item in candidates if item.status != "abolished"]
        candidates.sort(key=lambda item: (item.effective_date or "", item.publish_date or "", item.standard_year or 0), reverse=True)
        return candidates[:limit]

    def _sources(self, source_ids: list[str] | None) -> list[Source]:
        if source_ids is not None:
            return [self.repository.get(source_id) for source_id in source_ids[:5000]]
        return self.repository.list(10000)

    @staticmethod
    def _query_terms(query: str) -> list[str]:
        quoted = re.findall(r'["“”]([^"“”]+)["“”]', query)
        tokens = re.findall(r"[\u4e00-\u9fff]{2,12}|[A-Za-z0-9][A-Za-z0-9_./%-]*", query)
        return list(dict.fromkeys([*quoted, *tokens]))[:20] or [query]

    @staticmethod
    def _metadata_text(source: Source) -> str:
        return (
            f"文件名={source.filename}；标准号={source.standard_no or '未知'}；标题={source.standard_title or '未知'}；"
            f"年份={source.standard_year or '未知'}；发布日期={source.publish_date or '未知'}；"
            f"实施日期={source.effective_date or '未知'}；状态={source.status or '未知'}；"
            f"代替={source.replaces or '无记录'}；被替代={source.replaced_by or '无记录'}；"
            f"解析状态={source.parse_status}；Dify状态={source.dify_status}"
        )

    @staticmethod
    def _page_at(lines: list[str], line_no: int) -> int | None:
        for index in range(min(line_no - 1, len(lines) - 1), -1, -1):
            match = PAGE_MARKER.search(lines[index])
            if match:
                return int(match.group(1))
        return None

    @staticmethod
    def _location_at(lines: list[str], line_no: int) -> str | None:
        markers = (
            (PARAGRAPH_MARKER, "第 {value} 段"),
            (TEXT_LINE_MARKER, "第 {value} 行"),
            (TABLE_ROW_MARKER, "表格第 {value} 行"),
        )
        for index in range(min(line_no - 1, len(lines) - 1), -1, -1):
            for pattern, label in markers:
                if match := pattern.search(lines[index]):
                    return label.format(value=int(match.group(1)))
            if PAGE_MARKER.search(lines[index]):
                return None
        return None

    @staticmethod
    def _evidence(
        source: Source, content: str, retrieval_type: str, line_start: int | None = None,
        line_end: int | None = None, page: int | None = None, location: str | None = None,
        score: float | None = None,
        segment_id: str | None = None,
    ) -> Evidence:
        return Evidence(
            evidence_id=f"ev_{uuid.uuid4().hex[:10]}", source_id=source.source_id, filename=source.filename,
            standard_no=source.standard_no, page=page, line_start=line_start, line_end=line_end, location=location,
            content=content, retrieval_type=retrieval_type, score=score, segment_id=segment_id,
            pdf_url=source.pdf_url,
        )
