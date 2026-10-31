"""Bounded original-text scan, independent of lexical tokenization."""
import time
import regex


def exact_search(repo, scopes, query, *, regex_mode=False, case_sensitive=True,
                 limit=20, max_chars=2_000_000, timeout=1.0):
    if not query or len(query) > 256:
        raise ValueError('Query must contain 1..256 characters')
    if regex_mode and (regex.search(r'\\[1-9]|\(\?[=!<]|\{\d{5,}',query) or query.count('(')>16):
        raise ValueError('Regex backreferences, lookaround or excessive complexity are not allowed')
    try:
        pattern = regex.compile(query if regex_mode else regex.escape(query), 0 if case_sensitive else regex.IGNORECASE)
    except regex.error as exc:
        raise ValueError('Invalid regular expression') from exc
    deadline = time.monotonic()+min(max(timeout,0.01),2.0)
    max_chars, limit = min(max(max_chars,1),20_000_000),min(max(limit,1),100)
    results, scanned, partial = [],0,False
    for document_id, spec in scopes.items():
        if scanned >= max_chars or time.monotonic() >= deadline:
            partial = True
            break
        window = repo.text_window(document_id,spec['version_id'],limit=max_chars-scanned)
        text = window['content']
        scanned += len(text)
        partial |= len(text) != window['total_chars']
        try:
            for match in pattern.finditer(text,timeout=max(0.001,deadline-time.monotonic())):
                if match.start() == match.end():
                    continue
                with repo.sources.connect() as conn:
                    if spec['builder_version']:
                        rows = conn.execute('''SELECT id FROM retrieval_chunks WHERE version_id=? AND builder_version=?
                            AND start<? AND end>? ORDER BY ordinal LIMIT 100''',
                            (window['version_id'],spec['builder_version'],match.end(),match.start())).fetchall()
                    else:
                        rows = conn.execute('''SELECT id FROM document_nodes WHERE version_id=? AND kind='chunks'
                            AND json_extract(payload,'$.locator.start')<? AND json_extract(payload,'$.locator.end')>?
                            ORDER BY ordinal LIMIT 100''',(window['version_id'],match.end(),match.start())).fetchall()
                results.append(dict(document_id=document_id,version_id=window['version_id'],
                    builder_version=spec['builder_version'],chunk_ids=[r[0] for r in rows],
                    start=match.start(),end=match.end(),match=match[0][:12000],
                    context=text[max(0,match.start()-180):min(len(text),match.end()+180)][:12000],
                    context_start=max(0,match.start()-180)))
                if len(results) >= limit:
                    return dict(results=results,partial=True,reason='result_limit',scanned_chars=scanned)
        except TimeoutError:
            return dict(results=results,partial=True,reason='time_budget',scanned_chars=scanned)
    return dict(results=results,partial=partial,reason='scan_budget' if partial else None,scanned_chars=scanned)
