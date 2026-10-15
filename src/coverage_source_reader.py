"""Overlap independent source reads without parallelizing evidence or publication."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict, deque
from threading import Semaphore
from urllib.parse import urlsplit


class SourceReader:
    """Read every requested source once, with modest concurrency per host.

    The limits bound simultaneous connections, not the number of sources.
    Callers with a deadline use lazy sequential reads so their existing stop
    checks remain authoritative. Only private source-cache writes run in workers.
    """
    def __init__(self, loader, *, concurrent=True, workers=8, per_host=2):
        self.loader = loader
        self.concurrent = concurrent
        self.workers, self.per_host = workers, per_host
        self.results = {}

    def prepare(self, keys):
        keys = [key for key in dict.fromkeys(keys) if key not in self.results]
        if not self.concurrent or len(keys) < 2:
            return
        # A slow host must not occupy every worker while unrelated hosts wait.
        hosts = defaultdict(deque)
        for key in keys:
            hosts[urlsplit(key[1]).hostname].append(key)
        keys = []
        while hosts:
            for host in list(hosts):
                keys.append(hosts[host].popleft())
                if not hosts[host]:
                    del hosts[host]
        gates = {urlsplit(url).hostname: Semaphore(self.per_host) for _, url in keys}

        def load(key):
            with gates[urlsplit(key[1]).hostname]:
                return self.loader(*key)

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = {executor.submit(load, key): key for key in keys}
            for future in as_completed(futures):
                key = futures[future]
                try:
                    self.results[key] = (future.result(), None)
                except Exception as error:
                    # One bad page cannot prevent unrelated reads completing.
                    self.results[key] = (None, error)

    def get(self, kind, url):
        key = (kind, url)
        if key not in self.results:
            self.results[key] = (self.loader(kind, url), None)
        value, error = self.results[key]
        if error is not None:
            raise error
        return value
