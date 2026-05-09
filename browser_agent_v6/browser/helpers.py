"""browser_helpers — 同步裸函数,直接被 Tools 层和 sandbox 子进程共用。

设计:
- 所有函数同步阻塞(sandbox 子进程没有 asyncio loop)
- 失败抛 errors.py 里的语义化异常,而非裸 RuntimeError
- 通过 daemon.get_page() 拿 ChromiumPage,共享反爬环境
- js() 默认 50 KB 软上限 — 防止 LLM 一句 `return document.body.innerHTML` 污染上下文

LLM 在 run_browser_python 里典型用法:
    from browser.helpers import goto, wait_for_element, js, save_artifact

    goto("https://example.com")
    wait_for_element("h1", timeout=5)
    title = js("return document.title")
    print(title)

    # 批量
    rows = js('''return [...document.querySelectorAll('.row')]
                .map(e => ({name: e.dataset.name, price: e.querySelector('.price')?.textContent}))''')
    save_artifact("rows.json", rows)
"""
import json
import re
import time
from pathlib import Path
from typing import Any, Optional, Union

from .errors import (
    BrowserError,
    DaemonNotReadyError,
    ElementClickInterceptedError,
    ElementNotFoundError,
    ElementNotVisibleError,
    HumanInterventionRequired,
    JSExecutionError,
    JSResultTooLargeError,
    NavigationTimeoutError,
    PageDisconnectedError,
    PendingDialogError,
    TabNotFoundError,
)

# Vision adapter — re-exported so the sandbox subprocess (which copies all callables
# out of browser.helpers into its exec namespace) can call analyze_image() and
# vision_skeleton_scan() inside run_browser_python too.
from .vision import (
    analyze_image,
    vision_skeleton_scan,
    VisionNotConfiguredError,
)


# ──────────────────────────
# 全局配置(可被外部覆盖)
# ──────────────────────────

DEFAULT_JS_RESULT_LIMIT = 50_000        # bytes — js() 返回值软上限
DEFAULT_NAV_TIMEOUT = 30                # seconds — goto 默认
DEFAULT_ELEMENT_TIMEOUT = 10            # seconds — wait_for_element 默认
DEFAULT_CLICK_TIMEOUT = 5               # seconds


# ──────────────────────────
# Page 获取(独立于 daemon 模块 — worker 也能用)
# ──────────────────────────

# 进程局部缓存:每个进程(主 / worker)各持一个 ChromiumPage 单例
_ACTIVE_PAGE = None
_ACTIVE_PORT = None


def set_active_port(port: int) -> None:
    """主进程启动 daemon 后调用此函数注入端口;worker 启动时从环境变量自动读。

    重复调用会清掉旧 page 缓存,下次 _get_page() 重新 attach。
    """
    global _ACTIVE_PORT, _ACTIVE_PAGE
    _ACTIVE_PORT = int(port)
    _ACTIVE_PAGE = None


# ──────────────────────────────────
# 浏览器租约 hook(为未来 DaemonPool 多实例预留扩展点)
# ──────────────────────────────────
# Phase 1(当前):单 Chrome,acquire/release 返回唯一端口、no-op
# Phase 2(未来):DaemonPool 把 acquire 实现成真正的 lease 分配,
#                worker 用完 release 归还,实现真并行多浏览器。
#
# helpers / pool / spawner 层调用时,只看这两个函数的语义,不感知底层实现。

def acquire_browser(session_id: str = "default", timeout: float = 30.0) -> int:
    """申请一个浏览器实例的端口(未来:lease 一个;现在:返回唯一的)。

    Args:
        session_id: 用于将来 DaemonPool 按 session 黏性分配
        timeout: 等待空闲实例的最大时间(将来生效)
    Returns:
        端口号
    """
    if _ACTIVE_PORT is None:
        # 兜底从环境变量读
        import os
        env_port = os.environ.get("V6_BROWSER_PORT")
        if env_port:
            return int(env_port)
        return 9222
    return _ACTIVE_PORT


def release_browser(port: int) -> None:
    """归还浏览器实例(未来:归还到 DaemonPool;现在:no-op)"""
    pass


def _get_page():
    """返回 ChromiumPage 实例(进程内单例)。

    端口来源优先级:
    1. set_active_port() 注入的(主进程路径)
    2. 环境变量 V6_BROWSER_PORT(worker 启动时由 pool 注入)
    3. 兜底 9222(单进程脚本调试)

    helpers 不再依赖 daemon.py — worker subprocess 独立 attach。
    """
    global _ACTIVE_PAGE, _ACTIVE_PORT
    if _ACTIVE_PAGE is not None:
        return _ACTIVE_PAGE

    # 解析端口
    if _ACTIVE_PORT is None:
        import os
        env_port = os.environ.get("V6_BROWSER_PORT")
        _ACTIVE_PORT = int(env_port) if env_port else 9222

    try:
        from DrissionPage import ChromiumPage, ChromiumOptions
    except ImportError as e:
        raise DaemonNotReadyError(
            "DrissionPage 未安装。pip install DrissionPage>=4.0.0"
        ) from e

    co = ChromiumOptions()
    co.set_address(f"127.0.0.1:{_ACTIVE_PORT}")
    try:
        _ACTIVE_PAGE = ChromiumPage(co)
    except Exception as e:
        raise DaemonNotReadyError(
            f"attach DrissionPage 到 :{_ACTIVE_PORT} 失败: {e}。"
            f"主进程是否已启动 Browser daemon?"
        ) from e
    return _ACTIVE_PAGE


# ──────────────────────────────────
# 自愈:进程内重新 attach(对付 Chrome 崩 / page 失联)
# ──────────────────────────────────

# 已知的"page/connection 失联"错误关键词(全小写匹配)
_DISCONNECT_KEYWORDS = (
    "disconnect", "页面的连接已断开", "connection",
    "tab is closed", "page is closed", "websocket is closed",
    "no such target", "browser has crashed", "remote end closed",
)


def _is_disconnect_error(exc: BaseException) -> bool:
    """判断是否是浏览器/页面失联类错误(用于触发自愈)"""
    if isinstance(exc, PageDisconnectedError):
        return True
    msg = str(exc).lower()
    name = type(exc).__name__.lower()
    if "disconnect" in name or "closed" in name or "crash" in name:
        return True
    return any(k in msg for k in _DISCONNECT_KEYWORDS)


def _try_recover() -> bool:
    """尝试在当前进程重新 attach 到 daemon(端口仍活时)。

    成功的前提:Chrome 还在 9222 端口上听(可能是主进程 daemon.restart() 已重起)。
    Chrome 真死时返回 False — 调用方应该把异常往上抛。
    """
    global _ACTIVE_PAGE, _ACTIVE_PORT
    if _ACTIVE_PORT is None:
        import os
        _ACTIVE_PORT = int(os.environ.get("V6_BROWSER_PORT", 9222))
    # 主动清掉旧 page
    _ACTIVE_PAGE = None
    # 探一下端口
    try:
        import socket as _s
        with _s.create_connection(("127.0.0.1", _ACTIVE_PORT), timeout=0.5):
            pass
    except OSError:
        return False
    # 重新 attach 一次 — 失败说明 chrome 虽然听端口但已不可用
    try:
        _get_page()
        return True
    except Exception:
        _ACTIVE_PAGE = None
        return False


def _with_recovery(fn):
    """装饰器:catch 失联类异常 → 调 _try_recover() → 重试一次 → 仍失败抛 PageDisconnectedError。

    凡是触碰浏览器的 helper 都该用这个包一层。其他业务异常(ElementNotFound/Timeout 等)
    不是失联,会原样透传,不会被这里捕获。
    """
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except BrowserError:
            # 已经是我们的语义化异常 — 不属于失联,直接抛
            raise
        except BaseException as e:
            if not _is_disconnect_error(e):
                raise
            # 失联类 — 试一次重连
            if _try_recover():
                try:
                    return fn(*args, **kwargs)
                except BaseException as e2:
                    if _is_disconnect_error(e2):
                        raise PageDisconnectedError(str(e2), recovered_attempted=True) from e2
                    raise
            raise PageDisconnectedError(str(e), recovered_attempted=True) from e
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    wrapper.__wrapped__ = fn
    return wrapper


# ────────────────────────────
# 选择器规整 — 让 LLM 既能写 'css:.foo' 也能写 '.foo'
# ────────────────────────────

_TAGGED_PREFIX_RE = re.compile(r"^(css|xpath|x|@|@@|tag|text):", re.IGNORECASE)


def _normalize_selector(selector: str) -> str:
    """如果 LLM 没写前缀,默认按 css 处理"""
    if not isinstance(selector, str) or not selector.strip():
        raise ValueError(f"selector 必须是非空字符串,got {selector!r}")
    s = selector.strip()
    if _TAGGED_PREFIX_RE.match(s):
        return s
    return f"css:{s}"


# ────────────────────────────
# Dialog 检查 — 在每个交互动作前先 check 一下,撞上 dialog 立即抛清晰错误
# ────────────────────────────

def _check_pending_dialog() -> None:
    """检测原生 dialog;有则抛 PendingDialogError"""
    page = _get_page()
    # DrissionPage 的 page.handle_alert(accept=...) 不返回当前 dialog 内容,
    # 自己用 JS 探:dialog 阻塞时 _detect_dialog 注入的 listener 会留下证据
    # 简化版:依赖 DrissionPage 内部 has_alert 属性(不同版本兼容性写多个 fallback)
    try:
        if getattr(page, "has_alert", False):
            text = getattr(page, "alert_text", "") or "(unknown)"
            raise PendingDialogError("alert", text)
    except AttributeError:
        pass


# ────────────────────────────
# 导航
# ────────────────────────────

def goto(url: str, wait: float = 2, timeout: float = DEFAULT_NAV_TIMEOUT) -> dict:
    """导航到 url,默认等 DOM 加载完。

    Args:
        url: 目标 URL(必须 http/https)
        wait: 加载完成后再额外等 N 秒(给 SPA 渲染时间)
        timeout: 整个导航的硬超时

    Returns:
        {"url": final_url, "title": ..., "status": "ok",
         "available_skills": [{"name", "description"}, ...]  ← 仅当本站有 skill 时}

    Raises:
        NavigationTimeoutError: 超时
        HumanInterventionRequired: 检测到 Cloudflare / 登录页
    """
    page = _get_page()
    if not isinstance(url, str) or not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError(f"goto() 仅接受 http/https URL,got {url!r}")
    try:
        page.get(url, timeout=timeout)
        page.wait.doc_loaded(timeout=timeout)
    except Exception as e:
        # DrissionPage timeout 异常字符串通常包含 "timeout"
        msg = str(e).lower()
        if "timeout" in msg or "load" in msg:
            raise NavigationTimeoutError(url, timeout, str(e)) from e
        raise
    if wait > 0:
        time.sleep(wait)

    # 尝试识别 challenge 页面
    title = page.title or ""
    if any(k in title.lower() for k in ("just a moment", "cloudflare", "verify", "验证", "challenge")):
        raise HumanInterventionRequired(
            f"detected challenge/verify page: {title}", url=page.url
        )

    result = {"url": page.url, "title": title, "status": "ok"}

    # skill 提示 — 不预注入内容,只列名让 LLM 决定是否 read_skill
    try:
        from .skills import get_registry
        reg = get_registry()
        if reg:
            matched = reg.match_by_url(page.url)
            if matched:
                result["available_skills"] = [
                    {"name": s.name, "description": s.description}
                    for s in matched
                ]
    except Exception:
        pass  # skill 系统不可用不影响 navigate

    return result


# ──────────────────────────────────
# Skill 访问 helpers
# ──────────────────────────────────

def list_skills(url: Optional[str] = None) -> list:
    """列出与 url(默认当前页)匹配的 skill。

    Returns:
        [{"name", "description", "tokens": 估算长度}, ...]  空列表表示无匹配
    """
    from .skills import get_registry
    reg = get_registry()
    if not reg:
        return []
    target_url = url or current_url()
    matched = reg.match_by_url(target_url)
    return [
        {"name": s.name, "description": s.description, "tokens": s.estimated_tokens}
        for s in matched
    ]


def read_skill(name: str) -> str:
    """拉取 skill 的 markdown 内容。

    Raises:
        FileNotFoundError: skill 不存在
    """
    from .skills import get_registry
    reg = get_registry()
    if not reg:
        raise RuntimeError("SkillRegistry 未加载 — 主进程未调 skills.auto_load()")
    skill = reg.get(name)
    if not skill:
        available = [s.name for s in reg.list_all()]
        raise FileNotFoundError(f"skill {name!r} 不存在;可用: {available}")
    return skill.content


def reload(timeout: float = DEFAULT_NAV_TIMEOUT) -> dict:
    """刷新当前页"""
    page = _get_page()
    try:
        page.refresh()
        page.wait.doc_loaded(timeout=timeout)
    except Exception as e:
        raise NavigationTimeoutError(page.url, timeout, str(e)) from e
    return {"url": page.url, "title": page.title or "", "status": "ok"}


def back() -> dict:
    """浏览器后退"""
    page = _get_page()
    page.back()
    return {"url": page.url, "title": page.title or ""}


def forward() -> dict:
    """浏览器前进"""
    page = _get_page()
    page.forward()
    return {"url": page.url, "title": page.title or ""}


# ──────────────────────────
# 信息(只读,无副作用)
# ──────────────────────────

def page_info() -> dict:
    """返回当前页面摘要 — viewport/scroll/标题/url/页面尺寸。

    类似 harness 的 page_info(),用作 LLM 探针的第一选择。
    """
    page = _get_page()
    expr = (
        "return JSON.stringify({"
        "url:location.href,"
        "title:document.title,"
        "w:innerWidth,h:innerHeight,"
        "sx:scrollX,sy:scrollY,"
        "pw:document.documentElement.scrollWidth,"
        "ph:document.documentElement.scrollHeight,"
        "ready:document.readyState"
        "})"
    )
    raw = page.run_js(expr)
    if isinstance(raw, str):
        return json.loads(raw)
    return raw  # 某些场景 DrissionPage 已自动 parse


def current_url() -> str:
    return _get_page().url


def current_title() -> str:
    return _get_page().title or ""


# ──────────────────────────
# 元素查找/等待
# ──────────────────────────

def query(selector: str, timeout: float = 0) -> Optional[Any]:
    """查询单个元素,找不到返回 None(不抛异常)。

    timeout=0 立即返回;>0 时阻塞等到 timeout 秒。
    """
    sel = _normalize_selector(selector)
    page = _get_page()
    try:
        return page.ele(sel, timeout=timeout)
    except Exception:
        return None


def query_all(selector: str) -> list:
    """查询所有匹配元素,返回列表(可能为空)"""
    sel = _normalize_selector(selector)
    return _get_page().eles(sel) or []


def wait_for_element(
    selector: str,
    timeout: float = DEFAULT_ELEMENT_TIMEOUT,
    visible: bool = True,
) -> Any:
    """等待元素出现并(可选)可见。

    Args:
        selector: CSS / xpath
        timeout: 秒
        visible: True = 同时要求元素可见(尺寸 > 0 且非 display:none)

    Returns:
        匹配到的 element 对象

    Raises:
        ElementNotFoundError / ElementNotVisibleError
    """
    sel = _normalize_selector(selector)
    page = _get_page()

    el = page.ele(sel, timeout=timeout)
    if not el:
        raise ElementNotFoundError(selector, timeout)

    if visible:
        # DrissionPage 提供 wait.ele_displayed
        try:
            if not page.wait.ele_displayed(sel, timeout=timeout):
                raise ElementNotVisibleError(selector, timeout)
        except AttributeError:
            # 部分版本无此方法 — fallback 到 JS 自检
            displayed = page.run_js(
                "const e=document.querySelector(arguments[0]);"
                "if(!e)return false;"
                "const s=getComputedStyle(e);"
                "return s.display!=='none'&&s.visibility!=='hidden'&&e.offsetWidth>0&&e.offsetHeight>0;",
                selector if selector.startswith("css:") is False else selector[4:],
            )
            if not displayed:
                raise ElementNotVisibleError(selector, timeout)
    return el


# ────────────────────────
# 交互
# ────────────────────────

def click(selector: str, timeout: float = DEFAULT_CLICK_TIMEOUT) -> dict:
    """点击元素 — 内置等待 + dialog 检查。

    Returns:
        {"clicked": selector, "url_after": ...}
    """
    _check_pending_dialog()
    el = wait_for_element(selector, timeout=timeout, visible=True)
    page = _get_page()
    try:
        el.click()
    except Exception as e:
        # DrissionPage click 失败常见原因:被遮挡 / 动画 / iframe
        msg = str(e).lower()
        if "intercept" in msg or "covered" in msg or "obscured" in msg:
            raise ElementClickInterceptedError(selector, str(e)) from e
        raise
    return {"clicked": selector, "url_after": page.url}


def click_at(x: int, y: int) -> dict:
    """按坐标点击(用于无法用 selector 定位的场景,如 canvas 内点击)"""
    _check_pending_dialog()
    page = _get_page()
    page.actions.move_to((x, y)).click()
    return {"clicked_at": (x, y), "url_after": page.url}


def fill(selector: str, text: str, clear: bool = True, timeout: float = DEFAULT_CLICK_TIMEOUT) -> dict:
    """填充表单字段。

    Args:
        clear: 填入前先清空
    """
    _check_pending_dialog()
    el = wait_for_element(selector, timeout=timeout, visible=True)
    if clear:
        try:
            el.clear()
        except Exception:
            pass
    el.input(text)
    return {"filled": selector, "length": len(text)}


def press_key(key: str) -> dict:
    """按一个键(全局,作用于当前焦点元素)。

    例:press_key('Enter') / press_key('Tab') / press_key('Escape')
    """
    page = _get_page()
    page.actions.type(key)
    return {"pressed": key}


def scroll(dy: int = 500, dx: int = 0) -> dict:
    """滚动页面。dy>0 向下,<0 向上。"""
    page = _get_page()
    if dy > 0:
        page.scroll.down(abs(dy))
    elif dy < 0:
        page.scroll.up(abs(dy))
    if dx != 0:
        # DrissionPage 的水平滚动用 right/left
        if dx > 0:
            page.scroll.right(abs(dx))
        else:
            page.scroll.left(abs(dx))
    new_y = page.run_js("return window.scrollY")
    return {"scrolled": (dx, dy), "scroll_y": new_y}


# ──────────────────────────
# JavaScript
# ──────────────────────────

_RETURN_RE = re.compile(r"\breturn\b")


def js(
    expression: str,
    *,
    timeout: float = 30,
    max_result_bytes: int = DEFAULT_JS_RESULT_LIMIT,
) -> Any:
    """在页面执行 JavaScript 并返回值。

    自动处理:
    - 没有 return 时自动包成 IIFE 取最后表达式
    - 返回值序列化后超过 max_result_bytes 抛 JSResultTooLargeError

    Args:
        expression: JS 代码片段。可以是 'return 1+1'、'1+1'、或带 `return` 的语句块
        timeout: JS 执行超时
        max_result_bytes: 返回值 JSON 序列化后的字节上限

    Returns:
        JSON-decodable 值(dict/list/str/int/float/bool/None)

    Raises:
        JSExecutionError: JS 内部抛异常
        JSResultTooLargeError: 返回值过大
    """
    page = _get_page()
    code = expression.strip()
    if not _RETURN_RE.search(code):
        # 包成 IIFE,取最后一个语句作为返回值
        code = f"return (function(){{ {code} }})();"

    try:
        result = page.run_js(code, timeout=timeout)
    except Exception as e:
        raise JSExecutionError(code, str(e)) from e

    # 大小检查 — 序列化后看字节
    try:
        serialized = json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        serialized = repr(result)
    size = len(serialized.encode("utf-8"))
    if size > max_result_bytes:
        raise JSResultTooLargeError(size, max_result_bytes)
    return result


# ──────────────────────────
# 视觉
# ──────────────────────────

def set_window_size(width: int = 1440, height: int = 1740) -> dict:
    """Resize the Chromium window to a known outer size.

    Why this matters for the vision-first workflow: vision models perform best on
    images of consistent, predictable size. When the inner viewport floats with
    whatever Chrome opened at, every screenshot has different dimensions and the
    worker cannot plan scroll amounts. Set once early in the session and every
    viewport screenshot becomes a stable size (default 1440x1740 outer →
    ~1410 × ~1310 CSS px inner, after Chrome chrome subtracts ~30 W / ~140 H).
    The tall default minimizes scroll passes (≈6 instead of ≈12 for a 7000px page)
    while staying within qwen3.5-omni-plus's clear-OCR window.

    Args:
        width: outer window width (Chrome chrome eats ~30px → inner viewport ~ width-30)
        height: outer window height (Chrome chrome eats ~140px → inner viewport ~ height-140)

    Returns:
        {"requested": [w,h], "innerWidth": ..., "innerHeight": ..., "scrollHeight": ...}
    """
    page = _get_page()
    page.set.window.size(int(width), int(height))
    geom = js(
        "return {innerWidth: window.innerWidth, "
        "innerHeight: window.innerHeight, "
        "scrollHeight: document.documentElement.scrollHeight, "
        "devicePixelRatio: window.devicePixelRatio};"
    )
    return {"requested": [int(width), int(height)], **geom}


def screenshot(path: Optional[Union[str, Path]] = None, full_page: bool = False) -> str:
    """截图保存,只返回文件路径(不返回 base64,避免污染上下文)。

    Args:
        path: 保存路径。None 则用时间戳自动命名,放到 daemon worktree 的 screenshots/
        full_page: True = 截整个滚动页面;False = 仅可视区域

    Returns:
        保存的绝对路径(str)
    """
    page = _get_page()
    if path is None:
        # 默认放到 v6/worktrees/_screenshots/(由 daemon 启动时 ensure)
        scratch = Path(__file__).resolve().parents[1] / "worktrees" / "_screenshots"
        scratch.mkdir(parents=True, exist_ok=True)
        path = scratch / f"shot_{int(time.time() * 1000)}.png"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    page.get_screenshot(path=str(path), full_page=full_page)
    return str(path.resolve())


# ────────────────────────
# 标签页
# ────────────────────────

def list_tabs() -> list:
    """列出当前所有 tab 的 {target_id, title, url}"""
    page = _get_page()
    tabs = []
    try:
        for t in page.tabs:
            tabs.append({
                "target_id": getattr(t, "tab_id", str(t)),
                "title": getattr(t, "title", "") or "",
                "url": getattr(t, "url", "") or "",
            })
    except Exception:
        # fallback:用 CDP /json/list
        import urllib.request
        from .daemon import get_daemon
        port = get_daemon().port
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as r:
                for entry in json.loads(r.read()):
                    if entry.get("type") == "page":
                        tabs.append({
                            "target_id": entry.get("id"),
                            "title": entry.get("title", ""),
                            "url": entry.get("url", ""),
                        })
        except Exception:
            pass
    return tabs


def switch_tab(target: Union[str, int]) -> dict:
    """切到指定 tab(支持 target_id 或索引)"""
    page = _get_page()
    tabs = list_tabs()
    if not tabs:
        raise TabNotFoundError(target, [])

    if isinstance(target, int):
        if not (0 <= target < len(tabs)):
            raise TabNotFoundError(target, [(i, t["url"]) for i, t in enumerate(tabs)])
        tid = tabs[target]["target_id"]
    else:
        match = next((t for t in tabs if t["target_id"] == target), None)
        if not match:
            raise TabNotFoundError(target, [t["target_id"] for t in tabs])
        tid = target

    try:
        page.activate_tab(tid)
    except AttributeError:
        # 旧版 DrissionPage 用 page.to_tab
        page.to_tab(tid)
    return {"switched_to": tid, "url": page.url}


def new_tab(url: Optional[str] = None) -> dict:
    """打开新 tab(可选指定 url)"""
    page = _get_page()
    new = page.new_tab(url) if url else page.new_tab()
    return {
        "target_id": getattr(new, "tab_id", ""),
        "url": getattr(new, "url", url or ""),
    }


def close_tab() -> dict:
    """关闭当前 tab"""
    page = _get_page()
    cur_url = page.url
    page.close()
    return {"closed_url": cur_url, "remaining": len(list_tabs())}


# ────────────────────────
# 等待
# ────────────────────────

def sleep(seconds: float) -> None:
    """阻塞等待"""
    if seconds < 0 or seconds > 60:
        raise ValueError(f"sleep 仅支持 0-60 秒,got {seconds}")
    time.sleep(seconds)


def wait_for_load(timeout: float = 10) -> dict:
    """等 document.readyState === 'complete'"""
    page = _get_page()
    page.wait.doc_loaded(timeout=timeout)
    return {"ready": True, "url": page.url}


# ──────────────────────────────────
# DOM 探索 helpers — 防止 LLM 反复瞎试 selector
# ──────────────────────────────────

def dom_outline(max_items: int = 30, min_text_len: int = 4) -> list:
    """一次返回页面上前 N 个候选交互/文本元素的摘要,给 LLM 当 selector 候选库。

    每个返回 {tag, id, classes, text_preview, attrs, selector_hint}。
    text_preview 是 textContent 的前 60 字符。
    selector_hint 是优先级最高的 CSS selector(优先 id,其次首个 class,再次 tag)。

    用法:LLM 不知道列表页结构时,先调 dom_outline(),看输出找最像产品卡的 selector,
    再写 js("return [...document.querySelectorAll('your_sel')].map(...)")。
    """
    page = _get_page()
    expr = (
        "const out = [];"
        "const seen = new Set();"
        "const all = document.querySelectorAll('a, article, li, div[class], section');"
        "for (const e of all) {"
        "  if (out.length >= " + str(max_items) + ") break;"
        "  const t = (e.textContent || '').trim().replace(/\\s+/g, ' ');"
        "  if (t.length < " + str(min_text_len) + ") continue;"
        "  const sig = e.tagName + '|' + (e.className || '') + '|' + t.slice(0, 30);"
        "  if (seen.has(sig)) continue;"
        "  seen.add(sig);"
        "  const cls = e.className && typeof e.className === 'string' ? e.className.split(/\\s+/).filter(Boolean) : [];"
        "  const hint = e.id ? ('#' + e.id) : (cls.length ? (e.tagName.toLowerCase() + '.' + cls[0]) : e.tagName.toLowerCase());"
        "  const attrs = {};"
        "  for (const a of ['href', 'data-slug', 'data-id', 'data-name']) {"
        "    if (e.hasAttribute(a)) attrs[a] = e.getAttribute(a);"
        "  }"
        "  out.push({tag: e.tagName.toLowerCase(), id: e.id || null, classes: cls.slice(0, 5), text_preview: t.slice(0, 60), attrs, selector_hint: hint});"
        "}"
        "return out;"
    )
    return js(expr, max_result_bytes=DEFAULT_JS_RESULT_LIMIT)


def dom_classes(min_count: int = 3, top_n: int = 30) -> list:
    """返回页面上出现 >= min_count 次的 className,按出现次数倒序。

    列表项(产品卡 / 评论 / 行)的天然标志:同一个 class 出现多次。
    比 dom_outline 更聚焦"批量数据 selector"。

    Returns:
        [{"class_name": "product-card", "count": 50, "tag_sample": "div", "text_sample": "Werd..."}]

    用法:LLM 不知道列表 selector 时,先调 dom_classes(),输出里出现 N 次的就是
    候选列表选择器,直接 `.<class_name>` 当 selector 用。
    """
    page = _get_page()
    expr = (
        "const counts = new Map();"
        "const samples = new Map();"
        "for (const e of document.querySelectorAll('[class]')) {"
        "  if (typeof e.className !== 'string') continue;"
        "  for (const c of e.className.split(/\\s+/)) {"
        "    if (!c || c.length > 60) continue;"
        "    counts.set(c, (counts.get(c) || 0) + 1);"
        "    if (!samples.has(c)) {"
        "      const t = (e.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 50);"
        "      samples.set(c, {tag: e.tagName.toLowerCase(), text: t});"
        "    }"
        "  }"
        "}"
        "const out = [];"
        "for (const [name, n] of counts) {"
        "  if (n < " + str(min_count) + ") continue;"
        "  const s = samples.get(name);"
        "  out.push({class_name: name, count: n, tag_sample: s.tag, text_sample: s.text});"
        "}"
        "out.sort((a, b) => b.count - a.count);"
        "return out.slice(0, " + str(top_n) + ");"
    )
    return js(expr, max_result_bytes=DEFAULT_JS_RESULT_LIMIT)


def dom_query(selector: str, max_items: int = 20, fields: Optional[list] = None) -> list:
    """精准探索 — 给一个 selector,返回前 N 个匹配元素的字段摘要。

    fields 默认提取 {text, href, src, value, dataset.*}。
    用 dom_outline() 找到大概 selector 后,用 dom_query() 取细节,再写主循环。

    例:dom_query('.product-card', max_items=3)
        → [{text:'Werd...', href:'/ai/werd/', dataset:{slug:'werd'}}, ...]
    """
    page = _get_page()
    safe_sel = selector.replace("\\", "\\\\").replace("'", "\\'")
    expr = (
        "const els = [...document.querySelectorAll('" + safe_sel + "')]"
        ".slice(0, " + str(max_items) + ");"
        "return els.map(e => {"
        "  const dataset = {};"
        "  for (const k in e.dataset) dataset[k] = e.dataset[k];"
        "  return {"
        "    text: (e.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 200),"
        "    href: e.href || e.getAttribute('href') || null,"
        "    src: e.src || null,"
        "    value: e.value || null,"
        "    inner_html_size: e.innerHTML ? e.innerHTML.length : 0,"
        "    dataset: dataset"
        "  };"
        "});"
    )
    return js(expr, max_result_bytes=DEFAULT_JS_RESULT_LIMIT)


# ──────────────────────────
# Dialog
# ──────────────────────────

def accept_dialog(text: Optional[str] = None) -> dict:
    """接受 alert/confirm/prompt(prompt 时可填入 text)"""
    page = _get_page()
    try:
        page.handle_alert(accept=True, send=text)
    except Exception as e:
        raise PendingDialogError("unknown", str(e)) from e
    return {"action": "accept", "text_sent": text}


def dismiss_dialog() -> dict:
    """拒绝 confirm/prompt"""
    page = _get_page()
    try:
        page.handle_alert(accept=False)
    except Exception as e:
        raise PendingDialogError("unknown", str(e)) from e
    return {"action": "dismiss"}


# ──────────────────────────────────
# 模块加载末尾:把所有触碰浏览器的 helper 自动装上 _with_recovery
# 这样无论主进程还是 worker 子进程,helpers 失联时都会:
#   1. 先在自己进程内重新 attach 一次(端口仍活时)
#   2. 仍失败 → 抛 PageDisconnectedError(让工具层 / batch 处理)
# ──────────────────────────────────

_RECOVERABLE_HELPERS = (
    # 导航
    "goto", "reload", "back", "forward",
    # 信息
    "page_info", "current_url", "current_title",
    # 查询 / 等待
    "query", "query_all", "wait_for_element", "wait_for_load",
    # 交互
    "click", "click_at", "fill", "press_key", "scroll",
    # JS
    "js",
    # 媒体
    "screenshot",
    # Tab
    "list_tabs", "switch_tab", "new_tab", "close_tab",
    # DOM 探查
    "dom_outline", "dom_classes", "dom_query",
    # Dialog
    "accept_dialog", "dismiss_dialog",
)


def _install_recovery() -> None:
    """模块尾运行一次:把上述 helpers 全部装上 _with_recovery 装饰器。

    set_active_port / acquire_browser / release_browser / sleep / list_skills /
    read_skill 不触碰 Chrome,故不装(避免吞业务异常)。
    """
    import sys as _sys
    mod = _sys.modules[__name__]
    for name in _RECOVERABLE_HELPERS:
        fn = getattr(mod, name, None)
        if fn is None or not callable(fn):
            continue
        if getattr(fn, "__wrapped__", None) is not None:
            continue  # 已经装过(防止重 import)
        setattr(mod, name, _with_recovery(fn))


_install_recovery()


# ────────────────────────
# 模块自检 — python -m browser.helpers
# ────────────────────────

if __name__ == "__main__":
    print("[browser_helpers] 自检")
    info = goto("https://example.com")
    print(f"  goto example.com → {info}")
    print(f"  page_info → {page_info()}")
    print(f"  query('h1') → {query('h1')}")
    print(f"  js('return document.title') → {js('return document.title')}")
    shot = screenshot()
    print(f"  screenshot → {shot}")
    try:
        js("return 'x'.repeat(100000)")
    except JSResultTooLargeError as e:
        print(f"  ✅ JSResultTooLargeError 正确触发: size={e.size_bytes} limit={e.limit_bytes}")
    print("[browser_helpers] 全部通过")
