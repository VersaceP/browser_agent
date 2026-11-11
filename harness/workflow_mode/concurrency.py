"""Process-local execution capacity and explicit resource ownership.

WebCross remains the authority across processes. Capacity errors are returned,
never retried here. No scheduling rule infers business independence.
"""
from __future__ import annotations
import asyncio
import weakref
from contextlib import asynccontextmanager, AsyncExitStack
from pathlib import Path

_POOLS = weakref.WeakKeyDictionary()


class AdmissionStopped(RuntimeError):
    pass


@asynccontextmanager
async def hold(lock, stop_check=None):
    if stop_check is None:
        await lock.acquire()
    else:
        while True:
            if stop_check():
                raise AdmissionStopped('Stopped while queued; no execution dispatched')
            try:
                await asyncio.wait_for(lock.acquire(), timeout=0.2)
                break
            except asyncio.TimeoutError:
                continue
    try:
        yield
    finally:
        lock.release()


def parallelism(runtime):
    value = getattr(runtime.harness, 'workflow_parallelism', 2)
    if type(value) is not int or value < 1:
        raise ValueError('harness.workflow_parallelism must be a positive integer')
    return value


def pool(runtime):
    browser = runtime.browser
    endpoint = (str(Path(browser.runtime_descriptor).expanduser().resolve())
                if browser.transport == 'local' else browser.ws_url)
    pools = _POOLS.setdefault(asyncio.get_running_loop(), {})
    key = (browser.transport, endpoint)
    size = parallelism(runtime)
    if key not in pools:
        pools[key] = (size, asyncio.Semaphore(size), weakref.WeakValueDictionary())
    size_before, slots, locks = pools[key]
    if size_before != size:
        raise ValueError('One WebCross endpoint must use one workflow_parallelism in this process')
    return slots, locks


@asynccontextmanager
async def lease(runtime, resources=(), *, capacity=False, stop_check=None):
    slots, locks = pool(runtime)
    async with AsyncExitStack() as stack:
        # Total ordering prevents cycles when two items share several resources.
        for key in sorted(set(resources)):
            lock = locks.setdefault(key, asyncio.Lock())
            await stack.enter_async_context(hold(lock, stop_check))
        if capacity:
            await stack.enter_async_context(hold(slots, stop_check))
        yield
