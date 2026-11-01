"""Deterministic number witnesses in verified evidence, not a calculation engine.

Offsets are into the exact model-visible evidence content (including projections).
Only unambiguous parser associations are attached; unknown geometry/units are null.
"""
import json
import re
from .checkpoint import digest
from app.document_structure import table_cells

NUMBER=re.compile(r'(?<![\w.,+−-])[+−-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d{1,3})?(?![\w]|[.,]\d)')
UNIT=re.compile(r'\s*(%|％|USD|EUR|kg|mm|cm|万元|million|thousand)\b|\s*(%|％)')


def regions(content, projection):
    """Yield value regions and reliable cell context; never extract tag attributes."""
    cells=[]
    if projection:
        try:
            obj=json.loads(content);values=obj['cells']
            matches=list(re.finditer(r'"content"\s*:\s*"((?:[^"\\]|\\.)*)"',content))
            if len(matches)!=len(values):return []
            for m,cell in zip(matches,values):
                if m[1]==cell['content']:cells.append((m.start(1),m.end(1),cell,values))
            return cells
        except (ValueError,KeyError,TypeError):return []
    # Whole HTML tables: coordinates from the existing parser, exact text from raw.
    table_ranges=[]
    for table in re.finditer(r'<table\b[^>]*>.*?</table\s*>',content,re.S|re.I):
        raw=table[0]
        if len(re.findall(r'<table\b',raw,re.I))!=1:continue
        values=[c.model_dump() for c in table_cells(raw,'numeric',True)]
        tags=list(re.finditer(r'<(?:td|th)\b[^>]*>(.*?)</(?:td|th)\s*>',raw,re.S|re.I))
        if len(values)!=len(tags):continue
        for tag,cell in zip(tags,values):
            # Inline formatting handled below using text runs; no fabricated offsets.
            a=table.start()+tag.start(1);b=table.start()+tag.end(1)
            cells.append((a,b,cell,values))
        table_ranges.append(table.span())
    # Complete Markdown table, simple exact cells; escaped pipes remain unknown.
    offset=0;lines=content.splitlines(keepends=True);i=0
    while i<len(lines):
        if i+1<len(lines) and '|' in lines[i] and re.fullmatch(r'[\s|:\-]+',lines[i+1]) and '-' in lines[i+1]:
            end=i+2
            while end<len(lines) and '|' in lines[end]:end+=1
            raw=''.join(lines[i:end]);values=[c.model_dump() for c in table_cells(raw,'numeric')]
            spans=[];local=0
            for line in lines[i:end]:
                if not re.fullmatch(r'[\s|:\-]+',line):
                    cuts=[m.start() for m in re.finditer(r'(?<!\\)\|',line)]
                    if line.strip().startswith('|') and line.strip().endswith('|'):
                        spans.extend((offset+local+a+1,offset+local+b) for a,b in zip(cuts,cuts[1:]))
                local+=len(line)
            if len(spans)==len(values):
                cells.extend((a,b,c,values) for (a,b),c in zip(spans,values));table_ranges.append((offset,offset+len(raw)))
            offset+=len(raw);i=end
        else:offset+=len(lines[i]);i+=1
    # Remaining prose; HTML text nodes only (attributes are never operands).
    boundaries=sorted(table_ranges);cursor=0
    for a,b in boundaries+[(len(content),len(content))]:
        if cursor<a:cells.append((cursor,a,None,[]))
        cursor=max(cursor,b)
    return cells


def extract(snippet, run_id, source_id):
    content=snippet['content'];result=[]
    for a,b,cell,all_cells in regions(content,snippet.get('content_is_projection',False)):
        for text_node in re.finditer(r'[^<>]+(?:$|(?=<))',content[a:b]):
            # Reject tag interiors; this regex also matches tag names without this check.
            node_start=a+text_node.start()
            if content.rfind('<',a,node_start+1)>content.rfind('>',a,node_start+1):continue
            for match in NUMBER.finditer(text_node[0]):
                start=node_start+match.start();end=node_start+match.end();raw=content[start:end]
                # Numeric entity spellings are markup, not displayed numeric values.
                entity_start=content.rfind('&',a,start+1)
                if entity_start>=a and re.match(r'&(?:#\d+|#x[0-9a-f]+|[a-z]+);',content[entity_start:b],re.I):
                    entity_end=content.find(';',entity_start,b)
                    if start<=entity_end: continue
                unit_match=UNIT.match(content[end:min(b,end+30)])
                unit='';unit_start=unit_end=None
                if unit_match:
                    group=1 if unit_match.group(1) else 2
                    unit=unit_match.group(group);unit_start=end+unit_match.start(group);unit_end=end+unit_match.end(group)
                headers=[];row_context=[]
                if cell:
                    for other in all_cells:
                        if other.get('is_header') and other['column']<=cell['column']<other['column']+other.get('column_span',1):
                            headers.append(other['content'])
                        if other['row']<=cell['row']<other['row']+other.get('row_span',1):row_context.append(other['content'])
                candidate=dict(candidate_id='num_'+digest([run_id,snippet['evidence_id'],start,end,raw])[:20],
                    source_id=source_id,evidence_id=snippet['evidence_id'],raw_text=raw,start=start,end=end,
                    unit=unit,unit_start=unit_start,unit_end=unit_end,
                    row=cell['row'] if cell else None,column=cell['column'] if cell else None,
                    headers=headers[:8],row_context=row_context[:12],
                    context=content[max(a,start-48):min(b,end+48)],verification_status='verified_source_text')
                result.append(candidate)
                if len(result)>=1000:return result
    return result
