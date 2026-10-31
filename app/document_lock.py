"""Reentrant cross-process lock shared by ingestion, backfill and deletion."""
import fcntl
import threading
from contextlib import contextmanager

_state = threading.local()


@contextmanager
def document_lock(path, exclusive=True):
    path = path.resolve()
    held = getattr(_state, 'held', None)
    if held is None:
        held = _state.held = {}
    if path in held:
        if exclusive and not held[path]:
            raise RuntimeError('不能升级文档共享锁')
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        held[path] = exclusive
        try:
            yield
        finally:
            del held[path]
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
