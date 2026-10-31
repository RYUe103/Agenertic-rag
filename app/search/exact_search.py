"""Bounded original-text search. Signed cursors pin query, scope, principal and versions."""
import base64
import codecs
import hashlib
import hmac
import json
import secrets
import time
import regex


class ContinuationInvalid(ValueError):
    pass


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


class Cursor:
    def __init__(self, repo, binding):
        self.binding = binding
        with repo.sources.connect() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS exact_cursor_key(singleton INTEGER PRIMARY KEY CHECK(singleton=1), secret BLOB NOT NULL)')
            conn.execute('INSERT OR IGNORE INTO exact_cursor_key VALUES(1,?)', (secrets.token_bytes(32),))
            self.key = conn.execute('SELECT secret FROM exact_cursor_key WHERE singleton=1').fetchone()[0]

    def encode(self, index, offset, byte_offset):
        data = json.dumps(dict(binding=self.binding, index=index, offset=offset,
                               byte_offset=byte_offset, expires=int(time.time())+900), separators=(',', ':')).encode()
        return base64.urlsafe_b64encode(data+hmac.digest(self.key, data, 'sha256')).decode()

    def decode(self, token):
        try:
            if len(token) > 4096:
                raise ValueError()
            raw = base64.b64decode(token, altchars=b'-_', validate=True)
            data, signature = raw[:-32], raw[-32:]
            if not hmac.compare_digest(signature, hmac.digest(self.key, data, 'sha256')):
                raise ValueError()
            value = json.loads(data)
            if value['binding'] != self.binding or value['expires'] < time.time():
                raise ValueError()
            if not isinstance(value['index'], int) or not isinstance(value['offset'], int) or min(value['index'], value['offset']) < 0:
                raise ValueError()
            if not isinstance(value['byte_offset'], int) or value['byte_offset'] < 0:
                raise ValueError()
            return value['index'], value['offset'], value['byte_offset']
        except (ValueError, KeyError, TypeError):
            raise ContinuationInvalid('Continuation expired or query, permissions, scope or versions changed; restart search') from None


def exact_search(repo, scopes, query, *, principal, continuation=None, binding_options=None,
                 regex_mode=False, case_sensitive=True, limit=20, max_chars=2_000_000, timeout=1.0):
    if not query or len(query) > 256:
        raise ValueError('Query must contain 1..256 characters')
    if regex_mode and (regex.search(r'\\[1-9]|\(\?[=!<]|\{\d{5,}', query) or query.count('(')>16):
        raise ValueError('Regex backreferences, lookaround or excessive complexity are not allowed')
    try:
        pattern = regex.compile(query if regex_mode else regex.escape(query), 0 if case_sensitive else regex.IGNORECASE)
    except regex.error as exc:
        raise ValueError('Invalid regular expression') from exc
    cursor = Cursor(repo, fingerprint(dict(principal=principal, kb=repo.knowledge_base_id,
        query=query, regex_mode=regex_mode, case_sensitive=case_sensitive,
        scopes=scopes, options=binding_options)))
    index, offset, byte_offset = cursor.decode(continuation) if continuation else (0, 0, 0)
    items = sorted(scopes.items())
    deadline = time.monotonic()+min(max(timeout, 0.01), 2.0)
    max_chars, limit = min(max(max_chars, 1), 20_000_000), min(max(limit, 1), 100)
    results, scanned = [], 0

    def finish(reason=None, **extra):
        return dict(results=results, partial=reason is not None, reason=reason,
            scanned_chars=scanned, completed_documents=index, total_documents=len(items),
            next_offset=offset, next_byte_offset=byte_offset,
            continuation=cursor.encode(index, offset, byte_offset) if reason else None,
            order='document_id ascending, non-overlapping match start ascending',
            **extra)

    while index < len(items):
        if time.monotonic() >= deadline:
            return finish('time_budget')
        document_id, spec = items[index]
        vid = spec['version_id']
        with repo.sources.connect() as conn:
            rowid = conn.execute('SELECT rowid FROM document_versions WHERE id=?', (vid,)).fetchone()[0]
            # Incremental SQLite blob IO also works on TEXT columns. Unlike SQL
            # substr(CAST(...)), it never materializes an entire huge document.
            with conn.blobopen('document_versions', 'markdown', rowid, readonly=True) as blob:
                total_bytes = len(blob)
            if byte_offset >= total_bytes:
                index, offset, byte_offset = index+1, 0, 0
                continue
            remaining = max_chars-scanned
            # Arbitrary regex cannot be correctly evaluated on truncated subjects
            # (anchors, greediness, unbounded width). Require a full bounded subject.
            if remaining <= 0:
                return finish('scan_budget')
            if regex_mode and total_bytes > remaining*4:
                return finish('document_budget', required_bytes=total_bytes,
                    resumable=total_bytes <= 80_000_000)
            start = 0 if regex_mode else offset
            byte_start = 0 if regex_mode else byte_offset
            # BLOB slices preserve embedded NUL; SQLite TEXT length/substr do not.
            with conn.blobopen('document_versions', 'markdown', rowid, readonly=True) as blob:
                blob.seek(byte_start)
                raw = blob.read(remaining*4)
            decoded = codecs.getincrementaldecoder('utf-8')().decode(raw, final=byte_start+len(raw)==total_bytes)
            if regex_mode and len(decoded)>remaining:
                return finish('document_budget', required_chars=len(decoded),
                    resumable=len(decoded)<=20_000_000)
            text = decoded if regex_mode else decoded[:remaining]
        end_reached = byte_start+len(text.encode()) == total_bytes
        if not regex_mode and not end_reached and len(text)<len(query):
            return finish('scan_budget', required_chars=len(query))
        scanned += len(text)
        # Only accept starts whose whole literal is inside this window; retain
        # the suffix for the next page so cross-window matches cannot be lost.
        safe_end = start+len(text) if end_reached else start+len(text)-len(query)+1
        try:
            for match in pattern.finditer(text, pos=offset if regex_mode else 0,
                                          timeout=max(0.001, deadline-time.monotonic())):
                a, b = start+match.start(), start+match.end()
                if a >= safe_end:
                    break
                if a == b:
                    # Zero-width matches are deliberately excluded.
                    offset = min(start+len(text), b+1)
                    byte_offset = byte_start+len(text[:offset-start].encode())
                    continue
                with repo.sources.connect() as conn:
                    if spec['builder_version']:
                        rows = conn.execute('''SELECT id FROM retrieval_chunks WHERE version_id=? AND builder_version=?
                            AND start<? AND end>? ORDER BY ordinal LIMIT 101''',
                            (vid, spec['builder_version'], b, a)).fetchall()
                    else:
                        rows = conn.execute("""SELECT id FROM document_nodes WHERE version_id=? AND kind='chunks'
                            AND json_extract(payload,'$.locator.start')<? AND json_extract(payload,'$.locator.end')>?
                            ORDER BY ordinal LIMIT 101""", (vid, b, a)).fetchall()
                context_start = max(start, a-180)
                context = text[context_start-start:min(len(text),b-start+180)][:12000]
                results.append(dict(document_id=document_id, version_id=vid, builder_version=spec['builder_version'],
                    chunk_ids=[r[0] for r in rows[:100]], chunk_ids_truncated=len(rows)>100,
                    start=a, end=b, match=match[0][:12000], match_truncated=b-a>12000,
                    context=context, context_start=context_start))
                offset = b
                byte_offset = byte_start+len(text[:offset-start].encode())
                if len(results) >= limit:
                    return finish('result_limit')
                if time.monotonic() >= deadline:
                    return finish('time_budget')
        except TimeoutError:
            return finish('time_budget')
        offset = max(offset, safe_end)
        byte_offset = byte_start+len(text[:offset-start].encode())
        if end_reached:
            index, offset, byte_offset = index+1, 0, 0
    return finish()
