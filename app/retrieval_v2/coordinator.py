"""Bounded shared workers; timed-out calls cannot create unbounded queued work."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

_pool = ThreadPoolExecutor(max_workers=12, thread_name_prefix='retrieval-v2')
_slots = threading.BoundedSemaphore(12)


def run(calls, deadline):
    futures, results, diagnostics = {}, {}, {}
    def measured(fn):
        start = time.monotonic()
        try:
            return fn(), (time.monotonic()-start)*1000
        finally:
            _slots.release()
    for name, fn in calls.items():
        if time.monotonic()>=deadline or not _slots.acquire(blocking=False):
            diagnostics[name] = dict(status='budget_or_capacity_exceeded')
            continue
        futures[_pool.submit(measured,fn)] = name
    done, pending = wait(futures, timeout=max(0,deadline-time.monotonic())) if futures else (set(),set())
    for future in done:
        name = futures[future]
        try:
            result, elapsed = future.result()
            results[name] = result
            diagnostics[name] = dict(status='ok',elapsed_ms=elapsed)
        except Exception as exc:
            # No response bodies, credentials, query, or source text in diagnostics.
            diagnostics[name] = dict(status='failed',error=type(exc).__name__,
                                     code=getattr(exc,'code','backend_error'))
    for future in pending:
        diagnostics[futures[future]] = dict(status='timeout')
    return results, diagnostics
