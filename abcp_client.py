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
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import websockets

from runtime_config import ABCPClientConfig


JsonDict = Dict[str, Any]
EventCallback = Callable[[str, JsonDict], None]
NotificationPredicate = Callable[[JsonDict], bool]
NotificationCallback = Callable[[JsonDict], None]


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
    ) -> None:
        super().__init__(message)
        self.rpc_code = rpc_code
        self.rpc_method = str(rpc_method or "")
        self.rpc_data = rpc_data


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
        # The server still does not reliably echo client-side request ids
        # (responses can arrive shaped as observation/data/taskId only); we
        # therefore only allow one RPC in flight at a time and route the next
        # response-shaped message to it. The notification hub demultiplexes
        # everything else.
        self._call_lock = asyncio.Lock()
        self._pending_call: Optional["asyncio.Future[JsonDict]"] = None
        self._pending_request_id: Optional[str] = None
        self._pending_method: Optional[str] = None
        self._closed = False
        self._reader_failure: Optional[BaseException] = None
        self.notifications = NotificationHub()

    async def __aenter__(self) -> "ABCPClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

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
                f"Unable to connect to ABCP Browser WebSocket: {self.config.ws_url}"
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
        pending = self._pending_call
        if pending is not None and not pending.done():
            pending.set_exception(ABCPTransportError("WebSocket closed"))
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def call(self, method: str, params: Optional[JsonDict] = None) -> JsonDict:
        if self._ws is None:
            raise ABCPTransportError("WebSocket is not connected")
        if self._closed:
            raise ABCPTransportError("WebSocket has been closed")
        if self._reader_failure is not None:
            raise ABCPTransportError(
                f"WebSocket background reader failed: {self._reader_failure}"
            )

        request_id = str(uuid.uuid4())
        payload = self._build_payload(request_id, method, params or {})

        async with self._call_lock:
            loop = asyncio.get_running_loop()
            future: "asyncio.Future[JsonDict]" = loop.create_future()
            self._pending_call = future
            self._pending_request_id = request_id
            self._pending_method = method
            try:
                self._emit("request", payload)
                await self._ws.send(json.dumps(payload, ensure_ascii=False))
                try:
                    raw_response = await asyncio.wait_for(
                        future, timeout=self.config.call_timeout_seconds
                    )
                except asyncio.TimeoutError as exc:
                    raise ABCPTransportError(
                        f"Call to {method} timed out ({self.config.call_timeout_seconds}s)"
                    ) from exc
            finally:
                self._pending_call = None
                self._pending_request_id = None
                self._pending_method = None

        if "error" in raw_response and not self._is_implicit_error_envelope(raw_response):
            self._emit("response", raw_response)
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
            )
        response = self._unwrap_response(raw_response)
        self._emit("response", response)
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
                or ABCPTransportError("WebSocket reader stopped")
            )

    def _dispatch_message(self, message: JsonDict) -> None:
        if self._is_response_for_pending(message):
            future = self._pending_call
            if future is not None and not future.done():
                future.set_result(message)
            else:
                # No one is waiting; treat as orphan (still log).
                self._emit("orphan_response", message)
            return
        self._emit("notify", message)
        self.notifications.publish_once(message)

    def _is_response_for_pending(self, message: JsonDict) -> bool:
        if self._pending_call is None:
            return False
        if self._pending_call.done():
            return False
        if not isinstance(message, dict):
            return False

        # JSON-RPC notifications always carry a "method" field and never an
        # id; reject them up-front so the dispatcher never confuses an
        # unsolicited notification for the in-flight response.
        if "method" in message and "id" not in message:
            return False
        if message.get("type") == "notification":
            return False

        id_candidates = (
            message.get("id"),
            message.get("requestId"),
            message.get("correlationId"),
        )
        if self._pending_request_id and self._pending_request_id in id_candidates:
            return True

        if self.config.request_shape.lower() == "jsonrpc":
            # Strict jsonrpc shape: still allow ABCP's response-only-by-shape
            # quirk because the server does not always echo the client id.
            pass

        if message.get("type") in {"response", "result", "error"}:
            return True
        if "result" in message or "error" in message:
            return True

        # ABCP UI returns responses as observation/data/taskId objects with no
        # client-side id. With only one call in flight at a time (enforced by
        # _call_lock), the next response-shaped message is the answer.
        response_keys = {"observation", "suggested_prompt", "data", "taskId"}
        return bool(response_keys.intersection(message.keys())) and "method" not in message

    def _is_implicit_error_envelope(self, message: JsonDict) -> bool:
        """ABCP sometimes returns successful payloads that happen to have an
        observation describing a domain-level error but no top-level `error`
        field. Only treat truly JSON-RPC-style {error:{...}} envelopes as
        transport errors."""
        err = message.get("error")
        return not isinstance(err, dict)

    def _fail_pending(self, exc: BaseException) -> None:
        future = self._pending_call
        if future is not None and not future.done():
            future.set_exception(
                ABCPTransportError(str(exc) or "transport failure")
            )

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

    def _emit(self, event_type: str, payload: JsonDict) -> None:
        if self.on_event:
            self.on_event(event_type, payload)
