"""Cooperative local search without blocking the Agent event loop."""
from __future__ import annotations

import asyncio
import threading
import time

from harness.tools.local_fs import local_fs_search


async def local_fs_search_async(logger, *, cancelled=None, **arguments):
    # Cancelling to_thread alone leaves a live scan behind. Signal it, then join.
    stopped = threading.Event()
    next_check = 0.0

    def should_stop():
        nonlocal next_check
        if stopped.is_set():
            return True
        now = time.monotonic()
        if cancelled is not None and now >= next_check:
            next_check = now + 0.05
            if cancelled():
                stopped.set()
        return stopped.is_set()

    worker = asyncio.create_task(asyncio.to_thread(
        local_fs_search, logger, cancelled=should_stop, **arguments))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        stopped.set()
        # A filesystem syscall is not preemptible. Do not detach the scanner;
        # it stops at the next cooperative checkpoint and closes its handles.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                stopped.set()
            except Exception:
                break
        if worker.done() and not worker.cancelled():
            worker.exception()  # Retrieve a concurrent failure before propagating cancellation.
        raise
