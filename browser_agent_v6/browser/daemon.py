"""Chrome 长驻 daemon + DrissionPage 接管。

设计:
- Chrome 以 --remote-debugging-port=9222 长驻运行(主进程拉起的 subprocess)
- DrissionPage ChromiumPage 以 set_local_port 接管,保留反爬伪装能力
- 启动前先 probe 端口,如果已有 Chrome 在听就直接 attach(避免重复拉起)
- 单例模式:主进程生命周期内只保持一个 daemon 实例
"""
import atexit
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional


class BrowserDaemonError(Exception):
    """Browser daemon 生命周期错误"""


def _probe_port(port: int, timeout: float = 0.5) -> bool:
    """检测本地端口是否在听"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except (OSError, ConnectionRefusedError):
        return False


def _probe_devtools(port: int, timeout: float = 1.0) -> Optional[dict]:
    """HTTP probe DevTools /json/version,返回 Browser/WebKit-Version 等信息。不可达返回 None"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _find_chrome_binary() -> Optional[str]:
    """查找 Chrome/Chromium/Edge 可执行文件路径"""
    # 1. PATH 中查
    for name in ("chrome", "google-chrome", "chromium", "msedge"):
        path = shutil.which(name)
        if path:
            return path

    # 2. 系统默认安装位置
    system = platform.system()
    if system == "Windows":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
    elif system == "Darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    else:
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
        ]

    for p in candidates:
        if p and os.path.exists(p):
            return p
    return None


class BrowserDaemon:
    """Chrome 长驻 daemon。

    使用:
        daemon = BrowserDaemon(port=9222)
        daemon.start()                # 调 chrome --remote-debugging-port 拉起 / 或重用已有实例
        page = daemon.get_page()      # 拿 DrissionPage ChromiumPage 实例
        daemon.stop()                 # 仅在是本 daemon 拉起的情况下 kill。外部 attach 的不动
    """

    def __init__(
        self,
        port: int = 9222,
        user_data_dir: Optional[str] = None,
        binary_path: Optional[str] = None,
        headless: bool = False,
    ):
        self.port = port
        self.user_data_dir = user_data_dir
        self.binary_path = binary_path or _find_chrome_binary()
        self.headless = headless

        self._chrome_proc: Optional[subprocess.Popen] = None
        self._owns_chrome = False  # True 表示 chrome 是本 daemon 拉起的,stop 时负责 kill
        self._page = None  # DrissionPage ChromiumPage 实例(延迟导入)

    # ──────────────────────────
    # 外部 API
    # ──────────────────────────

    def start(self, wait_seconds: float = 10.0) -> dict:
        """启动或接管 Chrome。返回 DevTools /json/version 信息。

        优先策略:端口已听 → 直接重用;未听 → 拉起新实例。
        """
        # 1. probe 端口
        info = _probe_devtools(self.port, timeout=1.0)
        if info:
            self._owns_chrome = False
            print(f"[BrowserDaemon] ✅ 复用已有 Chrome 实例 @ :{self.port} — {info.get('Browser', '?')}")
            return info

        # 2. 拉起新实例
        if not self.binary_path:
            raise BrowserDaemonError(
                "未找到 Chrome/Chromium/Edge 可执行文件。\n"
                "请在 config.json -> browser.binary_path 手动指定路径,"
                "或设置 PATH 包含 chrome.exe / chromium。"
            )

        if not os.path.exists(self.binary_path):
            raise BrowserDaemonError(f"指定的浏览器路径不存在: {self.binary_path}")

        # 默认 user_data_dir 用项目下的 chrome_profile/(保证不与用户日常浏览器冲突)
        udd = self.user_data_dir or str(Path(__file__).resolve().parents[1] / "chrome_profile")
        Path(udd).mkdir(parents=True, exist_ok=True)

        args = [
            self.binary_path,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={udd}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",  # 反检测
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-popup-blocking",
        ]
        if self.headless:
            args.append("--headless=new")

        print(f"[BrowserDaemon] \U0001f680 启动 Chrome: {self.binary_path} (port={self.port}, udd={udd})")

        # detached 子进程,主进程退出不会顶死它(但我们的 atexit 会主动 cleanup)
        creationflags = 0
        if platform.system() == "Windows":
            # CREATE_NEW_PROCESS_GROUP = 0x00000200,让 ctrl-c 不跟随子进程
            creationflags = 0x00000200
        self._chrome_proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        self._owns_chrome = True
        atexit.register(self._cleanup_chrome)

        # 3. 等 DevTools 可访问
        deadline = time.time() + wait_seconds
        info = None
        while time.time() < deadline:
            info = _probe_devtools(self.port, timeout=1.0)
            if info:
                print(f"[BrowserDaemon] ✅ Chrome 就绪 — {info.get('Browser', '?')}")
                return info
            if self._chrome_proc.poll() is not None:
                raise BrowserDaemonError(
                    f"Chrome 启动后立即退出(exit code={self._chrome_proc.returncode})。"
                    f"可能原因:端口被占 / user_data_dir 锁住 / binary 损坏"
                )
            time.sleep(0.3)

        # 超时仍未起来 — kill 并报错
        self._cleanup_chrome()
        raise BrowserDaemonError(
            f"Chrome 启动 {wait_seconds}s 内 DevTools 未响应 @ :{self.port}"
        )

    def get_page(self):
        """返回 DrissionPage ChromiumPage 实例(延迟创建,单例)"""
        if self._page is not None:
            return self._page

        try:
            from DrissionPage import ChromiumPage, ChromiumOptions
        except ImportError as e:
            raise BrowserDaemonError(
                "DrissionPage 未安装。请 pip install DrissionPage>=4.0.0"
            ) from e

        co = ChromiumOptions()
        co.set_local_port(self.port)
        # 关闭 DrissionPage 自带的启动逻辑 — 我们已经拉起 chrome 了,它只需 attach
        co.set_address(f"127.0.0.1:{self.port}")

        self._page = ChromiumPage(co)
        print(f"[BrowserDaemon] \U0001f4f1 DrissionPage ChromiumPage 已 attach @ :{self.port}")
        return self._page

    def is_alive(self) -> bool:
        """健康检查 — 端口活 + 已 attach 的 page 句柄真能动"""
        if _probe_devtools(self.port, timeout=0.5) is None:
            return False
        if self._page is None:
            return True
        # 已 attach,真探一下 page 是否还能用
        try:
            _ = self._page.url
            return True
        except Exception:
            return False

    def restart(self, wait_seconds: float = 10.0) -> dict:
        """强制重启 Chrome — 用于 PageDisconnectedError 自愈。

        kill 当前 Chrome → 清 page handle → 重新 start()。
        端口保持不变,所有持有 ChromiumPage 的进程(主 + workers)需要在
        下次操作时调 helpers._try_recover() 自己重新 attach。
        """
        print("[BrowserDaemon] 🔄 restart triggered (Chrome 失联 / 自愈)")
        # 释放 page 引用(不强制 quit,因为 chrome 可能已死)
        self._page = None
        # kill 旧进程(只在我们拥有时;外部 attach 的不动)
        if self._owns_chrome:
            self._cleanup_chrome()
        # 短暂 sleep 让端口释放
        time.sleep(0.5)
        # 重置 owns 标记,start() 会重新 probe + 拉起
        self._owns_chrome = False
        return self.start(wait_seconds=wait_seconds)

    def stop(self) -> None:
        """退出。仅在本 daemon 拉起的情况下 kill chrome。外部 attach 的不动"""
        if self._page is not None:
            try:
                self._page.quit() if hasattr(self._page, "quit") else None
            except Exception:
                pass
            self._page = None

        if self._owns_chrome:
            self._cleanup_chrome()

    # ─────────────────────────
    # 内部
    # ────────────────────────

    def _cleanup_chrome(self) -> None:
        """kill 本 daemon 拉起的 chrome 进程"""
        if not self._chrome_proc or self._chrome_proc.poll() is not None:
            return
        try:
            print(f"[BrowserDaemon] \U0001f6d1 关闭 Chrome (pid={self._chrome_proc.pid})")
            self._chrome_proc.terminate()
            try:
                self._chrome_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._chrome_proc.kill()
        except Exception as e:
            print(f"[BrowserDaemon] ⚠️ 关闭 Chrome 失败: {e}", file=sys.stderr)
        finally:
            self._chrome_proc = None


# ──────────────────────────
# 进程全局单例
# ──────────────────────────

_DAEMON: Optional[BrowserDaemon] = None


def get_daemon(
    port: int = 9222,
    user_data_dir: Optional[str] = None,
    binary_path: Optional[str] = None,
    headless: bool = False,
) -> BrowserDaemon:
    """进程全局单例。重复调用返回同一个 daemon。

    该函数只创建实例不会自动 start(),调用方需手动 daemon.start()。
    """
    global _DAEMON
    if _DAEMON is None:
        _DAEMON = BrowserDaemon(
            port=port,
            user_data_dir=user_data_dir,
            binary_path=binary_path,
            headless=headless,
        )
    return _DAEMON


# ──────────────────────────
# 独立启动(调试用):python -m browser.daemon
# ──────────────────────────

if __name__ == "__main__":
    daemon = get_daemon()
    info = daemon.start()
    page = daemon.get_page()
    print(f"\n[验证] 当前页面: {page.url}")
    print(f"[验证] 打开一个测试页...")
    page.get("https://example.com")
    print(f"[验证] 到达: {page.title} — {page.url}")
    print("\n按 Ctrl-C 退出(主进程退出后 Chrome 也会被清理)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n退出")
        daemon.stop()
