"""Wall-clock budgets shared by async requests and their blocking I/O helpers."""

import asyncio
import time
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event

_deadline: ContextVar[float | None] = ContextVar("backend_deadline", default=None)
_cancelled: ContextVar[Event | None] = ContextVar("backend_cancelled", default=None)


@contextmanager
def budget(seconds: float):
    previous = _deadline.get()
    deadline = time.monotonic() + max(0, seconds)
    token = _deadline.set(min(previous, deadline) if previous is not None else deadline)
    cancelled = _cancelled.get() or Event()
    cancel_token = _cancelled.set(cancelled)
    try:
        yield
    except (TimeoutError, asyncio.CancelledError):
        # Shared across copied thread contexts: a cancelled caller cannot authorize late writes.
        cancelled.set()
        raise
    finally:
        _cancelled.reset(cancel_token)
        _deadline.reset(token)


def remaining(limit: float = 30) -> float:
    cancelled = _cancelled.get()
    if cancelled is not None and cancelled.is_set():
        raise TimeoutError("video processing was cancelled")
    deadline = _deadline.get()
    seconds = min(limit, deadline - time.monotonic()) if deadline is not None else limit
    if seconds <= 0:
        raise TimeoutError("video processing deadline exceeded")
    return seconds
