"""Request-local progress with a fixed, content-free vocabulary.

There is deliberately no logger or file writer here. Non-opt-in callers, such
as the existing Workbench protocol, observe no change to their response stream.
"""

from contextlib import contextmanager
from contextvars import ContextVar


PHASES = frozenset({"starting", "listing", "loading_cache", "collecting",
                    "reconciling", "validating"})
_observer = ContextVar("upcam_collection_progress", default=None)
_latest = ContextVar("upcam_collection_progress_latest", default=None)


@contextmanager
def observe_collection(observer):
    token = _observer.set(observer)
    previous = _latest.set(None)
    try:
        yield
    finally:
        _latest.reset(previous)
        _observer.reset(token)


def report_collection(phase, *, completed=0, total=0, requests=0, batches=0, passes=0):
    observer = _observer.get()
    if observer is None:
        return
    counts = {"completed": completed, "total": total, "requests": requests,
              "batches": batches, "passes": passes}
    if (phase not in PHASES or any(type(value) is not int or not 0 <= value <= 1000000
                                  for value in counts.values())):
        raise ValueError("Invalid numeric collection progress")
    progress = {"phase": phase, **counts}
    _latest.set(progress)
    observer(progress)


def refresh_collection_progress(*, requests):
    """Refresh counts at read-only transport boundaries, never partial task completion."""
    current = _latest.get()
    if current is not None:
        report_collection(**dict(current, requests=requests))
