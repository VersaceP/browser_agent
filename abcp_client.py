"""
abcp_client.py - ABCP Browser WebSocket RPC client.

The harness sends one RPC at a time, but the server may emit unsolicited
System.notification events at any moment (page lifecycle, HITL resume, etc).
A background reader drains the socket; responses are correlated to the current
in-flight call, and notifications are published to a NotificationHub that
supports both replay (so a wait_for() registered slightly after a notification
still sees it) and multi-subscriber dispatch.
"""

import asyncio
import inspect
import json
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import websockets

from runtime_config import ABCPClientConfig

# Deferred, not top-level: `harness/__init__.py` imports observation.browser_call,
# which imports this module, so any `harness.*` import here at module scope is a
# cycle. Resolved once on first use and cached.
_REDACTORS: Optional[Tuple[Callable[..., Any], Callable[..., Any]]] = None


def _redactors() -> Tuple[Callable[..., Any], Callable[..., Any]]:
    global _REDACTORS
    if _REDACTORS is None:
        from harness.tool_policy import (
            collect_sensitive_replacements,
            sanitize_transport_payload,
        )

        _REDACTORS = (collect_sensitive_replacements, sanitize_transport_payload)
    return _REDACTORS


JsonDict = Dict[str, Any]
EventCallback = Callable[[str, JsonDict], None]
NotificationPredicate = Callable[[JsonDict], bool]
NotificationCallback = Callable[[JsonDict], None]


# ``ABCPTransportError`` also represents JSON-RPC action failures, so its
# Python type alone cannot answer whether the underlying WebSocket is usable.
# Keep that distinction explicit and machine-readable at the client boundary.
ABCP_TRANSPORT_CONNECT_FAILED = "ABCP_TRANSPORT_CONNECT_FAILED"
ABCP_TRANSPORT_NOT_CONNECTED = "ABCP_TRANSPORT_NOT_CONNECTED"
ABCP_TRANSPORT_CLOSED = "ABCP_TRANSPORT_CLOSED"
ABCP_TRANSPORT_READER_FAILED = "ABCP_TRANSPORT_READER_FAILED"
ABCP_TRANSPORT_SEND_FAILED = "ABCP_TRANSPORT_SEND_FAILED"
ABCP_TRANSPORT_CALL_TIMEOUT = "ABCP_TRANSPORT_CALL_TIMEOUT"
ABCP_RPC_ERROR = "ABCP_RPC_ERROR"
ABCP_TRANSPORT_UNKNOWN = "ABCP_TRANSPORT_UNKNOWN"


class ABCPTransportError(RuntimeError):
    """Raised when the WebSocket transport cannot complete a request.

    JSON-RPC failures retain their machine-readable code/data.  Callers that
    need to distinguish a remote action timeout from a local socket failure
    must not parse the rendered exception string.
    """

    def __init__(
        self,
        message: str,
        *,
        rpc_code: Optional[int] = None,
        rpc_method: str = "",
        rpc_data: Any = None,
        transport_code: str = ABCP_TRANSPORT_UNKNOWN,
        connection_fatal: bool = False,
        request_sent: Optional[bool] = None,
    ) -> None:
        super().__init__(message)
        self.rpc_code = rpc_code
        self.rpc_method = str(rpc_method or "")
        self.rpc_data = rpc_data
        self.transport_code = str(transport_code or ABCP_TRANSPORT_UNKNOWN)
        self.connection_fatal = bool(connection_fatal)
        # ``None`` means a send failed while its delivery was indeterminate.
        self.request_sent = request_sent if isinstance(request_sent, bool) else None


@dataclass
class _NotificationWaiter:
    predicate: NotificationPredicate
    future: "asyncio.Future[JsonDict]"


class NotificationHub:
    """In-process publisher/subscriber for ABCP browser notifications.

    Why a hub instead of a single asyncio.Queue: with a Queue, multiple
    consumers compete for messages — one get() dequeues, another loses.
    HITL waiter, page-crashed watchdog, and a debug logger may all need the
    same event. A hub broadcasts to every subscriber and every matching
    waiter.

    Why a replay buffer: there is always a small gap between
    Hitl.requestPause returning and the caller registering wait_for(). If
    the server sent hitl_resumed inside that gap, a fresh waiter would hang
    forever. wait_for(..., replay_window_s=N) checks the buffer first for any
    matching message published in the last N seconds.
    """

    def __init__(self, *, replay_size: int = 64, replay_ttl_seconds: float = 30.0):
        self._replay: Deque[Tuple[float, JsonDict]] = deque(maxlen=replay_size)
        self._replay_ttl_seconds = replay_ttl_seconds
        self._dedupe_limit = max(64, replay_size * 4)
        self._dedupe_order: Deque[Tuple[float, str]] = deque()
        self._dedupe_keys: set[str] = set()
        self._waiters: List[_NotificationWaiter] = []
        self._subscribers: List[NotificationCallback] = []
        self._closed = False

    def publish(self, message: JsonDict) -> None:
        if self._closed:
            return
        now = time.monotonic()
        self._replay.append((now, message))
        # Snapshot — predicate evaluation may register/remove waiters.
        for waiter in list(self._waiters):
            if waiter.future.done():
                continue
            try:
                matched = bool(waiter.predicate(message))
            except Exception:
                matched = False
            if matched:
                waiter.future.set_result(message)
        for subscriber in list(self._subscribers):
            try:
                subscriber(message)
            except Exception:
                # Subscribers are passive observers; don't let a buggy one
                # break delivery for others.
                pass

    def publish_once(self, message: JsonDict) -> bool:
        """Publish one logical ABCP control event at most once.

        ABCP may deliver the same persisted event through its default Agent
        stream, an explicit Events.watch subscription, and a Harness owner
        relay.  Stable eventId/cursor identity is transport metadata, not a
        second occurrence of the browser action.

        Messages without stable control-event identity retain the historical
        broadcast behavior.  ``publish`` also remains unconditional for local
        synthetic notifications and tests.
        """

        if self._closed:
            return False
        key = self._control_event_key(message)
        if key:
            now = time.monotonic()
            cutoff = now - max(0.0, self._replay_ttl_seconds)
            while self._dedupe_order and (
                self._dedupe_order[0][0] < cutoff
                or len(self._dedupe_order) >= self._dedupe_limit
            ):
                _timestamp, expired_key = self._dedupe_order.popleft()
                self._dedupe_keys.discard(expired_key)
            if key in self._dedupe_keys:
                return False
            self._dedupe_keys.add(key)
            self._dedupe_order.append((now, key))
        self.publish(message)
        return True

    @staticmethod
    def _control_event_key(message: JsonDict) -> Optional[str]:
        candidates: List[JsonDict] = []
        if isinstance(message, dict):
            candidates.append(message)
            params = message.get("params")
            if isinstance(params, dict):
                candidates.append(params)
                data = params.get("data")
                if isinstance(data, dict):
                    candidates.append(data)
            data = message.get("data")
            if isinstance(data, dict):
                candidates.append(data)

        for candidate in candidates:
            event_id = str(
                candidate.get("eventId") or candidate.get("event_id") or ""
            ).strip()
            if event_id:
                return f"event:{event_id}"
        for candidate in candidates:
            cursor = candidate.get("cursor")
            event = str(candidate.get("event") or "").strip()
            if cursor is not None and event:
                return f"cursor:{cursor}:{event}"
        return None

    async def wait_for(
        self,
        predicate: NotificationPredicate,
        timeout: float,
        *,
        replay_window_seconds: Optional[float] = None,
        replay_predicate: Optional[NotificationPredicate] = None,
    ) -> Optional[JsonDict]:
        """Wait for a notification matching `predicate`.

        If `replay_window_seconds` is set, also check the replay buffer for
        messages received within the last N seconds. By default the replay
        scan uses the same `predicate`; pass `replay_predicate` to apply a
        STRICTER filter to replayed messages (the live waiter still uses the
        broader `predicate`). This is important when `predicate` matches
        recurring events (e.g. page_navigated) that should only count as a
        match if they happen AFTER wait registration.
        """
        scan_predicate = replay_predicate if replay_predicate is not None else predicate
        match = self.peek_replay(scan_predicate, window_seconds=replay_window_seconds)
        if match is not None:
            return match
        if self._closed:
            return None
        loop = asyncio.get_running_loop()
        waiter = _NotificationWaiter(predicate=predicate, future=loop.create_future())
        self._waiters.append(waiter)
        try:
            return await asyncio.wait_for(waiter.future, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            try:
                self._waiters.remove(waiter)
            except ValueError:
                pass

    def peek_replay(
        self,
        predicate: NotificationPredicate,
        *,
        window_seconds: Optional[float],
    ) -> Optional[JsonDict]:
        """Synchronously scan the replay buffer for the most recent message
        within `window_seconds` matching `predicate`. Returns None if
        `window_seconds` is None, the buffer is empty, or nothing matches.
        """
        if window_seconds is None or not self._replay:
            return None
        cutoff = time.monotonic() - max(0.0, window_seconds)
        for ts, message in reversed(self._replay):
            if ts < cutoff:
                break
            try:
                if predicate(message):
                    return message
            except Exception:
                continue
        return None

    def subscribe(self, callback: NotificationCallback) -> Callable[[], None]:
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass

        return unsubscribe

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for waiter in self._waiters:
            if not waiter.future.done():
                waiter.future.cancel()
        self._waiters.clear()
        self._subscribers.clear()
        self._replay.clear()
        self._dedupe_order.clear()
        self._dedupe_keys.clear()


def _connect_supports_proxy_arg() -> bool:
    try:
        return "proxy" in inspect.signature(websockets.connect).parameters
    except (TypeError, ValueError):
        return False


def _is_local_ws_url(ws_url: str) -> bool:
    host = urlparse(ws_url).hostname
    return host in {"localhost", "127.0.0.1", "::1"}


@dataclass
class _PendingCall:
    """One RPC awaiting its answer, keyed in ABCPClient._pending by request id."""

    future: "asyncio.Future[JsonDict]"
    method: str
    request_id: str


class ABCPClient:
    def __init__(
        self,
        config: ABCPClientConfig,
        on_event: Optional[EventCallback] = None,
    ):
        self.config = config
        self.on_event = on_event
        self._ws: Any = None
        self._reader_task: Optional[asyncio.Task] = None
        # Responses are routed by the id we sent, and by nothing else. Probed
        # against the live panel on 2026-09-12: 17 of 17 calls across System,
        # Fleet, Page, DOM and Workflow echoed the request id, error envelopes
        # included, and not one response arrived without one.
        #
        # The shape fallback this replaces ("the next response-shaped message
        # belongs to the call in flight") needed a global lock to be safe, and
        # was unsafe anyway the moment anything arrived out of order. The same
        # probe sent five requests without waiting and the platform answered
        # them in a different order than they were sent, so the fallback was
        # one late or reordered response away from handing a caller someone
        # else's result. A response that matches no pending id is now reported
        # as an orphan instead of being delivered to whoever is waiting.
        self._pending: Dict[str, "_PendingCall"] = {}
        self._closed = False
        self._reader_failure: Optional[BaseException] = None
        self.notifications = NotificationHub()
        # The platform assigns cursors to durable Agent events.  This remains
        # transport state: it never decides what an event means, only which
        # events have already been delivered to the harness.
        self._event_cursor: Optional[int] = None

    async def __aenter__(self) -> "ABCPClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    @property
    def connection_usable(self) -> bool:
        """Local transport fact only; not proof of Fleet/page availability."""
        return self._ws is not None and not self._closed and self._reader_failure is None

    async def connect(self) -> None:
        headers = {}
        if self.config.jwt_token:
            headers["Authorization"] = f"Bearer {self.config.jwt_token}"

        kwargs = {
            "open_timeout": self.config.connect_timeout_seconds,
            "ping_interval": self.config.ping_interval_seconds,
            "max_size": self.config.max_message_size_bytes,
        }
        if _is_local_ws_url(self.config.ws_url) and _connect_supports_proxy_arg():
            kwargs["proxy"] = None

        try:
            try:
                self._ws = await websockets.connect(
                    self.config.ws_url,
                    additional_headers=headers or None,
                    **kwargs,
                )
            except TypeError:
                self._ws = await websockets.connect(
                    self.config.ws_url,
                    extra_headers=headers or None,
                    **kwargs,
                )
        except OSError as exc:
            raise ABCPTransportError(
                f"Unable to connect to ABCP Browser WebSocket: {self.config.ws_url}",
                transport_code=ABCP_TRANSPORT_CONNECT_FAILED,
                connection_fatal=True,
                request_sent=False,
            ) from exc

        self._closed = False
        self._reader_failure = None
        self._reader_task = asyncio.create_task(
            self._read_loop(), name="abcp-client-reader"
        )

    async def close(self) -> None:
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        self.notifications.close()
        for entry in list(self._pending.values()):
            if not entry.future.done():
                entry.future.set_exception(ABCPTransportError(
                    "WebSocket closed",
                    rpc_method=entry.method,
                    transport_code=ABCP_TRANSPORT_CLOSED,
                    connection_fatal=not self._closed,
                    request_sent=True,
                ))
        self._pending.clear()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def call(
        self,
        method: str,
        params: Optional[JsonDict] = None,
        *,
        redact_params: Optional[Set[str]] = None,
    ) -> JsonDict:
        """Send one RPC and return its unwrapped response.

        `redact_params` names the param keys whose values are secret. The
        browser still receives the real values; the keys only govern what the
        transport event log may keep, for both the request and the response the
        platform echoes them back in.
        """
        if self._ws is None:
            raise ABCPTransportError(
                "WebSocket is not connected",
                transport_code=ABCP_TRANSPORT_NOT_CONNECTED,
                connection_fatal=True,
                request_sent=False,
            )
        if self._closed:
            raise ABCPTransportError(
                "WebSocket has been closed",
                transport_code=ABCP_TRANSPORT_CLOSED,
                connection_fatal=True,
                request_sent=False,
            )
        if self._reader_failure is not None:
            raise ABCPTransportError(
                f"WebSocket background reader failed: {self._reader_failure}",
                rpc_method=method,
                transport_code=ABCP_TRANSPORT_READER_FAILED,
                connection_fatal=True,
                request_sent=False,
            )

        request_id = str(uuid.uuid4())
        payload = self._build_payload(request_id, method, params or {})
        # Derived once from the request: the response has to be scrubbed with
        # the request's secrets, because the platform echoes typed values back
        # in its own feedback fields.
        secrets = _redactors()[0](params or {}, redact_params)

        loop = asyncio.get_running_loop()
        future: "asyncio.Future[JsonDict]" = loop.create_future()
        self._pending[request_id] = _PendingCall(
            future=future, method=method, request_id=request_id,
        )
        try:
            self._emit("request", payload, secrets)
            try:
                await self._ws.send(json.dumps(payload, ensure_ascii=False))
            except Exception as exc:
                raise ABCPTransportError(
                    f"WebSocket send failed for {method}: {exc}",
                    rpc_method=method,
                    transport_code=ABCP_TRANSPORT_SEND_FAILED,
                    connection_fatal=True,
                    request_sent=None,
                ) from exc
            try:
                raw_response = await asyncio.wait_for(
                    future, timeout=self.config.call_timeout_seconds
                )
            except asyncio.TimeoutError as exc:
                raise ABCPTransportError(
                    f"Call to {method} timed out ({self.config.call_timeout_seconds}s)",
                    rpc_method=method,
                    transport_code=ABCP_TRANSPORT_CALL_TIMEOUT,
                    request_sent=True,
                ) from exc
        finally:
            self._pending.pop(request_id, None)

        if "error" in raw_response and not self._is_implicit_error_envelope(raw_response):
            self._emit("response", raw_response, secrets)
            rpc_error = (
                raw_response.get("error")
                if isinstance(raw_response.get("error"), dict)
                else {}
            )
            raw_code = rpc_error.get("code")
            raise ABCPTransportError(
                self._format_jsonrpc_error(method, raw_response),
                rpc_code=raw_code if isinstance(raw_code, int) else None,
                rpc_method=method,
                rpc_data=rpc_error.get("data"),
                transport_code=ABCP_RPC_ERROR,
                request_sent=True,
            )
        response = self._unwrap_response(raw_response)
        self._emit("response", response, secrets)
        return response

    async def wait_for_notification(
        self,
        predicate: NotificationPredicate,
        timeout: float,
        *,
        replay_window_seconds: Optional[float] = None,
        replay_predicate: Optional[NotificationPredicate] = None,
    ) -> Optional[JsonDict]:
        """Wait up to `timeout` seconds for a notification matching `predicate`.

        Set `replay_window_seconds` to also accept any matching notification
        that arrived in the last N seconds (race-safe for "publish-before-wait").
        Set `replay_predicate` to apply a STRICTER filter to the replay scan
        than to live messages — important for predicates that match recurring
        events (e.g. page_navigated) which should only count as a fresh match
        when emitted after wait registration. Returns the matched message,
        or None on timeout.
        """
        return await self.notifications.wait_for(
            predicate,
            timeout=timeout,
            replay_window_seconds=replay_window_seconds,
            replay_predicate=replay_predicate,
        )

    def subscribe_notifications(
        self, callback: NotificationCallback
    ) -> Callable[[], None]:
        """Register a passive observer for every notification. Returns an
        unsubscribe function. Subscribers must not block."""
        return self.notifications.subscribe(callback)

    @property
    def event_cursor(self) -> Optional[int]:
        """Largest durable event cursor delivered on this client, if known."""

        return self._event_cursor

    def set_event_cursor(self, cursor: Any, *, reset: bool = False) -> Optional[int]:
        """Record a server-provided event cursor without inventing progress.

        ``reset`` is reserved for a fresh registration baseline.  Normal event
        delivery never moves the cursor backwards, which prevents a stale
        notification from reopening a replay gap.
        """

        if isinstance(cursor, bool):
            return self._event_cursor
        try:
            normalized = int(cursor)
        except (TypeError, ValueError):
            return self._event_cursor
        if normalized < 0:
            return self._event_cursor
        if reset or self._event_cursor is None or normalized >= self._event_cursor:
            self._event_cursor = normalized
        return self._event_cursor

    async def replay_events(
        self,
        *,
        after_cursor: Any,
        limit: int = 100,
        max_pages: int = 20,
    ) -> JsonDict:
        """Read and publish a bounded durable event sequence after a gap.

        ``events.read`` is a WebSocket transport operation rather than an
        Action, so it deliberately lives here instead of the model-visible
        capability layer.  Events are published through the same hub as live
        notifications; stable eventId/cursor de-duplication therefore covers a
        race between replay and the default live subscription.

        The cursor advances only after every event in a page was delivered to
        the hub.  A finite page bound prevents an arbitrarily old stream from
        starving connection recovery; callers can resume from ``nextCursor``.
        """

        if isinstance(after_cursor, bool):
            raise ValueError("after_cursor must be a non-negative integer")
        try:
            cursor = int(after_cursor)
        except (TypeError, ValueError) as exc:
            raise ValueError("after_cursor must be a non-negative integer") from exc
        if cursor < 0:
            raise ValueError("after_cursor must be a non-negative integer")

        page_limit = min(500, max(1, int(limit)))
        page_budget = min(100, max(1, int(max_pages)))
        initial_cursor = cursor
        latest_cursor: Optional[int] = None
        pages_read = 0
        events_read = 0
        events_published = 0
        has_more = False

        while pages_read < page_budget:
            prior_cursor = cursor
            page = await self.call(
                "events.read",
                {"afterCursor": cursor, "limit": page_limit},
            )
            if not isinstance(page, dict):
                raise RuntimeError("events.read returned a non-object response")
            events = page.get("events")
            if not isinstance(events, list):
                raise RuntimeError("events.read returned no event list")
            next_cursor = self._event_cursor_value(page.get("nextCursor"))
            if next_cursor is None or next_cursor < cursor:
                raise RuntimeError("events.read returned an invalid nextCursor")
            latest = self._event_cursor_value(page.get("latestCursor"))
            if latest is not None:
                latest_cursor = latest

            for event in events:
                if not isinstance(event, dict):
                    raise RuntimeError("events.read returned a non-object event")
                event_name = str(event.get("event") or "").strip()
                if not event_name:
                    raise RuntimeError("events.read returned an unnamed event")
                message: JsonDict = {
                    "jsonrpc": "2.0",
                    "method": "System.notification",
                    "params": {"type": "event", "data": event},
                }
                if self.notifications.publish_once(message):
                    events_published += 1
                events_read += 1

            # Every event above reached the hub successfully.  Commit only the
            # server's safe page cursor, never a cursor inferred from payload.
            self.set_event_cursor(next_cursor)
            cursor = next_cursor
            pages_read += 1
            has_more = bool(page.get("hasMore"))
            if not has_more:
                break
            if next_cursor <= prior_cursor:
                raise RuntimeError("events.read reported more events without progress")

        return {
            "afterCursor": initial_cursor,
            "nextCursor": cursor,
            "latestCursor": latest_cursor,
            "pagesRead": pages_read,
            "eventsRead": events_read,
            "eventsPublished": events_published,
            "hasMore": has_more,
            "truncated": bool(has_more and pages_read >= page_budget),
        }

    async def _read_loop(self) -> None:
        try:
            while not self._closed:
                try:
                    raw = await self._ws.recv()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not self._closed:
                        self._reader_failure = exc
                    self._fail_pending(exc)
                    return
                message = self._decode_message(raw)
                self._dispatch_message(message)
        finally:
            # Make sure no caller hangs waiting on a future that will never
            # resolve once the read loop is gone.
            self._fail_pending(
                self._reader_failure
                or ABCPTransportError(
                    "WebSocket reader stopped",
                    transport_code=ABCP_TRANSPORT_READER_FAILED,
                    connection_fatal=not self._closed,
                    request_sent=True,
                )
            )

    def _dispatch_message(self, message: JsonDict) -> None:
        entry = self._pending_for(message)
        if entry is not None:
            if not entry.future.done():
                entry.future.set_result(message)
            else:
                # The caller already gave up (timeout); the answer is late.
                self._emit("orphan_response", message)
            return
        if self._looks_like_response(message):
            # Response-shaped but matching no pending id: a late answer to a
            # call that already timed out, or a reply the platform sent without
            # echoing an id. Either way it belongs to nobody, and guessing an
            # owner is exactly the cross-talk this routing exists to prevent.
            # It is emitted rather than dropped so that "the platform stopped
            # echoing ids" shows up in the transport log instead of as calls
            # mysteriously timing out.
            self._emit("orphan_response", message)
            return
        self._emit("notify", message)
        if self.notifications.publish_once(message):
            self.set_event_cursor(self._event_cursor_from_message(message))

    @staticmethod
    def _event_cursor_value(value: Any) -> Optional[int]:
        if isinstance(value, bool):
            return None
        try:
            cursor = int(value)
        except (TypeError, ValueError):
            return None
        return cursor if cursor >= 0 else None

    @classmethod
    def _event_cursor_from_message(cls, message: Any) -> Optional[int]:
        if not isinstance(message, dict):
            return None
        candidates: List[Any] = [message]
        params = message.get("params")
        if isinstance(params, dict):
            candidates.append(params)
            candidates.append(params.get("data"))
        candidates.append(message.get("data"))
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if not str(candidate.get("event") or "").strip():
                continue
            cursor = cls._event_cursor_value(candidate.get("cursor"))
            if cursor is not None:
                return cursor
        return None

    def _pending_for(self, message: JsonDict) -> Optional["_PendingCall"]:
        """The in-flight call this message answers, matched by id alone."""
        if not isinstance(message, dict) or not self._pending:
            return None
        # A JSON-RPC notification carries a method and no id. Reject it here so
        # a notification can never be mistaken for somebody's response.
        if "method" in message and "id" not in message:
            return None
        if message.get("type") == "notification":
            return None
        for key in ("id", "requestId", "correlationId"):
            value = message.get(key)
            if isinstance(value, str) and value in self._pending:
                return self._pending[value]
        return None

    @staticmethod
    def _looks_like_response(message: JsonDict) -> bool:
        """Whether an unmatched message is shaped like somebody's answer.

        Used only to decide between the `orphan_response` and `notify`
        channels. It never selects an owner -- that is what routing by id is
        for -- so a wrong guess here costs a log line, not a wrong result.
        """
        if not isinstance(message, dict) or "method" in message:
            return False
        if message.get("type") in {"response", "result", "error"}:
            return True
        if "result" in message or "error" in message:
            return True
        return bool(
            {"observation", "suggested_prompt", "data", "taskId"}
            .intersection(message.keys())
        )

    def _is_implicit_error_envelope(self, message: JsonDict) -> bool:
        """ABCP sometimes returns successful payloads that happen to have an
        observation describing a domain-level error but no top-level `error`
        field. Only treat truly JSON-RPC-style {error:{...}} envelopes as
        transport errors."""
        err = message.get("error")
        return not isinstance(err, dict)

    def _fail_pending(self, exc: BaseException) -> None:
        """Fail every in-flight call. The transport is gone for all of them."""
        for entry in list(self._pending.values()):
            if entry.future.done():
                continue
            if isinstance(exc, ABCPTransportError):
                entry.future.set_exception(exc)
            else:
                entry.future.set_exception(ABCPTransportError(
                    str(exc) or "transport failure",
                    rpc_method=entry.method,
                    transport_code=ABCP_TRANSPORT_READER_FAILED,
                    connection_fatal=True,
                    request_sent=True,
                ))

    def _build_payload(self, request_id: str, method: str, params: JsonDict) -> JsonDict:
        shape = self.config.request_shape.lower()

        if shape == "jsonrpc":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }

        if shape == "typed":
            return {
                "type": "request",
                "id": request_id,
                "method": method,
                "params": params,
            }

        return {
            "id": request_id,
            "method": method,
            "params": params,
        }

    def _decode_message(self, raw: Any) -> JsonDict:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if not isinstance(raw, str):
            return {"raw": raw}
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}
        return decoded if isinstance(decoded, dict) else {"data": decoded}

    def _unwrap_response(self, message: JsonDict) -> JsonDict:
        if "result" in message and isinstance(message["result"], dict):
            return message["result"]
        if message.get("type") == "response" and isinstance(message.get("payload"), dict):
            return message["payload"]
        return message

    def _format_jsonrpc_error(self, method: str, message: JsonDict) -> str:
        error = message.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            text = error.get("message") or error.get("data") or "unknown error"
            if code is not None:
                return f"ABCP Browser call {method} failed: {code} {text}"
            return f"ABCP Browser call {method} failed: {text}"
        return f"ABCP Browser call {method} failed: {error or 'unknown error'}"

    def _emit(
        self,
        event_type: str,
        payload: JsonDict,
        secrets: Optional[Dict[str, str]] = None,
    ) -> None:
        """Hand one transport frame to the event hook, scrubbed and bounded.

        Nothing downstream of this re-reads the frame, so this is the last place
        a credential can be removed before `make_browser_event_logger` writes it
        to the run log. Every frame contributes its own URL-embedded
        credentials, which needs no declaration from anyone; `secrets` adds the
        keys the caller declared for this particular call, which is the only way
        a bare `Input.type.text` password can be recognised. Notifications and
        orphan responses arrive with no call context and rely on the first
        source alone.
        """
        if not self.on_event:
            return
        collect, sanitize = _redactors()
        table = dict(collect(payload))
        if secrets:
            table.update(secrets)
        self.on_event(event_type, sanitize(payload, table))
