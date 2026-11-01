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


def observation(output, sources, admitted):
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
            content=snippet['content'], document_id=snippet.get('document_id'),
            title=snippet.get('document_name'), page_numbers=snippet.get('page_numbers', []),
            locator=snippet.get('locator'), content_truncated=snippet.get('content_truncated', False)))
    # Navigation and calculation results are useful; raw body/items duplicate excerpts.
    visible['data'] = {k:v for k,v in data.items() if k in {
        'status','partial','next_start','next_after','outline','documents','document_id',
        'knowledge_base_id','version_id','operation','result','unit','precision','operands'}}
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
