"""Atomic row groups and exact raw spans; headers/cells are navigation, not evidence."""
import re


def table_page(table, block, start, limit, max_chars):
    raw=table['raw'];cells=table.get('cells',[])
    if table.get('format')=='table_html':
        if len(re.findall(r'<table\b',raw,re.I))!=1: raise ValueError('nested_table_requires_full_read')
        spans=[m.span() for m in re.finditer(r'<tr\b[^>]*>.*?</tr\s*>',raw,re.I|re.S)]
    else:
        spans=[];offset=0
        for line in raw.splitlines(keepends=True):
            if '|' in line and not re.fullmatch(r'\s*\|?[\s:|\-]+\|?\s*',line):spans.append((offset,offset+len(line)))
            offset+=len(line)
    if not spans: raise ValueError('table_rows_unavailable')
    headers=[c for c in cells if c.get('is_header')]
    # Include the connected rowspan group; never cut a merged cell across pages.
    def group_end(row):
        end=row+1
        while True:
            expanded=max([end]+[c['row']+c.get('row_span',1) for c in cells if row<=c['row']<end])
            if expanded==end:return min(end,len(spans))
            end=expanded
    boundaries=[];i=0
    while i<len(spans): boundaries.append(i);i=group_end(i)
    if start not in boundaries and start!=len(spans): raise ValueError('table_start_requires_row_group_boundary')
    chosen=[];end=start
    overhead=len(str(headers))+len(str(table.get('unit')))+512
    used=overhead
    while end<len(spans) and len(chosen)<limit:
        next_end=group_end(end);a=spans[end][0];b=spans[next_end-1][1]
        group_cells=[c for c in cells if end<=c['row']<next_end]
        cost=b-a+len(str(group_cells))
        if used+cost>max_chars or b-a>12000:
            if not chosen: raise OverflowError('table_row_group_exceeds_budget')
            break
        chosen.append((a,b));used+=cost;end=next_end
    source_base=block['locator']['start']
    ranges=[{'locator':{'start':source_base+a,'end':source_base+b}} for a,b in chosen]
    # Header raw rows also have exact locators. No constructed table is registered.
    for row in sorted({c['row'] for c in headers}):
        if row<start or row>=end:
            if row>=len(spans): raise ValueError('table_header_coordinate_mismatch')
            a,b=spans[row];ranges.insert(0,{'locator':{'start':source_base+a,'end':source_base+b}})
    return dict(table_page=dict(table_id=table['id'],row_start=start,row_end=end,
        headers=headers,unit=table.get('unit'),cells=[c for c in cells if start<=c['row']<end]),
        items=ranges,partial=end<len(spans),next_start=end if end<len(spans) else None)
