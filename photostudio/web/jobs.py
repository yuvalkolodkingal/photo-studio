"""Tiny background job manager (export batches, culling) with polled progress."""
from __future__ import annotations

import collections
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable


class Job:
    def __init__(self, kind: str):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.state = "queued"
        self.done = 0
        self.total = 0
        self.results: Any = None
        self.error: str | None = None
        self.created = time.time()
        self.updated = self.created
        self.cancel = threading.Event()

    def progress(self, done: int, total: int, *_a) -> None:
        self.done, self.total, self.updated = int(done), int(total), time.time()

    def to_dict(self, with_results: bool = True) -> dict:
        d = {"id": self.id, "kind": self.kind, "state": self.state, "done": self.done, "total": self.total,
             "error": self.error, "created": self.created, "updated": self.updated}
        if with_results:
            d["results"] = self.results
        return d


class Jobs:
    def __init__(self, workers: int = 1):
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ps-job")
        self.jobs: "collections.OrderedDict[str, Job]" = collections.OrderedDict()
        self._lock = threading.Lock()

    def submit(self, kind: str, fn: Callable[[Job], Any]) -> Job:
        job = Job(kind)
        with self._lock:
            self.jobs[job.id] = job
            while len(self.jobs) > 50:
                self.jobs.popitem(last=False)

        def run():
            job.state = "running"
            try:
                job.results = fn(job)
                job.state = "cancelled" if job.cancel.is_set() else "done"
            except Exception as exc:  # noqa: BLE001
                job.error = f"{type(exc).__name__}: {exc}"
                job.state = "error"
            job.updated = time.time()

        self.pool.submit(run)
        return job

    def get(self, jid: str) -> Job | None:
        with self._lock:
            return self.jobs.get(jid)

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
