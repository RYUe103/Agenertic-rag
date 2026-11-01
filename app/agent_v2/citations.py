"""Run-local presentation identities. Never an alternative evidence authority."""
import json
import re

SHORT = re.compile(r'\[(S\d+)\]')
INTERNAL = re.compile(r'\[(ev_[^\]\s]+)\]')
INSUFFICIENT = '证据不足或引用未通过复核，无法给出可靠结论。'


def assign_sources(refs, existing):
    sources = dict(existing)
    reverse = {ident: label for label, ident in sources.items()}
    next_number = max((int(s[1:]) for s in sources), default=0) + 1
    for ident in refs:
        if ident not in reverse:
            sources[f'S{next_number}'] = ident
            reverse[ident] = f'S{next_number}'
            next_number += 1
    return sources


def observation(output, sources, admitted, tool_name="", seen_documents=None):
    """Explicit allowlist: receipts/artifacts never form provider message content."""
    data = output.get('data') or {}
    reverse = {ident: label for label, ident in sources.items()}
    visible = dict(status=output.get('status'), evidence=[])
    if output.get('error'):
        visible['error'] = {'code': output['error'].get('code', 'tool_failed')}
    for snippet in output.get('evidence', []):
        ident = snippet['evidence_id']
        if ident not in admitted or ident not in reverse:
            continue
        visible['evidence'].append(dict(source_id=reverse[ident], evidence_id=ident,
            content=snippet['content'], numeric_candidates=snippet.get('numeric_candidates',[]),
            next_number_start=snippet.get('next_number_start'),document_id=snippet.get('document_id'),
            knowledge_base_id=snippet.get('knowledge_base_id'),version_id=snippet.get('version_id'),
            title=snippet.get('document_name'), page_numbers=snippet.get('page_numbers', []),
            locator=snippet.get('locator'), continuation=snippet.get('continuation'), content_truncated=snippet.get('content_truncated', False)))
    from .context import profile
    _, limits = profile(tool_name)
    seen_documents = seen_documents if seen_documents is not None else set()
    visible['data'] = {k:v for k,v in data.items() if k in {
        'status','partial','next_start','next_after','document_id', 'outline_next_after','outline_start','document_start',
        'knowledge_base_id','version_id','operation','result','unit','precision','operands',
        'table_page','headers','table_id','continuation','units'}}
    documents=[]
    start=data.get('document_start',0)
    for doc in data.get('documents',[])[start:start+limits['navigation_limit']]:
        identity=(doc.get('knowledge_base_id'),doc.get('document_id'),doc.get('version_id'))
        fields=('knowledge_base_id','document_id','version_id','document_name','filename','standard_no','standard_year') if identity in seen_documents else (
            'knowledge_base_id','document_id','version_id','document_name','title','filename','rank','score','standard_title','standard_no','standard_year','metadata_scope')
        documents.append({k:v for k,v in doc.items() if k in fields})
        seen_documents.add(identity)
    if documents: visible['data']['documents']=documents
    if len(data.get('documents',[]))>start+len(documents):
        visible['data']['next_document_start']=start+len(documents)
    if data.get('outline'):
        visible['data']['outline']=data['outline'][:limits['navigation_limit']]
    # Pagination data are navigation only, never model-visible Evidence.
    if data.get('table_page'):
        visible['data']['table_page']=data['table_page']
    handles=[{k:v for k,v in item.items() if k in {'table_id','block_id','format','unit'}}
             for item in data.get('items',[]) if item.get('table_id')]
    if handles: visible['data']['tables']=handles[:limits['navigation_limit']]
    if output.get('_navigation') and not visible['evidence']: visible['navigation']=output['_navigation'][:limits['navigation_limit']]
    if output.get('error'):
        visible['error']={k:v for k,v in output['error'].items() if k in {'code','reason','hint'}}
    return visible


def validate(registry, text, refs, sources, visible_ids, allow_unreferenced=False):
    """Parse claims or bounded marker fallback, then reuse strict Repository checks."""
    claims = []
    errors = []
    body = text.strip()
    if body.startswith('```'):
        body = re.sub(r'^```(?:json)?\s*|\s*```$', '', body)
    if body.startswith('{'):
        try:
            obj = json.loads(body)
            if set(obj) != {'claims'} or not isinstance(obj['claims'], list) or not 1 <= len(obj['claims']) <= 50:
                raise ValueError()
            for c in obj['claims']:
                if (not isinstance(c, dict) or set(c) != {'text', 'source_ids'} or
                    not isinstance(c['text'], str) or not c['text'].strip() or
                    not isinstance(c['source_ids'], list) or len(c['source_ids']) > 50 or
                    any(not isinstance(s, str) or not re.fullmatch(r'S[1-9]\d*', s) for s in c['source_ids']) or
                    re.search(r'\[(?:S\d+|ev_[^\]]+|\d+)\]', c['text'])):
                    raise ValueError()
                if not c['source_ids'] and not allow_unreferenced:
                    errors.append('uncited_claim')
                claims.append({'text':c['text'], 'source_ids':list(dict.fromkeys(c['source_ids']))})
            text = '\n\n'.join(c['text'] + ''.join(' ['+s+']' for s in c['source_ids']) for c in claims)
        except (ValueError, TypeError, KeyError):
            errors.append('invalid_claim_structure')
    reverse = {ident:label for label,ident in sources.items()}
    # Compatibility only for current trusted IDs, never creates an authorization.
    text = INTERNAL.sub(lambda m:'['+reverse.get(m[1], m[1])+']', text)
    labels = list(dict.fromkeys(SHORT.findall(text)))
    for label in labels:
        if label not in sources:
            errors.append('unknown_source')
        elif sources[label] not in visible_ids:
            errors.append('source_not_model_visible')
    if re.search(r'\[\d+\]', text):
        errors.append('unsupported_numeric_citation')
    internal = SHORT.sub(lambda m:'['+sources.get(m[1], 'ev_unknown_source')+']', text)
    checked = registry.validate_answer(internal, refs, allow_unreferenced=allow_unreferenced)
    errors.extend(checked['validation_errors'])
    metadata = []
    for label in labels:
        ident = sources.get(label)
        if ident in checked['citations']:
            metadata.append(dict(number=len(metadata)+1, source_id=label, evidence_id=ident))
    if errors:
        checked.update(answer=INSUFFICIENT, citations={}, validation_errors=list(dict.fromkeys(errors)),
            citation_validation='rejected', evidence_insufficient=True)
        metadata = []; claims = []
    else:
        numbers = {c['source_id']:c['number'] for c in metadata}
        checked['answer'] = SHORT.sub(lambda m:'['+str(numbers[m[1]])+']', text)
        if not claims and labels:
            # Fallback preserves paragraph text; no claim-level entailment is asserted.
            claims = [{'text':SHORT.sub('', text).strip(), 'source_ids':labels}]
    checked.update(claims=claims, citation_metadata=metadata,
        answer_validation='validated' if not errors else 'insufficient_evidence',
        answer_displayable=not errors, trusted_evidence_count=len(refs))
    return checked
