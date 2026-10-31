"""Lossless Markdown blocks and independent chunks; never split on magic markers."""
import re
from html.parser import HTMLParser
from .document_models import Block, Chunk, EvidenceLocator, Page, Section, Table, TableCell
from .chunk_builder import build_chunks


class CellParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cells = []
        self.row = -1
        self.active = None
        self.occupied = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'tr':
            self.row += 1
        if tag in ('td', 'th'):
            self.row = max(self.row, 0)
            col = 0
            while (self.row, col) in self.occupied:
                col += 1
            def span(key):
                try:
                    return min(1000, max(1, int(attrs.get(key, 1))))
                except ValueError:
                    return 1
            self.active = dict(row=self.row, column=col, row_span=span('rowspan'),
                               column_span=span('colspan'), is_header=tag == 'th', content='')
            for r in range(self.row, self.row + self.active['row_span']):
                for c in range(col, col + self.active['column_span']):
                    self.occupied.add((r, c))
        elif tag == 'br' and self.active is not None:
            self.active['content'] += '\n'

    def handle_data(self, data):
        if self.active is not None:
            self.active['content'] += data

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.active is not None:
            self.cells.append(self.active)
            self.active = None


def table_cells(raw, table_id, html=False):
    if html:
        parser = CellParser()
        parser.feed(raw)
        values = parser.cells
    else:
        lines = [s for s in raw.splitlines() if '|' in s and not s.lstrip().startswith('<!--')]
        values = []
        row = 0
        for line in lines:
            if re.fullmatch(r'[\s|:\-]+', line):
                continue
            for col, text in enumerate(re.split(r'(?<!\\)\|', line.strip().strip('|'))):
                values.append(dict(row=row, column=col, content=text.strip().replace('\\|', '|'), is_header=row == 0))
            row += 1
    cells = [TableCell(id=f'{table_id}:cell:{i}', **value) for i, value in enumerate(values)]
    for cell in cells:
        unit = re.fullmatch(r'\s*[+-]?[\d,.]+\s*(kg|g|mg|km|cm|mm|m|万元|元|吨|千克|公斤|%|％)\s*', cell.content)
        if unit:
            cell.unit = unit[1]
    return cells


def build_structure(markdown: str, version_id: str, content_list=None):
    blocks, sections, tables = [], [], []
    pages = {}
    stack = []
    page = None
    # Blank lines delimit ordinary blocks; tables remain atomic even when multiline.
    # Use a line walker so offsets partition the original text without loss.
    lines = markdown.splitlines(keepends=True)
    i = 0
    offset = 0
    while i < len(lines):
        start = offset
        first = lines[i]
        end = i + 1
        kind = 'paragraph'
        if '<table' in first.lower():
            while '</table>' not in ''.join(lines[i:end]).lower() and end < len(lines):
                end += 1
            kind = 'table_html'
        elif '|' in first and i + 1 < len(lines) and re.fullmatch(r'[\s|:\-]+', lines[i + 1]) and '-' in lines[i + 1]:
            end = i + 2
            while end < len(lines) and ('|' in lines[end] or re.match(r'\s*<!-- table-row:', lines[end])):
                end += 1
            kind = 'table_markdown'
        elif re.match(r'^#{1,6}\s', first):
            kind = 'heading'
        elif not first.strip():
            kind = 'whitespace'
        elif first.lstrip().startswith('<!--'):
            kind = 'marker'
        else:
            while end < len(lines) and lines[end].strip() and not re.match(r'\s*(?:#|<!--|<table)', lines[end], re.I) and '|' not in lines[end]:
                end += 1
        content = ''.join(lines[i:end])
        offset += len(content)
        match = re.search(r'<!-- page: (\d+) -->', content)
        if match:
            page = int(match.group(1))
            pages.setdefault(page, Page(id=f'{version_id}:page:{page}', version_id=version_id, number=page))
        if kind == 'heading':
            m = re.match(r'^(#{1,6})\s+(.+)', first)
            level, title = len(m[1]), m[2].strip().rstrip('#').strip()
            while stack and stack[-1].level >= level:
                stack.pop()
            section = Section(id=f'{version_id}:section:{len(sections)}', version_id=version_id,
                              title=title, level=level, parent_id=stack[-1].id if stack else None)
            sections.append(section)
            stack.append(section)
        bid = f'{version_id}:block:{len(blocks)}'
        locator = EvidenceLocator(version_id=version_id, block_ids=[bid], page_numbers=[page] if page else [],
                                  section_ids=[s.id for s in stack], start=start, end=offset)
        block = Block(id=bid, version_id=version_id, ordinal=len(blocks), kind=kind, content=content, locator=locator)
        for key in ('paragraph', 'line', 'table-row'):
            found = re.search(r'<!-- '+key+r': (\d+) -->', content)
            if found:
                block.attributes[key] = int(found[1])
        if '![' in content:
            block.attributes['images'] = re.findall(r'!\[([^]]*)\]\(([^)]+)\)', content)
        if kind.startswith('table_'):
            tid = f'{version_id}:table:{len(tables)}'
            locator.table_ids = [tid]
            tables.append(Table(id=tid, version_id=version_id, block_id=bid, raw=content,
                                format=kind, cells=table_cells(content, tid, kind == 'table_html')))
            explicit_units = [m[1].strip() for cell in tables[-1].cells
                              if (m := re.fullmatch(r'\s*(?:单位|Unit)\s*[:：]\s*([^\n]+)', cell.content, re.I))]
            if len(set(explicit_units)) == 1:
                tables[-1].unit = explicit_units[0]
        blocks.append(block)
        if page:
            pages[page].block_ids.append(bid)
        for section in stack:
            section.block_ids.append(bid)
        i = end
    # Only attach MinerU geometry when exact unique text identifies a source block.
    for item in content_list or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get('text') or item.get('table_body') or '').strip()
        matches = [b for b in blocks if b.content.strip() == text] if text else []
        if len(matches) != 1:
            continue
        block = matches[0]
        block.attributes['mineru'] = item
        idx = item.get('page_idx')
        if isinstance(idx, int) and not isinstance(idx, bool) and idx >= 0:
            number = idx + 1
            if not block.locator.page_numbers or block.locator.page_numbers == [number]:
                block.locator.page_numbers = [number]
                pages.setdefault(number, Page(id=f'{version_id}:page:{number}', version_id=version_id, number=number))
                if block.id not in pages[number].block_ids:
                    pages[number].block_ids.append(block.id)
        # Raw bbox retained above; unit/coordinate convention is not inferred.
        bbox = item.get('bbox')
        if item.get('coordinate_system') and isinstance(bbox, list) and len(bbox) == 4:
            block.locator.bbox = bbox
            block.locator.coordinate_system = item['coordinate_system']
    chunks = [Chunk(**c) for c in build_chunks(markdown,version_id,
        [b.model_dump() for b in blocks],[s.model_dump() for s in sections],[t.model_dump() for t in tables])]
    return {'pages': list(pages.values()), 'sections': sections, 'blocks': blocks, 'chunks': chunks, 'tables': tables}
