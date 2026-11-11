"""Bounded, syntax-preserving evidence captured before the SDK consumes SSE.

Only failed attempts publish this buffer. String contents and numeric digits
are masked before persistence; event order, quotes and escapes remain visible.
No request body, headers, text or thinking content is recorded.
"""
from __future__ import annotations

from collections import deque
from contextvars import ContextVar
import hashlib
import json
import re
import traceback
from typing import Any

import httpx


ACTIVE_CAPTURE: ContextVar[Any] = ContextVar("llm_stream_capture", default=None)


class SyntaxMask:
    def __init__(self):
        self.quoted = False
        self.escaped = False
        self.unicode_left = 0

    def feed(self, value: str) -> str:
        out = []
        for char in value:
            if self.unicode_left:
                out.append("0" if char in "0123456789abcdefABCDEF"
                           else char if char in '"\\' or ord(char) < 32 else "?")
                self.unicode_left -= 1
            elif self.escaped:
                out.append(char if char in '"\\/bfnrtu' else "?" if char.isalnum() else char)
                self.unicode_left = 4 if char == "u" else 0
                self.escaped = False
            elif char == "\\" and self.quoted:
                out.append(char)
                self.escaped = True
            elif char == '"':
                self.quoted = not self.quoted
                out.append(char)
            elif self.quoted:
                # Preserve illegal raw controls so the original failure stays
                # distinguishable from a valid escaped string.
                out.append(char if ord(char) < 32 else "x")
            elif char.isdigit():
                out.append("1")
            elif char in "{}[],: \t\r\n-+.eEtruefalsn":
                out.append(char)
            else:
                out.append("?")
        return "".join(out)


class StreamCapture:
    def __init__(self):
        self.events = deque(maxlen=16)
        self.pending = bytearray()
        self.blocks: dict[int, dict] = {}
        self.request_id = None
        self.sequence = 0
        self.dropped_bytes = 0
        self.available = False
        self.sdk_window = None

    def feed(self, chunk: bytes) -> None:
        # HTTP chunks can contain many small SSE events. Bound the unfinished
        # event, not an otherwise valid batch delivered by the transport.
        for offset in range(0, len(chunk), 8192):
            self._feed_piece(chunk[offset:offset + 8192])

    def _feed_piece(self, chunk: bytes) -> None:
        self.pending.extend(chunk)
        # A single event is bounded independently of the number of deltas.
        if len(self.pending) > 131072:
            self.dropped_bytes += len(self.pending)
            self.pending.clear()
            return
        while True:
            match = re.search(br"\r?\n\r?\n", self.pending)
            if match is None:
                return
            frame = bytes(self.pending[:match.start()])
            del self.pending[:match.end()]
            data = b"\n".join(line[5:].lstrip(b" ") for line in frame.splitlines() if line.startswith(b"data:"))
            self.sequence += 1
            try:
                event = json.loads(data)
            except (ValueError, UnicodeError):
                self.events.append({"sequence": self.sequence, "kind": "invalid_sse_json",
                    "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                    "syntax": SyntaxMask().feed(data.decode("utf-8", "replace"))[:2048]})
                continue
            if not isinstance(event, dict):
                continue
            if self.sdk_window is None:
                self.record(event)

    def record(self, event: dict) -> None:
        kind, index = event.get("type"), event.get("index")
        if kind == "message_start":
            self.message_id = (event.get("message") or {}).get("id")
        if kind == "content_block_start" and type(index) is int:
            block = event.get("content_block") or {}
            if not isinstance(block, dict):
                return
            if block.get("type") == "tool_use":
                if len(self.blocks) >= 64:
                    self.blocks.pop(next(iter(self.blocks)))
                self.blocks[index] = {"mask": SyntaxMask(), "offset": 0,
                                      "toolName": str(block.get("name") or "")[:128]}
        delta = event.get("delta") or {}
        if not isinstance(delta, dict):
            return
        if kind != "content_block_delta" or delta.get("type") != "input_json_delta":
            return
        if type(index) is not int:
            return
        if index not in self.blocks:
            if len(self.blocks) >= 64:
                self.blocks.pop(next(iter(self.blocks)))
            self.blocks[index] = {"mask": SyntaxMask(), "offset": 0, "toolName": None}
        block = self.blocks[index]
        raw = delta.get("partial_json")
        if not isinstance(raw, str):
            return
        safe = block["mask"].feed(raw)
        self.events.append({"sequence": self.sequence, "blockIndex": index,
            "toolName": block["toolName"], "offsetChars": block["offset"],
            "chars": len(raw), "sha256": hashlib.sha256(raw.encode()).hexdigest(),
            "syntax": safe[:2048], "tailSyntax": safe[-512:] if len(safe) > 2048 else None,
            "truncated": len(safe) > 2048})
        block["offset"] += len(raw)

    def failure(self, exc: BaseException) -> dict:
        # Exception messages can quote response values. Preserve the syntax
        # and parser position while masking quoted contents.
        message = re.sub(r"(['\"])(.*?)(\1)", lambda m: m[1] + "<redacted>" + m[3], str(exc))
        frames = traceback.extract_tb(exc.__traceback__)[-8:]
        window = self.sdk_window or self
        return {"captureAvailable": self.available, "requestId": self.request_id,
            "messageId": getattr(window, "message_id", getattr(self, "message_id", None)),
            "originalException": {"type": type(exc).__name__, "message": message[:1000],
                "frames": [{"file": f.filename, "line": f.lineno, "function": f.name} for f in frames]},
            "redaction": "string contents and numeric digits masked; quotes, escapes and delta boundaries retained",
            "events": list(window.events), "windowSource": "sdk_raw_events" if self.sdk_window else "http_sse",
            "httpEventCount": self.sequence, "sdkEventCount": window.sequence if self.sdk_window else None,
            "httpInvalidEvents": [event for event in self.events if event.get("kind") == "invalid_sse_json"],
            "droppedBytes": self.dropped_bytes}


class _ObservedEvents:
    """Observe each raw SDK event immediately before accumulate_event sees it.

    An HTTP chunk can contain many future SSE events. The transport tail alone
    can therefore lose the offending delta before the accumulator raises.
    The proxy preserves the original event objects and delegates close/response.
    """
    def __init__(self, source, capture):
        self.source, self.capture = source, capture

    def __getattr__(self, name):
        return getattr(self.source, name)

    async def __aiter__(self):
        async for event in self.source:
            try:
                self.capture.sequence += 1
                kind = getattr(event, "type", None)
                if (kind in {"message_start", "content_block_start"}
                        or kind == "content_block_delta"
                        and getattr(getattr(event, "delta", None), "type", None) == "input_json_delta"):
                    self.capture.record(event.model_dump())
            except Exception:
                pass
            yield event

    async def close(self):
        await self.source.close()


def observe_sdk_events(stream: Any, capture: StreamCapture) -> None:
    raw = getattr(stream, "_raw_stream", None)
    if raw is not None and hasattr(raw, "__aiter__"):
        capture.sdk_window = StreamCapture()
        stream._raw_stream = _ObservedEvents(raw, capture.sdk_window)


class _CaptureStream(httpx.AsyncByteStream):
    def __init__(self, source, capture):
        self.source, self.capture = source, capture

    async def __aiter__(self):
        async for chunk in self.source:
            try:
                self.capture.feed(chunk)
            except Exception:
                self.capture.dropped_bytes += len(chunk)
            yield chunk

    async def aclose(self):
        await self.source.aclose()


async def _capture_response(response: httpx.Response) -> None:
    capture = ACTIVE_CAPTURE.get()
    if capture is None or "text/event-stream" not in response.headers.get("content-type", ""):
        return
    capture.available = True
    capture.request_id = (response.headers.get("request-id") or response.headers.get("x-request-id")
                          or response.headers.get("x-tt-logid"))
    if response.is_stream_consumed:
        capture.feed(response.content)
    else:
        response.stream = _CaptureStream(response.stream, capture)


def install_capture(client: Any) -> None:
    # The SDK owns its HTTP client. Leave its transport, timeout and connection
    # pool intact, including caller-provided transports used in contract tests.
    http = getattr(client, "_client", None)
    if isinstance(http, httpx.AsyncClient) and _capture_response not in http.event_hooks["response"]:
        http.event_hooks["response"].append(_capture_response)
