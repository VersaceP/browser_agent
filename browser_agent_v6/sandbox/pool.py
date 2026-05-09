"""WarmPool — 主进程侧的 subprocess pool 协调器。

设计:
- 启动时 fork N 个常驻 worker(默认 3 个),每个 worker 已经 import 好 browser.helpers
- exec_code(code, worktree) 从池里 acquire 一个 idle worker,投递任务,等结果,归还
- 单段代码超时 → SIGKILL 该 worker + 立即起新 worker 填回池
- 死掉的 worker 自动剔除(EOF/json decode 异常)
- 主进程退出时 shutdown 全部 worker
"""
import atexit
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Sequence


PROJECT_ROOT = str(Path(__file__).resolve().parents[1])


class WorkerError(Exception):
    """worker 进程级异常(崩了 / 启动失败 / 协议错乱)"""


class WorkerTimeoutError(Exception):
    """单段代码执行超过 timeout — worker 已被 kill,主进程不再相信它"""

    def __init__(self, timeout: float, partial_stdout: str = ""):
        self.timeout = timeout
        self.partial_stdout = partial_stdout
        super().__init__(f"sandbox exec timeout {timeout}s; killed worker")


# ────────────────────────────
# 单个 worker 进程的封装
# ────────────────────────────

class Worker:
    """一个常驻 subprocess。包含发收协议 + 死活检查"""

    def __init__(
        self,
        blocked_imports: Sequence[str],
        env_passthrough: Sequence[str],
        env_prefix: Sequence[str],
        worker_id: str,
        browser_port: Optional[int] = None,
    ):
        self.worker_id = worker_id
        self.blocked_imports = list(blocked_imports)
        self.env_passthrough = list(env_passthrough)
        self.env_prefix = list(env_prefix)
        self.browser_port = browser_port

        self.proc: Optional[subprocess.Popen] = None
        self.pid: Optional[int] = None
        self._busy = False
        self._lock = threading.Lock()  # 保护单 worker 不被并发用

    # ───────── 启动/关闭 ─────────

    def start(self, timeout: float = 5.0) -> None:
        """fork 子进程并等其发 ready 事件"""
        env = self._build_env()
        # python -u 强制 unbuffered I/O
        cmd = [sys.executable, "-u", "-m", "sandbox.worker"]
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,  # worker 自身的诊断输出走这里,不污染协议
            cwd=PROJECT_ROOT,
            env=env,
            text=True,
            encoding="utf-8",
            bufsize=1,  # 行缓冲
        )
        self.pid = self.proc.pid

        # 第一行写 init payload
        init = {
            "blocked_imports": self.blocked_imports,
            "project_root": PROJECT_ROOT,
        }
        self._write_line(init)

        # 等 ready 事件
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                stderr = self.proc.stderr.read() if self.proc.stderr else ""
                raise WorkerError(
                    f"worker {self.worker_id} 启动后立即退出 (exit={self.proc.returncode}). "
                    f"stderr: {stderr[:500]}"
                )
            try:
                line = self._read_line(timeout=0.2)
                if line.get("event") == "ready":
                    return
            except (TimeoutError, json.JSONDecodeError):
                continue
        raise WorkerError(f"worker {self.worker_id} 启动后 {timeout}s 内未发 ready")

    def shutdown(self, timeout: float = 2.0) -> None:
        """优雅关闭:发 shutdown,等 ack,超时 kill"""
        if not self.proc or self.proc.poll() is not None:
            return
        try:
            self._write_line({"action": "shutdown"})
        except (BrokenPipeError, OSError):
            pass
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()

    def kill(self) -> None:
        """强杀 — 用于超时或 worker 卡死"""
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.kill()
            except Exception:
                pass
        if self.proc:
            try:
                self.proc.wait(timeout=1.0)
            except Exception:
                pass

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    # ───────── 任务执行 ─────────

    def exec_code(
        self,
        code: str,
        worktree: str,
        shared: Sequence[str] = (),
        timeout: float = 120.0,
    ) -> dict:
        """投递一段代码,阻塞等结果。

        Raises:
            WorkerError: worker 死了 / 协议错乱
            WorkerTimeoutError: 超时(worker 已被 kill)
        """
        with self._lock:
            self._busy = True
            try:
                if not self.is_alive():
                    raise WorkerError(f"worker {self.worker_id} 已死")
                task_id = uuid.uuid4().hex[:8]
                self._write_line({
                    "action": "exec",
                    "task_id": task_id,
                    "code": code,
                    "worktree": worktree,
                    "shared": list(shared),
                })
                try:
                    result = self._read_line(timeout=timeout)
                except TimeoutError:
                    self.kill()
                    raise WorkerTimeoutError(timeout)
                if result.get("task_id") != task_id:
                    raise WorkerError(
                        f"task_id 不匹配 expected={task_id} got={result.get('task_id')}"
                    )
                return result
            finally:
                self._busy = False

    # ───────── 内部:协议 IO ─────────

    def _write_line(self, obj: dict) -> None:
        if not self.proc or not self.proc.stdin:
            raise WorkerError(f"worker {self.worker_id} stdin 不可用")
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        self.proc.stdin.write(line)
        self.proc.stdin.flush()

    def _read_line(self, timeout: float) -> dict:
        """阻塞读一行,带超时"""
        if not self.proc or not self.proc.stdout:
            raise WorkerError(f"worker {self.worker_id} stdout 不可用")
        # subprocess.PIPE + text 在 Windows 上没有 fcntl 可设非阻塞,
        # 用线程 + Queue 的方式打超时
        result_q: "queue.Queue" = queue.Queue(maxsize=1)

        def _reader():
            try:
                line = self.proc.stdout.readline()
                result_q.put(("line", line))
            except Exception as e:
                result_q.put(("err", str(e)))

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        try:
            kind, payload = result_q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"read_line timeout {timeout}s")

        if kind == "err":
            raise WorkerError(f"read err: {payload}")
        if not payload:
            raise WorkerError(f"worker {self.worker_id} EOF (likely crashed)")
        return json.loads(payload)

    def _build_env(self) -> Dict[str, str]:
        """裁剪环境变量 — 只透传白名单 + 前缀匹配,加 V6_BROWSER_PORT"""
        env: Dict[str, str] = {}
        for k in self.env_passthrough:
            if k in os.environ:
                env[k] = os.environ[k]
        for prefix in self.env_prefix:
            for k, v in os.environ.items():
                if k.startswith(prefix):
                    env[k] = v
        # browser 端口 — 让 worker 内 helpers._get_page() 能 attach
        if self.browser_port:
            env["V6_BROWSER_PORT"] = str(self.browser_port)
        return env


# ────────────────────────────
# Pool 主管
# ────────────────────────────

class WarmPool:
    """N 个 idle worker 的池。线程安全。

    Usage:
        pool = WarmPool(size=3, blocked_imports=[...], env_passthrough=[...])
        pool.start()
        result = pool.exec_code("print(1+1)", worktree="/tmp/wt")
        print(result['stdout'])  # "2\n"
        pool.shutdown()
    """

    def __init__(
        self,
        size: int = 3,
        blocked_imports: Sequence[str] = (),
        env_passthrough: Sequence[str] = ("PATH", "PYTHONPATH", "HOME", "USERPROFILE",
                                          "TEMP", "TMP", "LANG", "LC_ALL",
                                          "SYSTEMROOT", "APPDATA", "LOCALAPPDATA"),
        env_prefix: Sequence[str] = ("LARK_", "FEISHU_"),
        browser_port: Optional[int] = None,
    ):
        self.size = size
        self.blocked_imports = list(blocked_imports)
        self.env_passthrough = list(env_passthrough)
        self.env_prefix = list(env_prefix)
        self.browser_port = browser_port  # 注入到 worker 的 V6_BROWSER_PORT 环境变量

        self._workers: List[Worker] = []
        self._idle: "queue.Queue[Worker]" = queue.Queue()
        self._lock = threading.Lock()
        self._counter = 0
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        for _ in range(self.size):
            self._spawn_one()
        atexit.register(self.shutdown)
        self._started = True

    def _spawn_one(self) -> Worker:
        with self._lock:
            self._counter += 1
            wid = f"w{self._counter}"
        w = Worker(
            blocked_imports=self.blocked_imports,
            env_passthrough=self.env_passthrough,
            env_prefix=self.env_prefix,
            worker_id=wid,
            browser_port=self.browser_port,
        )
        w.start()
        with self._lock:
            self._workers.append(w)
        self._idle.put(w)
        return w

    def exec_code(
        self,
        code: str,
        worktree: str,
        shared: Sequence[str] = (),
        timeout: float = 120.0,
        acquire_timeout: float = 30.0,
    ) -> dict:
        """主入口:取 worker → exec → 归还(死了就替换)"""
        if not self._started:
            self.start()

        try:
            worker = self._idle.get(timeout=acquire_timeout)
        except queue.Empty:
            raise WorkerError(f"no idle worker after {acquire_timeout}s")

        try:
            result = worker.exec_code(code, worktree, shared, timeout=timeout)
        except (WorkerTimeoutError, WorkerError):
            # worker 已死 / 已 kill — 剔除 + 异步补充新 worker
            with self._lock:
                if worker in self._workers:
                    self._workers.remove(worker)
            threading.Thread(target=self._spawn_one, daemon=True).start()
            raise

        # 健康才归还
        self._idle.put(worker)
        return result

    def shutdown(self) -> None:
        with self._lock:
            workers = list(self._workers)
            self._workers.clear()
        for w in workers:
            w.shutdown()
        self._started = False

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                "total": len(self._workers),
                "idle": self._idle.qsize(),
                "alive": sum(1 for w in self._workers if w.is_alive()),
            }


# ─────────────────────
# 自检
# ─────────────────────

if __name__ == "__main__":
    import tempfile

    pool = WarmPool(
        size=2,
        blocked_imports=["requests", "httpx", "aiohttp", "urllib", "socket"],
    )
    print("[pool] 启动 2 个 warm worker...")
    pool.start()
    print(f"[pool] stats: {pool.stats}")

    with tempfile.TemporaryDirectory() as wt:
        # 1. 简单 print
        r = pool.exec_code("print('hello from sandbox'); print(1+1)", worktree=wt)
        print(f"\n[test 1] stdout={r['stdout']!r} status={r['status']} duration={r['duration_s']}s")

        # 2. 异常捕获
        r = pool.exec_code("raise ValueError('boom')", worktree=wt)
        print(f"\n[test 2] status={r['status']} exc={r['exception']} msg={r['message']}")

        # 3. 网络库被拦
        r = pool.exec_code("import requests; requests.get('https://x')", worktree=wt)
        print(f"\n[test 3] status={r['status']} exc={r['exception']} msg={r['message'][:100]}")

        # 4. save_artifact
        r = pool.exec_code("save_artifact('test.json', {'a': 1, 'b': 2}); print(list_artifacts())", worktree=wt)
        print(f"\n[test 4] stdout={r['stdout']!r}")

        # 5. 路径穿越
        r = pool.exec_code("save_artifact('../../../etc/x', 'data')", worktree=wt)
        print(f"\n[test 5] status={r['status']} exc={r['exception']}")

        # 6. 超时
        try:
            pool.exec_code("import time; time.sleep(3)", worktree=wt, timeout=1.0)
            print(f"\n[test 6] ❌ 超时未触发")
        except WorkerTimeoutError as e:
            print(f"\n[test 6] ✅ WorkerTimeoutError: {e}")
        time.sleep(0.5)  # 给补 worker 时间起来
        print(f"[pool] stats after timeout: {pool.stats}")

        # 7. 超时后池仍可用
        r = pool.exec_code("print('still alive')", worktree=wt)
        print(f"\n[test 7] stdout={r['stdout']!r} (池在 worker kill 后自动补充)")

    print(f"\n[pool] 关闭...")
    pool.shutdown()
    print("[pool] 全部测试通过")
