import re
from app.search.tokenization import identifiers, TECH


def plan(request):
    """Deterministic policy, not a learned/test-set-specific classifier."""
    exact = request.exact_query
    q = request.query
    identifiers_found = identifiers(q).split()
    occurrence = bool(re.search(r'所有.*(?:位置|出现)|all occurrences|exact text|原文.*查找', q, re.I))
    quoted = re.search(r'[“"「](.+?)[”"」]', q)
    if not exact and quoted:
        exact = quoted[1]
    if not exact and identifiers_found:
        # Preserve original spelling (normalization belongs to BM25, not literal search).
        match = re.search(r'\b[A-Z]{1,8}(?:/[A-Z]{1,4})?\s+\d+(?:\.\d+)*(?:-\d{4})?\b', q) or TECH.search(q)
        if match:
            exact = match[0].strip()
    kind = 'semantic'
    reason = 'Natural language: lexical and semantic recall complement each other'
    channels = ['lexical','vector']
    if occurrence and exact:
        kind, channels, reason = 'occurrence', ['exact'], 'Preserve literal occurrence order and continuation'
    elif exact:
        kind, channels, reason = 'identifier', ['lexical','exact'], 'Explicit literal or technical identifier'
    elif re.search(r'\d{4}|多少|偏差|比例|差值|table|percent|difference|increase|decrease', q, re.I):
        kind, reason = 'numeric_table', 'Broaden candidates; preserve table and adjacent source context'
    if request.mode != 'auto':
        channels = {'hybrid':['lexical','vector']+(['exact'] if exact else []),
                    'lexical':['lexical'],'vector':['vector'],'exact':['exact']}[request.mode]
        reason = 'Explicit mode overrides automatic channel selection'
    return dict(kind=kind, channels=channels, reason=reason, exact_query=exact or q,
                scoped=any(s.document_ids is not None for s in request.scopes),
                section_scoped=bool(request.filters.section_id),
                candidate_limit=min(100, request.budget.backend_candidates*(2 if kind=='numeric_table' else 1)),
                context_recommendation='table/section/adjacent reads' if kind=='numeric_table' else 'bounded adjacent reads')
