"""WebCross local-control v1 transport, as used by the official CLI.

The runtime descriptor supplies a Unix socket, NOT a WebSocket URL. The
length-prefixed local frames are adapted to the client's existing RPC/event
envelopes; correlation, permissions and business recovery remain unchanged.
"""

import asyncio
import json
import re
import struct
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def safe_endpoint(url):
    """Connection diagnostics must not expose URL credentials/query strings."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return urlunsplit((parsed.scheme, host + port, parsed.path, "", ""))
    except ValueError:
        return "<invalid endpoint>"


def connection_target(config):
    """Resolve afresh on EVERY connect, including a new client during resume."""
    if config.transport == "websocket":
        return {"transport": "websocket", "endpoint": safe_endpoint(config.ws_url),
                "source": "browser.ws_url"}
    if config.transport != "local":
        raise ValueError("Unsupported browser.transport")
    path = Path(config.runtime_descriptor).expanduser()
    return {"transport": "local", "endpoint": str(path),
            "source": str(path), "stage": "runtime_descriptor"}


def resolve_local_target(target):
    path = Path(target["source"])
    # Descriptors are small public routing documents; never read a Profile here.
    if path.stat().st_size > 65536:
        raise ValueError("WebCross runtime descriptor exceeds 64 KiB")
    descriptor = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(descriptor, dict) or descriptor.get("protocolVersion") != "1":
        raise ValueError("Unsupported WebCross local-control protocol version")
    socket_path = descriptor.get("socketPath")
    instance = descriptor.get("instanceId")
    if not isinstance(socket_path, str) or not Path(socket_path).is_absolute():
        raise ValueError("WebCross runtime descriptor requires an absolute socketPath")
    if not isinstance(instance, str) or not instance:
        raise ValueError("WebCross runtime descriptor requires instanceId")
    target.update(endpoint=socket_path, instanceId=instance, stage="connect")


class LocalControlSocket:
    """Persistent socket with no subprocess, port scan or automatic retry."""

    def __init__(self, reader, writer, max_size):
        self.reader, self.writer = reader, writer
        # Official local-control MAX_FRAME_BYTES, independent of the WS limit.
        self.max_size = min(max_size or 4 * 1024 * 1024, 4 * 1024 * 1024)
        self.bootstrap = deque()
        self.agent_id = None
        self.event_cursor = None
        self.watching = False

    @classmethod
    async def connect(cls, target, config, agent_id=None):
        # Local mode is explicitly selected, never a fallback from failed WS
        # authentication. JWT settings belong exclusively to websocket mode;
        # they are neither transmitted here nor converted into a CLI Profile.
        reader, writer = await asyncio.open_unix_connection(target["endpoint"])
        socket = cls(reader, writer, config.max_message_size_bytes)
        try:
            target["stage"] = "handshake"
            hello = {"type": "hello", "authMode": "unauthenticated", "clientKind": "cli"}
            if agent_id:
                hello["agentId"] = agent_id
            await socket.write(hello)
            while True:
                frame = await socket.read()
                if frame.get("type") == "event":
                    socket.bootstrap.append(socket.notification(frame))
                    continue
                if frame.get("type") == "error":
                    error = frame.get("error") or {}
                    # Codes are bounded metadata; do not expose arbitrary server text.
                    code = str(error.get("code") or "HANDSHAKE_FAILED")[:80]
                    raise ValueError(f"WebCross local handshake rejected ({code}); authenticated hosts require the paired CLI or JWT WebSocket transport")
                valid_id = re.fullmatch(
                    r"agent:open:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                    str(frame.get("agentId") or ""),
                )
                cursor = frame.get("eventCursor")
                if (frame.get("type") != "welcome" or frame.get("authMode") != "unauthenticated"
                        or frame.get("protocolVersion") != "1"
                        or frame.get("instanceId") != target["instanceId"] or not valid_id
                        or (agent_id and frame.get("agentId") != agent_id)
                        or type(cursor) is not int or cursor < 0):
                    raise ValueError("Invalid or stale WebCross local welcome/descriptor")
                socket.agent_id, socket.event_cursor = frame["agentId"], cursor
                return socket
        except BaseException:
            await socket.close()
            raise

    async def write(self, frame):
        payload = json.dumps(frame, ensure_ascii=False).encode("utf-8")
        if len(payload) > self.max_size:
            raise ValueError("WebCross local frame exceeds maximum size")
        self.writer.write(struct.pack(">I", len(payload)) + payload)
        await self.writer.drain()

    async def read(self):
        size = struct.unpack(">I", await self.reader.readexactly(4))[0]
        if size > self.max_size:
            raise ValueError("WebCross local frame exceeds maximum size")
        frame = json.loads(await self.reader.readexactly(size))
        if not isinstance(frame, dict):
            raise ValueError("WebCross local frame must be an object")
        return frame

    async def send(self, raw):
        request = json.loads(raw)
        method, params = request["method"], dict(request.get("params") or {})
        frame = {"id": request["id"]}
        if method in {"events.read", "events.watch", "events.unwatch"}:
            if "afterCursor" in params:
                params["cursor"] = params.pop("afterCursor")
            frame.update(params)
            frame["type"] = {"events.read": "replay", "events.watch": "watch",
                             "events.unwatch": "unwatch"}[method]
        else:
            purpose = params.pop("purpose", None)
            frame.update(type="request", action=method, input=params)
            if purpose:
                frame["purpose"] = purpose
        await self.write(frame)

    @staticmethod
    def notification(frame):
        event = dict(frame["event"])
        event["cursor"] = frame["cursor"]
        return json.dumps({"jsonrpc": "2.0", "method": "System.notification",
                           "params": {"type": "event", "data": event}})

    async def recv(self):
        if self.bootstrap:
            return self.bootstrap.popleft()
        frame = await self.read()
        if frame.get("type") == "event":
            return self.notification(frame)
        if frame.get("type") != "response" or type(frame.get("ok")) is not bool:
            raise ValueError("Unexpected WebCross local response frame")
        response = {"jsonrpc": "2.0", "id": frame.get("id")}
        if frame["ok"]:
            response["result"] = frame.get("result")
        else:
            error = frame.get("error") or {}
            response["error"] = {"code": error.get("code"), "message": error.get("message"),
                                 "data": error.get("details")}
        return json.dumps(response)

    async def close(self):
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (OSError, RuntimeError):
            pass
