"""Task-owned, bounded transport recovery. Business RPCs are never replayed.

The proxy stays stable across socket replacement, including its notification
hub. This lets active workers and HITL readers survive a Dispatcher restart.
Only the protocol bootstrap and read-only recovery inventory are retried.
"""
from __future__ import annotations

import asyncio
import errno
import math
import time
from typing import Any

from abcp_client import ABCPClient, ABCPTransportError, NotificationHub


class RecoveryWindow:
    """One outage deadline shared by a task's bootstrap and connection slots."""

    def __init__(self, seconds=60.0):
        self.seconds = float(seconds)
        if not math.isfinite(self.seconds) or not 0 <= self.seconds <= 60:
            raise ValueError("Dispatcher recovery window must be between 0 and 60 seconds")
        self.started = None
        self.users = 0
        self.exhausted = False

    def join(self):
        if self.started is None:
            self.started = time.monotonic()
        self.users += 1
        return self.started + self.seconds

    def leave(self, *, ready):
        self.users -= 1
        if self.users == 0 and ready and not self.exhausted:
            self.started = None


def task_recovery_window(owner):
    window = getattr(owner, "_browser_recovery_window", None)
    if window is None:
        window = owner._browser_recovery_window = RecoveryWindow(
            getattr(owner.runtime.browser, "reconnect_timeout_seconds", 60))
    return window


def managed_client(client, *, window=None, recovered=None, identity=""):
    # Keep protocol doubles and embedding-provided clients on their own
    # lifecycle; only the concrete native client has the required contract.
    if not isinstance(client, ABCPClient):
        return client
    return RecoveringBrowserClient(client, window=window, recovered=recovered,
                                   identity=identity)


def _identity(registration):
    data = registration.get("data", registration)
    return str(data.get("agentId") or "") if isinstance(data, dict) else ""


def _transient(exc):
    if isinstance(exc, asyncio.TimeoutError):
        return True
    if not isinstance(exc, ABCPTransportError):
        return False
    if exc.transport_code == "ABCP_TRANSPORT_CALL_TIMEOUT":
        return True  # Only called for the recovery bootstrap, never business calls.
    if not exc.connection_fatal:
        return False
    details = exc.connection_details
    # Local protocol/identity/auth validation is permanent. Do not repeatedly
    # retry malformed descriptors, rejected handshakes or JSON-RPC errors.
    if details.get("errorType") in {"ValueError", "JSONDecodeError"}:
        return False
    if details.get("httpStatus") and details["httpStatus"] not in {408, 429, 502, 503, 504}:
        return False
    number = details.get("errno")
    if number is not None and number not in {
        errno.ENOENT, errno.ECONNREFUSED, errno.ECONNRESET, errno.ECONNABORTED,
        errno.EPIPE, errno.ETIMEDOUT, errno.ENETUNREACH, errno.EHOSTUNREACH,
    }:
        return False
    return exc.transport_code in {
        "ABCP_TRANSPORT_CONNECT_FAILED", "ABCP_TRANSPORT_NOT_CONNECTED",
        "ABCP_TRANSPORT_READER_FAILED", "ABCP_TRANSPORT_SEND_FAILED",
    }


class RecoveringBrowserClient:
    def __init__(self, client, *, window=None, recovered=None, identity=""):
        self.config = client.config
        self.window = window or RecoveryWindow(self.config.reconnect_timeout_seconds)
        self.notifications = NotificationHub()
        self._client = client
        self._on_event = client.on_event
        self._recovered = recovered
        self._identity = identity
        self._cursor = None
        self._pending = None
        self._restoring = False
        self._closed = False
        self._blocked = None
        self.last_recovery = None
        self._bootstrap_receipts = {}
        self._relay = None
        self._install(client)

    @property
    def on_event(self):
        return self._on_event

    @on_event.setter
    def on_event(self, callback):
        self._on_event = callback

    def _emit(self, kind, payload):
        if self._on_event:
            try:
                self._on_event(kind, payload)
            except Exception:
                pass  # Diagnostics cannot stop recovery or strand waiters.

    def _install(self, client):
        def event(kind, payload):
            if (client is self._client and not self._closed and kind == "failure"
                    and payload.get("connectionFatal") is True):
                failure = ABCPTransportError(
                    "Dispatcher connection interrupted",
                    transport_code=payload.get("transportCode", ""),
                    connection_fatal=True, connection_details=payload.get("connection"))
                self._begin_recovery(failure)
            self._emit(kind, payload)
        client.on_event = event

    def _attach(self, client, buffer=None):
        if self._relay:
            self._relay()
        def forward(message):
            if buffer is not None and client is not self._client:
                buffer.append(message)
            else:
                self.notifications.publish_once(message)
        self._relay = client.subscribe_notifications(forward)

    @property
    def connection_details(self):
        return self._client.connection_details

    @property
    def connection_usable(self):
        return (not self._closed and self._blocked is None
                and not self._restoring
                and self._client.connection_usable)

    @property
    def event_cursor(self):
        return self._client.event_cursor if self._client.event_cursor is not None else self._cursor

    def set_event_cursor(self, cursor, *, reset=False):
        return self._client.set_event_cursor(cursor, reset=reset)

    async def replay_events(self, **kwargs):
        await self.ensure_connected()
        return await self._client.replay_events(**kwargs)

    async def __aenter__(self):
        try:
            await self.connect()
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(self, *_):
        await self.close()

    async def connect(self):
        if self._closed:
            self._closed = False
            self._blocked = self._pending = None
            self._restoring = False
            self.last_recovery = None
            self.notifications = NotificationHub()
        if self.window.exhausted:
            await self.ensure_connected()
            return
        try:
            if self._identity:
                self._client.restore_local_identity(self._identity)
            await self._client.connect()
            self._identity = self._client.local_agent_id or self._identity
            self._attach(self._client)
        except ABCPTransportError as exc:
            await self._recover_or_raise(exc)

    def _begin_recovery(self, failure):
        if self._closed or self._blocked is not None:
            return
        if self._pending is None or self._pending.done():
            # Capture before new bootstrap events can move the committed cursor.
            self._cursor = self.event_cursor
            deadline = self.window.join()
            self._restoring = True
            self._pending = asyncio.create_task(self._recover(failure, deadline),
                                                name="dispatcher-reconnect")

    async def _recover_or_raise(self, exc):
        if self.connection_usable and self.last_recovery:
            receipt = self.last_recovery
        else:
            self._begin_recovery(exc)
            receipt = None
        if receipt is None and self._pending:
            receipt = await asyncio.shield(self._pending)
        if receipt is None:
            receipt = self.last_recovery or {"status": "blocked", "reasonCode": "client_closed"}
        exc.connection_recovery = receipt
        if receipt["status"] != "ready":
            raise exc
        return receipt

    async def ensure_connected(self):
        if self._closed:
            raise ABCPTransportError("Browser client is closed", transport_code="ABCP_TRANSPORT_CLOSED",
                                     request_sent=False)
        if self.connection_usable:
            return
        exc = ABCPTransportError("Dispatcher connection is unavailable",
            transport_code="ABCP_TRANSPORT_NOT_CONNECTED", connection_fatal=True,
            request_sent=False, connection_details=self.connection_details)
        await self._recover_or_raise(exc)

    async def call(self, method, params=None, *, redact_params=None):
        if self._closed:
            raise ABCPTransportError("Browser client is closed", rpc_method=method,
                                     transport_code="ABCP_TRANSPORT_CLOSED", request_sent=False)
        if not self.connection_usable:
            # This call was not sent. Return that fact after recovery rather
            # than running a previously queued action against changed state.
            exc = ABCPTransportError("Call was not sent while Dispatcher was reconnecting",
                rpc_method=method, transport_code="ABCP_TRANSPORT_NOT_CONNECTED",
                connection_fatal=True, request_sent=False)
        else:
            try:
                result = await self._client.call(method, params, redact_params=redact_params)
                if method == "System.register":
                    self._identity = _identity(result) or self._identity
                return result
            except ABCPTransportError as failure:
                if not failure.connection_fatal and not (
                        self._restoring and failure.transport_code == "ABCP_TRANSPORT_CLOSED"):
                    raise
                exc = failure
        await self._recover_or_raise(exc)
        if method in self._bootstrap_receipts:
            return self._bootstrap_receipts[method]
        # Keep the original request ID, delivery uncertainty, and transport
        # failure. Only the connection's current fatal state has changed.
        exc.connection_fatal = False
        raise exc

    async def wait_for_notification(self, predicate, timeout=None, **kwargs):
        if self._blocked:
            raise self._blocked
        return await self.notifications.wait_for(predicate, timeout, **kwargs)

    def subscribe_notifications(self, callback):
        return self.notifications.subscribe(callback)

    async def _recover(self, original, deadline):
        ready = False
        attempt = 0
        last = original
        candidate = None
        stage = "connect"
        started = self.window.started
        receipt = None
        self._emit("recovery.started", {"timeoutSeconds": self.window.seconds})
        try:
            while _transient(last) and not self.window.exhausted:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                attempt += 1
                self._emit("recovery.attempt", {"attempt": attempt,
                    "remainingSeconds": round(remaining, 2)})
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    # One absolute deadline includes closing the old socket,
                    # bootstrap, event replay and inventory validation.
                    async def restore():
                        nonlocal candidate, stage, ready, receipt
                        if candidate is not None:
                            await candidate.close()
                        await self._client.close()
                        candidate = ABCPClient(self.config)
                        self._install(candidate)
                        if self._identity:
                            candidate.restore_local_identity(self._identity)
                        stage = "connect"
                        await candidate.connect()
                        # Subscribe before registration starts live delivery.
                        # Otherwise the raw hub can deduplicate an event seen
                        # during bootstrap before our stable hub ever sees it.
                        buffered_events = []
                        self._attach(candidate, buffered_events)
                        stage = "System.register"
                        registration = await candidate.call("System.register", {})
                        identity = _identity(registration) or candidate.local_agent_id
                        if self._identity and identity != self._identity:
                            raise ValueError("Dispatcher returned a different protocol identity")
                        stage = "System.getCapabilities"
                        caps = await candidate.call("System.getCapabilities", {"guide": "content"})
                        stage = "events.read"
                        replay = None
                        if self._cursor is not None:
                            replay = await candidate.replay_events(after_cursor=self._cursor)
                            if replay.get("hasMore") or replay.get("truncated"):
                                raise ValueError("Event recovery is incomplete")
                        stage = "Fleet.list"
                        inventory = await candidate.call("Fleet.list", {})
                        context = {}
                        if self._recovered:
                            stage = "session_inventory"
                            context = await self._recovered(candidate, registration, caps, inventory)
                        if time.monotonic() >= deadline:
                            raise asyncio.TimeoutError()
                        if not candidate.connection_usable:
                            raise ABCPTransportError("Connection lost during recovery",
                                transport_code="ABCP_TRANSPORT_READER_FAILED", connection_fatal=True)
                        self._client = candidate
                        self._bootstrap_receipts = {"System.register": registration,
                                                    "System.getCapabilities": caps}
                        self._identity = identity or self._identity
                        candidate = None
                        ready = True
                        self._restoring = False
                        receipt = {"status": "ready", "attempts": attempt,
                            "connection": self.connection_details, "eventReplay": replay,
                            "session": context, "businessActionsReplayed": 0}
                        for message in buffered_events:
                            self.notifications.publish_once(message)
                    await asyncio.wait_for(restore(), timeout=min(remaining, 5.0))
                    break
                except Exception as exc:
                    last = exc
                    if not _transient(last):
                        break
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        await asyncio.sleep(min(remaining, 0.5 * 2 ** min(attempt - 1, 3)))
            if not ready:
                exhausted = self.window.exhausted or time.monotonic() >= deadline
                self.window.exhausted |= exhausted
                receipt = {"status": "blocked", "attempts": attempt,
                    "reasonCode": "reconnect_timeout" if exhausted else
                        getattr(last, "transport_code", None) or type(last).__name__,
                    "probeStage": stage, "errorType": type(last).__name__,
                    "connection": getattr(last, "connection_details", None) or self.connection_details,
                    "businessActionsReplayed": 0}
                self._blocked = ABCPTransportError("Dispatcher recovery did not complete",
                    transport_code=receipt["reasonCode"], connection_fatal=True, request_sent=False)
                self._blocked.connection_recovery = receipt
                self.notifications.fail_waiters(self._blocked)
        finally:
            try:
                if candidate:
                    await asyncio.wait_for(candidate.close(), timeout=.2)
            except Exception:
                pass
            finally:
                self.window.leave(ready=ready)
        receipt["elapsedMs"] = int((time.monotonic() - started) * 1000)
        self.last_recovery = receipt
        self._emit("recovery.finished", receipt)
        return receipt

    async def close(self):
        self._closed = True
        if self._pending and not self._pending.done():
            self._pending.cancel()
            await asyncio.gather(self._pending, return_exceptions=True)
        if self._relay:
            self._relay()
        await self._client.close()
        self.notifications.close()
