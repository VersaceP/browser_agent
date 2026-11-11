"""Task-tree elapsed time: human waits are an interval union, never summed.

Context propagation includes in-process Workflow children and Browser workers.
This is instrumentation only: it never changes permissions or task decisions.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

_active = ContextVar('task_wait_timelines', default=())


class Timeline:
    def __init__(self, prior=None):
        prior = prior or {}
        self.prior_elapsed = float(prior.get('elapsedSeconds', 0))
        self.prior_human = float(prior.get('humanWaitSeconds', 0))
        self.started = time.monotonic()
        self.human = 0.0
        self.depth = 0
        self.wait_started = None

    def enter(self):
        if not self.depth:
            self.wait_started = time.monotonic()
        self.depth += 1

    def leave(self):
        self.depth -= 1
        if not self.depth:
            self.human += time.monotonic() - self.wait_started
            self.wait_started = None

    def snapshot(self):
        elapsed = self.prior_elapsed + (time.monotonic() - self.started if self.started is not None else 0)
        human = self.prior_human + self.human
        if self.depth:
            human += time.monotonic() - self.wait_started
        return {'elapsedSeconds': elapsed, 'humanWaitSeconds': human,
                'netSeconds': max(0.0, elapsed - human)}

    def resume(self):
        if self.started is None:
            self.started = time.monotonic()

    def pause(self):
        if self.started is not None:
            self.prior_elapsed += time.monotonic() - self.started
            self.started = None


@contextmanager
def human_wait(*extra):
    timelines = tuple(dict.fromkeys((*_active.get(), *extra)))
    for timeline in timelines:
        timeline.enter()
    try:
        yield
    finally:
        for timeline in reversed(timelines):
            timeline.leave()


def timed_human_wait(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        with human_wait():
            return await function(*args, **kwargs)
    return wrapped


def workflow_timing(function):
    @wraps(function)
    async def wrapped(session, *args, **kwargs):
        session.usage.timeline.resume()
        timelines = tuple(dict.fromkeys((*_active.get(), session.usage.timeline)))
        token = _active.set(timelines)
        try:
            return await function(session, *args, **kwargs)
        finally:
            _active.reset(token)
            session.usage.timeline.pause()
    return wrapped
