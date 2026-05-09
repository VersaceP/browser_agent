"""Subprocess worker entry — 由 pool.py 通过 subprocess.Popen 启动。

协议(JSON line over stdin/stdout):

主进程 → worker (one line per task):
    {"action":"exec","code":"...","worktree":"...","shared":["..."],"task_id":"abc"}
    {"action":"shutdown"}

worker → 主进程 (one line per result):
    {"task_id":"abc","status":"ok","stdout":"...","stderr":"...","duration_s":1.2}
    {"task_id":"abc","status":"error","exception":"ElementNotFoundError",
     "message":"...","traceback":"...","stdout":"...","stderr":"..."}
    {"event":"ready","pid":12345}                  # 启动时
    {"event":"shutdown_ack"}

设计:
- 启动时立即 install import blocker(在任何业务 import 之前)
- 预 import browser.helpers / json / re / pandas 等常用库 → 加快首次执行
- 每段代码在新建的 namespace 里 exec(防上次执行的状态污染下次)
- exec 期间 stdout/stderr 重定向捕获,出异常时也尽量带回 print 输出
- 收到 'shutdown' 优雅退出
"""
import io
import json
import os
import sys
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout


def _send(obj: dict) -> None:
    """写一行 JSON 到 stdout(原始 stdout,绕开 redirect)"""
    line = json.dumps(obj, ensure_ascii=False, default=str) + "\n"
    _ORIG_STDOUT.write(line)
    _ORIG_STDOUT.flush()


def _recv() -> dict:
    """从 stdin 读一行 JSON。EOF 返回 shutdown 任务"""
    line = sys.stdin.readline()
    if not line:
        return {"action": "shutdown"}
    return json.loads(line)


def _build_namespace(worktree: str, shared: list) -> dict:
    """构造 exec namespace。

    预先 inject:
    - browser_helpers 全部裸函数(goto/click/js/...)
    - 常用标准库(json/re/math/time/datetime/pathlib)
    - 数据处理库(pandas as pd / numpy as np,如果安装了)
    - save_artifact / read_artifact / list_artifacts(基于 path_guard)
    """
    import json as _json
    import math as _math
    import re as _re
    import time as _time
    import datetime as _datetime
    import pathlib as _pathlib

    ns = {
        "__name__": "__sandbox__",
        "__builtins__": __builtins__,
        # 标准库
        "json": _json,
        "math": _math,
        "re": _re,
        "time": _time,
        "datetime": _datetime,
        "pathlib": _pathlib,
        "Path": _pathlib.Path,
    }

    # 数据处理库(可选)
    try:
        import pandas as pd
        ns["pd"] = pd
    except ImportError:
        pass
    try:
        import numpy as np
        ns["np"] = np
    except ImportError:
        pass

    # browser helpers — 全部裸函数 + 异常类
    try:
        import browser.helpers as _bh
        import browser.errors as _be
        # 公开 API:不带下划线的且不是模块的
        for name in dir(_bh):
            if name.startswith("_"):
                continue
            obj = getattr(_bh, name)
            if callable(obj):
                ns[name] = obj
        # 全部异常类
        for name in dir(_be):
            if name.startswith("_"):
                continue
            obj = getattr(_be, name)
            if isinstance(obj, type) and issubclass(obj, Exception):
                ns[name] = obj
    except Exception as e:
        # 不致命 — daemon 还没起也没关系,LLM 调到再失败
        ns["__browser_import_error__"] = str(e)

    # 文件 IO 助手 — 基于 PathGuard
    from .path_guard import PathGuard
    guard = PathGuard(worktree=worktree, shared_dirs=shared)
    ns["__path_guard__"] = guard

    def save_artifact(filename: str, data) -> str:
        """把数据保存到 worktree。data 是 str → 文本写;dict/list → JSON 写"""
        path = guard.resolve(filename, mode="write")
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, (dict, list)):
            path.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        elif isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(str(data), encoding="utf-8")
        return str(path)

    def read_artifact(filename: str):
        """读 worktree / shared 内的文件。.json 自动 parse"""
        path = guard.resolve(filename, mode="read")
        if not path.exists():
            raise FileNotFoundError(f"{filename} → {path}")
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            return _json.loads(text)
        return text

    def list_artifacts(subdir: str = ".") -> list:
        """列 worktree 内文件 (相对路径)"""
        base = guard.resolve(subdir, mode="read")
        if not base.exists():
            return []
        return sorted(
            str(p.relative_to(guard.worktree))
            for p in base.rglob("*") if p.is_file()
        )

    ns["save_artifact"] = save_artifact
    ns["read_artifact"] = read_artifact
    ns["list_artifacts"] = list_artifacts

    # ── update_progress 通道(子进程 buffer,exec 结束统一回传) ──
    # 子进程内可任意时刻调,buffer 在 _execute 完成时一并返回给主进程,
    # 由 code_tool 的 handler flush 到 SessionState.progress
    progress_buffer: list = []

    def update_progress(goal_id: str, increment: int = 0,
                         status=None, note: str = "") -> dict:
        """记录一条进度上报。会在本次 run_browser_python 返回时一起 commit 到主进程"""
        progress_buffer.append({
            "goal_id": str(goal_id),
            "increment": int(increment),
            "status": status,
            "note": str(note)[:200],
        })
        return {"buffered": True, "pending_in_call": len(progress_buffer)}

    ns["update_progress"] = update_progress
    ns["__progress_buffer__"] = progress_buffer

    return ns


def _execute(code: str, namespace: dict) -> dict:
    """exec 一段代码,捕获 stdout/stderr/异常 + progress buffer flush。"""
    stdout = io.StringIO()
    stderr = io.StringIO()
    started = time.time()
    progress_updates = namespace.get("__progress_buffer__", [])

    def _base() -> dict:
        # 复制一份(后续 buffer 还可能被业务清掉/继续 push,这里冻结快照)
        return {
            "stdout": stdout.getvalue(),
            "stderr": stderr.getvalue(),
            "duration_s": round(time.time() - started, 3),
            "progress_updates": list(progress_updates),
        }

    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exec(compile(code, "<sandbox>", "exec"), namespace)
        return {"status": "ok", **_base()}
    except SystemExit as e:
        return {
            "status": "error", "exception": "SystemExit",
            "message": str(e), "traceback": "",
            **_base(),
        }
    except BaseException as e:
        return {
            "status": "error", "exception": type(e).__name__,
            "message": str(e), "traceback": traceback.format_exc(),
            **_base(),
        }


def main():
    # 读启动配置 — 第一行 stdin 是初始化 payload
    init_line = sys.stdin.readline()
    if not init_line:
        return
    init = json.loads(init_line)

    # 安装 import blocker 必须在 browser.helpers import 之前
    # (因为 helpers 内部可能间接 import urllib 等)
    from .blocker import install as install_blocker
    install_blocker(init.get("blocked_imports", []))

    # 把 v6 项目根加入 sys.path,这样 worker 能 import browser.helpers
    project_root = init.get("project_root")
    if project_root and project_root not in sys.path:
        sys.path.insert(0, project_root)

    # 加载 skill registry — worker 内的 helpers 也要能 list_skills / read_skill
    try:
        from browser.skills import auto_load
        auto_load()
    except Exception:
        pass  # skill 加载失败不致命,helpers 仍能用

    _send({"event": "ready", "pid": os.getpid()})

    while True:
        try:
            req = _recv()
        except (json.JSONDecodeError, KeyboardInterrupt):
            break

        action = req.get("action")
        if action == "shutdown":
            _send({"event": "shutdown_ack"})
            break

        if action == "exec":
            task_id = req.get("task_id", "")
            code = req.get("code", "")
            worktree = req.get("worktree") or os.getcwd()
            shared = req.get("shared", [])
            try:
                ns = _build_namespace(worktree, shared)
                result = _execute(code, ns)
            except Exception as e:
                result = {
                    "status": "error",
                    "exception": type(e).__name__,
                    "message": f"namespace build failed: {e}",
                    "traceback": traceback.format_exc(),
                    "stdout": "",
                    "stderr": "",
                    "duration_s": 0,
                }
            result["task_id"] = task_id
            _send(result)
        else:
            _send({"event": "error", "message": f"unknown action: {action}"})


# stdout 捕获 — 在 import 前先存原始 stdout
_ORIG_STDOUT = sys.stdout

if __name__ == "__main__":
    # Windows utf-8
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
            sys.stdin.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
        # _ORIG_STDOUT 在 reconfigure 后重新指向同一对象
        _ORIG_STDOUT = sys.stdout
    main()
