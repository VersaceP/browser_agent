"""极简 session 持久化 — 把 TeammateContext 序列化到 JSON 文件。"""
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from core.context import TeammateContext


class SessionPersistenceError(Exception):
    pass


def save_session(context: TeammateContext, target: str) -> Dict[str, Any]:
    """保存到文件。target 可以是文件路径或目录(目录则用 session_id+时间戳命名)"""
    p = Path(target)
    if p.exists() and p.is_dir():
        fname = f"{context.session_id}_{int(time.time())}.json"
        p = p / fname
    elif p.suffix.lower() != ".json":
        p = p.with_suffix(".json")
    p.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "version": 1,
        "saved_at": time.time(),
        "context": context.to_dict(),
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return {
        "file_path": str(p),
        "size_bytes": p.stat().st_size,
        "message_count": len(context.messages),
        "token_usage": context.token_usage,
    }


def load_session(path: str) -> Tuple[TeammateContext, Dict[str, Any]]:
    """从文件恢复 TeammateContext。path 可以是文件或目录(目录则取最新)"""
    p = Path(path)
    if p.is_dir():
        candidates = sorted(p.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
        if not candidates:
            raise SessionPersistenceError(f"目录 {p} 下没有 .json session 文件")
        p = candidates[0]
    if not p.exists():
        raise SessionPersistenceError(f"session 文件不存在: {p}")

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SessionPersistenceError(f"session 文件格式错误: {e}") from e

    if data.get("version") != 1:
        raise SessionPersistenceError(f"不支持的 session 版本: {data.get('version')}")

    ctx = TeammateContext.from_dict(data["context"])
    meta = {
        "file_path": str(p),
        "saved_at": data.get("saved_at"),
        "message_count": len(ctx.messages),
    }
    return ctx, meta


def list_sessions(directory: str) -> List[Dict[str, Any]]:
    """列出目录下所有 session 文件"""
    p = Path(directory)
    if not p.exists():
        return []
    out = []
    for f in sorted(p.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            ctx = data.get("context", {})
            saved_at = data.get("saved_at", 0)
            out.append({
                "path": str(f),
                "session_id": ctx.get("session_id", "?"),
                "agent_type": ctx.get("agent_type", "?"),
                "task_preview": (ctx.get("task", "") or "")[:80],
                "message_count": len(ctx.get("messages", [])),
                "token_usage": ctx.get("token_usage", 0),
                "saved_at_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(saved_at)),
            })
        except Exception:
            continue
    return out
