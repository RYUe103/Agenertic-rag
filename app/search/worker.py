"""Single process polling worker; a filesystem lock serializes multiple servers."""
import logging
import threading
from .index_manager import IndexManager

logger = logging.getLogger(__name__)


class IndexWorker:
    def __init__(self, registry, interval=5):
        self.registry, self.interval = registry, interval
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run,name='local-index',daemon=True)
        self.managers = {}

    def run(self):
        while not self.stop.wait(self.interval):
            seen = set()
            for runtime in self.registry.runtimes():
                repo = runtime.repository
                if repo.db_path in seen:
                    continue
                seen.add(repo.db_path)
                try:
                    with repo.connect() as conn:
                        dirty = conn.execute('SELECT 1 FROM search_changes LIMIT 1').fetchone()
                    manager = self.managers.get(repo.db_path)
                    if manager is None:
                        self.managers[repo.db_path] = manager = IndexManager(repo.documents)
                    current = manager.current()
                    interrupted = any(r['status']=='building' for r in manager.state())
                    if dirty or interrupted or current and current['status']=='failed':
                        manager.sync()
                    elif current:
                        manager.open_current()
                except Exception:
                    logger.exception('Local index sync failed; durable changes will be retried')

    def start(self):
        self.thread.start()

    def shutdown(self):
        self.stop.set()
        self.thread.join(timeout=10)
