"""Immutable retrieval builds over existing source structure (no reparsing)."""
import hashlib
import json
import re
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ChunkConfig:
    size: int = 1800
    overlap: int = 180
    algorithm: str = 'structure-v2'

    def __post_init__(self):
        if not 200 <= self.size <= 16000 or not 0 <= self.overlap <= self.size // 3:
            raise ValueError('size must be 200..16000; overlap must be <= size/3')
        if self.algorithm != 'structure-v2':
            raise ValueError('Unsupported builder algorithm')

    @property
    def version(self):
        digest = hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:12]
        return f'{self.algorithm}-{digest}'


def build_chunks(markdown, version_id, blocks, sections, tables, config=ChunkConfig()):
    """Soft size bound: indivisible sentences and table rows may exceed target."""
    output, pending = [], []
    previous_text = ''
    section_map = {s['id']: s['title'] for s in sections}
    table_map = {t['block_id']: t for t in tables}

    def emit(start, end, linked, table=None, cells=None, content=None):
        def values(key):
            return list(dict.fromkeys(x for b in linked for x in b['locator'].get(key, [])))
        locator = dict(version_id=version_id, start=start, end=end,
            block_ids=[b['id'] for b in linked], page_numbers=values('page_numbers'),
            section_ids=values('section_ids'), table_ids=values('table_ids'),
            bbox=None, coordinate_system=None)
        ordinal = len(output)
        output.append(dict(id=f'{version_id}:build:{config.version}:chunk:{ordinal}',
            version_id=version_id, builder_version=config.version, ordinal=ordinal,
            content=markdown[start:end] if content is None else content, locator=locator,
            section_titles=[section_map[s] for s in locator['section_ids'] if s in section_map],
            table_id=table['id'] if table else None,
            cell_ids=[c['id'] for c in cells] if cells else [],
            table_rows=sorted({c['row'] for c in cells}) if cells else [],
            table_caption=previous_text if table and len(previous_text)<240 and re.match(r'^(?:表\s*\d|Table\s+\d)',previous_text,re.I) else None,
            content_is_projection=content is not None))

    def flush():
        if pending:
            emit(pending[0]['locator']['start'], pending[-1]['locator']['end'], pending[:])
            pending.clear()

    for block in blocks:
        start, end = block['locator']['start'], block['locator']['end']
        table = table_map.get(block['id'])
        if table:
            flush()
            if len(table['raw']) <= config.size:
                emit(start, end, [block], table, table['cells'])
            else:
                # JSON cells preserve coordinates, exact strings, spans and repeated headers.
                headers = [c for c in table['cells'] if c['is_header']]
                rows = {}
                for cell in table['cells']:
                    if not cell['is_header']:
                        rows.setdefault(cell['row'], []).append(cell)
                group = []
                def table_emit(cells):
                    first_row=min((c['row'] for c in cells),default=0)
                    spanning=[c for c in table['cells'] if not c['is_header'] and
                        c['row']<first_row<c['row']+c['row_span'] and c not in cells]
                    context = dict(table_id=table['id'], title=[section_map[s] for s in block['locator']['section_ids']],
                                   caption=previous_text if len(previous_text)<240 else None,
                                   unit=table.get('unit'), cells=headers+spanning+cells)
                    emit(start, end, [block], table, headers+spanning+cells, json.dumps(context, ensure_ascii=False))
                for cells in rows.values():
                    if group and len(json.dumps(group+cells, ensure_ascii=False)) > config.size:
                        table_emit(group)
                        group = []
                    group.extend(cells)
                table_emit(group)
            continue
        if pending and (block['kind'] == 'heading' or
                end-pending[0]['locator']['start'] > config.size and pending[-1]['kind'] != 'heading'):
            flush()
        if end-start > config.size:
            flush()
            # Never cut identifiers or sentences at arbitrary character boundaries.
            boundaries = [start] + [start+m.end() for m in re.finditer(r'[。！？!?](?:\s*)|[.;](?=\s)\s+|\n', block['content'])]
            if boundaries[-1] != end:
                boundaries.append(end)
            left = 0
            while left < len(boundaries)-1:
                right = left+1
                while right+1 < len(boundaries) and boundaries[right+1]-boundaries[left] <= config.size:
                    right += 1
                emit(boundaries[left], boundaries[right], [block])
                new_left = right
                while new_left-1 > left and boundaries[right]-boundaries[new_left-1] <= config.overlap:
                    new_left -= 1
                left = new_left
        else:
            pending.append(block)
        if block['content'].strip():
            previous_text=block['content'].strip()
    flush()
    return output
