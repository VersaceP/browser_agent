"""
session_persistence.py — Session 状态持久化

将 TeammateContext 序列化为 JSON 文件，支持从磁盘恢复继续任务。
不依赖任何外部数据库，仅使用文件系统。

使用方式:
    from core.session_persistence import save_session, load_session

    # 保存
    save_session(context, "my_session.json")

    # 恢复
    ctx = load_session("my_session.json")
"""

import json
import os
import time
from typing import Any, Dict
from core.teammate_context import TeammateContext


SESSION_SCHEMA_VERSION = "1.0"
# 固定文件名仅用于 list/load 扫描；实际保存为带时间戳的副本
SESSION_FILE_NAME = "session_state.json"
SESSION_FILE_PREFIX = "session_state_"  # 保存时自动加时间戳后缀

def _session_save_path(target_dir: str) -> str:
    ts = time.strftime("%Y-%m-%d_%H-%M-%S")
    return os.path.join(target_dir, f"{SESSION_FILE_PREFIX}{ts}.json")

class SessionPersistenceError(Exception):
    """Session 持久化相关错误"""
    pass

def save_session(ctx: TeammateContext, path: str) -> Dict[str, Any]:
    """
    将 TeammateContext 序列化为带时间戳的 JSON 文件（自动保留历史）。

    :param ctx: TeammateContext 实例
    :param path: 保存路径（文件或目录均可）
                  - 若为文件：直接写入该文件（不加时间戳，适合覆盖指定文件）
                  - 若为目录：自动写入 <dir>/session_state_<timestamp>.json
    :return: 保存的元信息字典（包含 saved_at, file_path 等）
    :raises SessionPersistenceError: 序列化或写入失败时抛出
    """
    # 确定写入路径
    if os.path.exists(path):
        if os.path.isdir(path):
            save_path = _session_save_path(path)
        else:
            save_path = path  # 直接写文件，不加时间戳
    else:
        basename = os.path.basename(path)
        if "." in basename and not basename.endswith("."):
            save_path = path  # 带扩展名，视为文件路径
        else:
            os.makedirs(path, exist_ok=True)
            save_path = _session_save_path(path)  # 目录，自动加时间戳

    # 构造完整状态字典
    state: Dict[str, Any] = {
        "schema_version": SESSION_SCHEMA_VERSION,
        "saved_at": time.time(),
        "was_plan_approved": False,
        "session": {
            "session_id": ctx.session_id,
            "agent_type": ctx.agent_type,
            "task": ctx.task,
            "session_messages": ctx.session_messages,
            "env_vars": ctx.env_vars,
            "token_usage": ctx.token_usage,
            "max_tokens": ctx.max_tokens,
            "worktree_path": ctx.worktree_path,
            "created_at": ctx.created_at,
            "metadata": ctx.metadata,
        },
    }

    # 动态填入 plan approval 状态（从 lead_tools 读取，需避免循环导入）
    try:
        from toolkits.lead_tools import get_approved_plan_sessions
        state["was_plan_approved"] = ctx.session_id in get_approved_plan_sessions()
    except Exception:
        pass

    try:
        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, default=str)
    except (IOError, OSError) as e:
        raise SessionPersistenceError(f"写入 session 文件失败: {e}") from e
    except (TypeError, ValueError) as e:
        raise SessionPersistenceError(f"序列化 session 失败（数据类型不支持）: {e}") from e

    return {
        "saved_at": state["saved_at"],
        "file_path": os.path.abspath(save_path),
        "message_count": len(ctx.session_messages),
        "token_usage": ctx.token_usage,
    }


def load_session(path: str, *, validate_worktree: bool = True) -> tuple[TeammateContext, bool]:
    """
    从 JSON 文件恢复 TeammateContext。

    :param path: session 文件路径，或包含 session 文件的目录路径
                 - 若为目录：自动选择时间戳最新的 session 文件
    :param validate_worktree: 是否校验 worktree_path 目录是否存在
    :return: 重建的 TeammateContext 实例
    :raises SessionPersistenceError: 文件不存在、版本不兼容或数据损坏时抛出
    """
    # 解析路径
    if os.path.isdir(path):
        # 目录模式：找到时间戳最新的 session 文件
        candidates = []
        for fname in os.listdir(path):
            if fname == SESSION_FILE_NAME or fname.startswith(SESSION_FILE_PREFIX):
                candidates.append(os.path.join(path, fname))
        if not candidates:
            raise SessionPersistenceError(f"No session file found in: {path}")
        # 时间戳在文件名中，按文件名排序即可得到最新
        candidates.sort(key=lambda f: os.path.basename(f), reverse=True)
        file_path = candidates[0]
    else:
        file_path = path

    if not os.path.exists(file_path):
        raise SessionPersistenceError(f"Session file not found: {file_path}")

    # 读取并解析 JSON
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            state: Dict[str, Any] = json.load(f)
    except json.JSONDecodeError as e:
        raise SessionPersistenceError(f"Invalid JSON (file may be corrupted): {e}") from e
    except IOError as e:
        raise SessionPersistenceError(f"Failed to read session file: {e}") from e

    # Schema 版本校验
    schema_version = state.get("schema_version", "unknown")
    if schema_version != SESSION_SCHEMA_VERSION:
        raise SessionPersistenceError(
            f"Incompatible schema version: found {schema_version}, expected {SESSION_SCHEMA_VERSION}."
        )

    session_data = state.get("session", {})
    if not session_data:
        raise SessionPersistenceError("Session file is missing 'session' field.")

    # 重建 TeammateContext
    ctx = TeammateContext(
        agent_type=session_data.get("agent_type", "unknown"),
        session_id=session_data.get("session_id", ""),
        task=session_data.get("task", ""),
        session_messages=session_data.get("session_messages", []),
        worktree_path=session_data.get("worktree_path", ""),
        env_vars=session_data.get("env_vars", {}),
        token_usage=session_data.get("token_usage", 0),
        max_tokens=session_data.get("max_tokens", 200_000),
        created_at=session_data.get("created_at", time.time()),
        metadata=session_data.get("metadata", {}),
        progress_board=session_data.get("progress_board", {}),
    )

    # WorkTree 存在性校验
    if validate_worktree and ctx.worktree_path:
        if not os.path.exists(ctx.worktree_path):
            raise SessionPersistenceError(
                f"WorkTree directory does not exist: {ctx.worktree_path}\n"
                "Hint: If the worktree was deleted or moved, the session cannot continue.\n"
                "Pass validate_worktree=False to skip this check (Agent may fail to execute)."
            )

    saved_at = state.get("saved_at", 0)
    was_plan_approved = state.get("was_plan_approved", False)
    saved_at_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(saved_at)) if saved_at else "未知"

    print(f"[Session] [OK] session restored: {ctx.session_id}")
    print(f"         Agent: {ctx.agent_type} | Task: {ctx.task[:60]}{'...' if len(ctx.task) > 60 else ''}")
    print(f"         Messages: {len(ctx.session_messages)} | Token: {ctx.token_usage}")
    print(f"         Saved at: {saved_at_str}")
    print(f"         WorkTree: {ctx.worktree_path}")

    return ctx, was_plan_approved


def list_sessions(base_dir: str) -> list:
    """
    列出 base_dir 下所有有效的 session 文件。

    :param base_dir: 要扫描的根目录
    :return: session 文件信息列表，每项包含 path, session_id, agent_type, task, saved_at
    """
    sessions = []
    if not os.path.exists(base_dir):
        return sessions

    for root, dirs, files in os.walk(base_dir):
        # 兼容旧文件 session_state.json 和新文件 session_state_<timestamp>.json
        for fname in files:
            if fname == SESSION_FILE_NAME or fname.startswith(SESSION_FILE_PREFIX):
                file_path = os.path.join(root, fname)
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        state = json.load(f)
                    session_data = state.get("session", {})
                    saved_at = state.get("saved_at", 0)
                    sessions.append({
                        "path": file_path,
                        "session_id": session_data.get("session_id", ""),
                        "agent_type": session_data.get("agent_type", ""),
                        "task": session_data.get("task", ""),
                        "saved_at": saved_at,
                        "saved_at_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(saved_at)) if saved_at else "未知",
                        "message_count": len(session_data.get("session_messages", [])),
                    })
                except Exception:
                    # 跳过损坏的 session 文件
                    continue

    # 按保存时间倒序排列
    sessions.sort(key=lambda s: s["saved_at"], reverse=True)
    return sessions


def delete_session(path: str) -> bool:
    """
    删除指定路径的 session 文件。

    :param path: session 文件或目录
    :return: 是否删除成功
    """
    if os.path.isdir(path):
        # 删除目录下所有 session 文件（兼容新旧命名）
        deleted = False
        for fname in os.listdir(path):
            if fname == SESSION_FILE_NAME or fname.startswith(SESSION_FILE_PREFIX):
                try:
                    os.remove(os.path.join(path, fname))
                    deleted = True
                except OSError:
                    pass
        return deleted
    else:
        try:
            if os.path.exists(path):
                os.remove(path)
                return True
        except OSError:
            pass
        return False
