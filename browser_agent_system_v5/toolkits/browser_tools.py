"""
browser_tools.py — 基于 DrissionPage 的真实浏览器工具集

V4 核心浏览器操作工具，使用 DrissionPage 替换了原有的 Playwright，
以获取更原生的防反爬脱敏能力（绕过 Cloudflare 等）：
- NavigateTool: 打开指定 URL
- ClickElementTool: 点击页面元素（自动降级到 JS）
- ExtractTextTool: 提取页面/元素文本（优先 JS DOM 遍历）
- ScreenshotTool: 截图保存到 WorkTree（支持视觉分析）
- ScrollPageTool: 滚动页面
- FillFormTool: 填充表单字段（支持 enter_key）
- WaitUserTool: 人工干预工具

设计原则（通用性）：
- 所有 DOM 操作优先使用 page.run_js() 直接操作真实渲染 DOM
- DrissionPage 的 page.ele()/eles() 仅作辅助，避免其内部 DOM 表示层与
  真实页面不一致（如 WebComponent/Shadow DOM/SPA 懒加载）
- 每个工具都有 fallback 策略，不需要网站特定的 system prompt 知识
"""

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from toolkits.base_tool import BaseTool
from core.agent_definition import TrustLevel
from toolkits.vision_helper import get_vision_analyzer


# ══════════════════════════════════════════════════════════════
#  页面缓存
# ══════════════════════════════════════════════════════════════

_CACHE_TTL_SECONDS = 300  # 缓存有效期：5 分钟


@dataclass
class _PageCacheEntry:
    """页面缓存条目"""
    url: str                   # 原始请求 URL
    final_url: str             # 重定向后的最终 URL
    title: str                 # 页面标题
    timestamp: float           # 缓存时间
    # 文本提取缓存：selector_hash → extracted_text
    text_cache: Dict[str, str] = field(default_factory=dict)
    # 页面内容是否可能已变化（click/fill 后标记为脏）
    dirty: bool = False

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.timestamp) > _CACHE_TTL_SECONDS


# ══════════════════════════════════════════════════════════════
#  浏览器管理器
# ══════════════════════════════════════════════════════════════


class DPBrowserManager:
    """DrissionPage 浏览器实例管理器（带页面缓存）"""

    def __init__(self, profile_id: str = "default"):
        self.profile_id = profile_id
        self._page = None
        self._lock = asyncio.Lock()
        # 页面级缓存：url → _PageCacheEntry
        self._page_cache: Dict[str, _PageCacheEntry] = {}

    # ── 缓存管理 ──

    def cache_get(self, url: str) -> Optional[_PageCacheEntry]:
        """获取页面缓存，过期或脏标记则返回 None"""
        # 规范化 URL：去除尾部斜杠和 hash fragment
        normalized = self._normalize_url(url)
        entry = self._page_cache.get(normalized)
        if entry is None:
            return None
        if entry.is_expired or entry.dirty:
            del self._page_cache[normalized]
            return None
        return entry

    def cache_put(self, url: str, final_url: str, title: str) -> _PageCacheEntry:
        """写入/更新页面缓存条目"""
        normalized = self._normalize_url(url)
        entry = _PageCacheEntry(
            url=url,
            final_url=final_url,
            title=title,
            timestamp=time.time(),
        )
        self._page_cache[normalized] = entry
        return entry

    def cache_put_text(self, url: str, selector: str, text: str) -> None:
        """缓存文本提取结果"""
        normalized = self._normalize_url(url)
        entry = self._page_cache.get(normalized)
        if entry is None:
            return
        sel_hash = self._selector_hash(selector)
        entry.text_cache[sel_hash] = text

    def cache_get_text(self, url: str, selector: str) -> Optional[str]:
        """获取缓存的文本提取结果"""
        entry = self.cache_get(url)
        if entry is None:
            return None
        sel_hash = self._selector_hash(selector)
        return entry.text_cache.get(sel_hash)

    def cache_invalidate(self, url: str = "") -> None:
        """使缓存失效。url 为空则清空全部；否则仅失效指定 URL"""
        if not url:
            self._page_cache.clear()
            return
        normalized = self._normalize_url(url)
        self._page_cache.pop(normalized, None)

    def cache_mark_dirty(self, url: str = "") -> None:
        """
        标记当前页面缓存为脏（click/fill 等修改操作后调用）。
        url 为空则标记所有缓存为脏。
        """
        if not url:
            for entry in self._page_cache.values():
                entry.dirty = True
            return
        normalized = self._normalize_url(url)
        entry = self._page_cache.get(normalized)
        if entry:
            entry.dirty = True

    @staticmethod
    def _normalize_url(url: str) -> str:
        """规范化 URL 用于缓存 key：去除 fragment、尾部斜杠"""
        # 去除 #fragment
        base = url.split("#")[0]
        # 去除尾部斜杠
        return base.rstrip("/")

    @staticmethod
    def _selector_hash(selector: str) -> str:
        """对选择器生成短哈希，用作缓存子 key"""
        if not selector:
            return "__full_page__"
        return hashlib.md5(selector.encode()).hexdigest()[:12]

    async def get_page(self) -> Any:
        async with self._lock:
            if self._page is None:
                try:
                    # 浏览器启动添加超时保护（60秒），避免卡死
                    await asyncio.wait_for(
                        asyncio.to_thread(self._launch),
                        timeout=60.0,
                    )
                except asyncio.TimeoutError:
                    print(f"[BrowserManager] ❌ 浏览器启动超时（60秒），可能代理不可用或进程冲突")
                    raise RuntimeError(
                        "浏览器启动超时，请检查：\n"
                        "  1. 代理 127.0.0.1:7890 是否可用\n"
                        "  2. 是否有残留的 Chrome 进程占用 profile\n"
                        "  3. 手动关闭所有 Chrome 窗口后重试"
                    )
            return self._page

    def _launch(self) -> None:
        try:
            from DrissionPage import ChromiumPage, ChromiumOptions
        except ImportError:
            raise ImportError("请执行: pip install DrissionPage")

        co = ChromiumOptions()
        proxy_server = "http://127.0.0.1:7890"
        co.set_proxy(proxy_server)

        project_root = Path(__file__).parent.parent.absolute()
        profile_path = project_root / "browser_profiles" / self.profile_id
        profile_path.mkdir(parents=True, exist_ok=True)
        co.set_user_data_path(str(profile_path.absolute()))

        co.headless(False)
        co.mute(True)
        co.set_argument('--no-sandbox')
        co.set_argument('--disable-gpu')
        # 隐藏 navigator.webdriver 自动化特征（JD 等网站会用此检测自动化）
        co.set_argument('--disable-blink-features=AutomationControlled')
        co.set_argument('--disable-infobars')
        # 真实 User-Agent（JD 会检测 UA 真实性）
        co.set_user_agent(
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
        )

        self._page = ChromiumPage(co)
        print(f"[BrowserManager] [BROWSER] Browser launched | proxy: {proxy_server} | profile: {self.profile_id}")

        # 启动后注入 JS：覆盖 navigator.webdriver 等自动化标识
        self._page.run_js(
            "Object.defineProperty(navigator, 'webdriver', {"
            "get: () => undefined, configurable: true"
            "});"
        )

    async def close(self) -> None:
        if self._page:
            try:
                await asyncio.to_thread(self._page.quit)
            except Exception as e:
                print(f"[BrowserManager] [WARN] Close exception: {e}")
            self._page = None
            print("[BrowserManager] [CLOSE] Browser closed")

    @property
    def is_active(self) -> bool:
        if self._page is None:
            return False
        try:
            _ = self._page.title
            return True
        except Exception:
            self._page = None
            return False

    def _is_disconnected(self, error: Exception) -> bool:
        """检测是否是浏览器断连错误"""
        error_msg = str(error).lower()
        disconnect_keywords = [
            "disconnect", "connection", "closed", "target closed",
            "session", "not connected", "invalid session"
        ]
        return any(keyword in error_msg for keyword in disconnect_keywords)

    async def reconnect(self) -> bool:
        """尝试重新连接浏览器
        
        Returns:
            bool: 重连是否成功
        """
        print("[BrowserManager] [RECONNECT] 检测到浏览器断连，尝试重新连接...")
        async with self._lock:
            # 清理旧实例
            if self._page:
                try:
                    await asyncio.to_thread(self._page.quit)
                except Exception:
                    pass
                self._page = None
            
            # 重新启动
            try:
                await asyncio.to_thread(self._launch)
                print("[BrowserManager] [RECONNECT] ✅ 浏览器重连成功")
                return True
            except Exception as e:
                print(f"[BrowserManager] [RECONNECT] ❌ 浏览器重连失败: {e}")
                return False

    async def get_page_with_reconnect(self) -> tuple[Any, bool]:
        """获取页面，如果断连则自动重连
        
        Returns:
            tuple[page, reconnected]: (页面对象, 是否发生了重连)
        """
        page = await self.get_page()
        
        # 检查连接是否有效
        try:
            _ = page.title
            return page, False
        except Exception as e:
            if self._is_disconnected(e):
                # 尝试重连
                success = await self.reconnect()
                if success:
                    page = await self.get_page()
                    return page, True
                else:
                    raise Exception("浏览器重连失败，请手动重启")
            else:
                # 不是断连错误，直接抛出
                raise


_browser_manager = DPBrowserManager()


def get_browser_manager() -> DPBrowserManager:
    return _browser_manager


# ══════════════════════════════════════════════════════════════
#  通用 JS 工具函数（所有工具共享的底层能力）
# ══════════════════════════════════════════════════════════════


def _js_click(page, selector: str) -> str:
    """
    通用的 JS 点击实现：直接操作真实渲染 DOM。
    触发 mousedown → mouseup → click 完整事件链，兼容 React/Vue/WebComponent。
    返回 'OK' 或错误信息。
    注意：不用 IIFE，直接语句块，这样 page.run_js() 才能正确返回值。
    """
    escaped = selector.replace("'", "\\'")
    js = f"""
var el = document.querySelector('{escaped}');
if (!el) {{ throw new Error('NOT_FOUND'); }}
var opts = {{bubbles:true, cancelable:true, view:window}};
el.dispatchEvent(new MouseEvent('mousedown', opts));
el.dispatchEvent(new MouseEvent('mouseup', opts));
el.click();
"""
    try:
        page.run_js(js)
        return "OK"
    except Exception as e:
        if "NOT_FOUND" in str(e):
            return "NOT_FOUND"
        return str(e)


def _js_fill(page, selector: str, value: str) -> str:
    """通用的 JS 填写实现。返回 'OK' 或错误信息。"""
    escaped_sel = selector.replace("'", "\\'")
    escaped_val = (value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n"))
    js = f"""
var el = document.querySelector('{escaped_sel}');
if (!el) {{ throw new Error('NOT_FOUND'); }}
el.focus();
el.value = '';
el.dispatchEvent(new Event('input', {{bubbles:true}}));
el.value = '{escaped_val}';
el.dispatchEvent(new Event('input', {{bubbles:true}}));
el.dispatchEvent(new Event('change', {{bubbles:true}}));
"""
    try:
        page.run_js(js)
        return "OK"
    except Exception as e:
        if "NOT_FOUND" in str(e):
            return "NOT_FOUND"
        return str(e)


def _js_enter(page, selector: str) -> str:
    """在指定输入框上触发 Enter 键。"""
    escaped_sel = selector.replace("'", "\\'")
    js = f"""
var el = document.querySelector('{escaped_sel}');
if (!el) {{ throw new Error('NOT_FOUND'); }}
el.dispatchEvent(new KeyboardEvent('keydown', {{key:'Enter', keyCode:13, bubbles:true}}));
el.dispatchEvent(new KeyboardEvent('keyup', {{key:'Enter', keyCode:13, bubbles:true}}));
"""
    try:
        page.run_js(js)
        return "OK"
    except Exception as e:
        if "NOT_FOUND" in str(e):
            return "NOT_FOUND"
        return str(e)


def _js_get_text(page, selector: str) -> str:
    """
    用 JS 直接从渲染 DOM 获取文本。
    返回文本内容（无内容时返回空字符串，不会抛异常）。
    """
    escaped = selector.replace("'", "\\'")
    js = f"""
var el = document.querySelector('{escaped}');
if (el) {{
    return el.textContent.replace(/\\s+/g, ' ').trim();
}}
return '';
"""
    try:
        result = page.run_js(js)
        return result if result else ""
    except Exception:
        return ""


def _js_wait_for(page, selector: str, timeout: int = 10) -> bool:
    """
    用 Python 轮询 + JS querySelector 等待元素出现。
    替代 DrissionPage 的 page.wait.ele_displayed()，
    因为后者接收的选择器格式与标准 CSS 不完全兼容。
    在 asyncio.to_thread 内调用，不会阻塞事件循环。
    """
    escaped = selector.replace("'", "\\'")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            found = page.run_js(f"return document.querySelector('{escaped}') !== null;")
            if found:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def _dp_native_click(page, selector: str) -> str:
    """
    使用 DrissionPage 原生 ele().click() 点击元素。
    作为 JS 点击的 fallback — 某些 SPA 组件只响应真实的鼠标事件。
    返回 'OK' 或错误信息。
    """
    try:
        el = page.ele(f'css:{selector}', timeout=3)
        if el:
            el.click()
            return "OK"
        return "NOT_FOUND"
    except Exception as e:
        return str(e)


# ══════════════════════════════════════════════════════════════
#  工具实现
# ══════════════════════════════════════════════════════════════


class NavigateTool(BaseTool):
    """导航到指定 URL（带页面缓存）"""

    name = "navigate"
    description = (
        "打开浏览器并导航到指定的 URL。返回页面标题和当前 URL。仅支持 http/https。\n"
        "内置页面缓存：5 分钟内重复访问同一 URL 会直接复用上次结果，跳过实际导航。\n"
        "设置 force_refresh=true 可强制重新加载页面（用于页面内容可能已更新的场景）。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "目标 URL（必须以 http:// 或 https:// 开头）"},
            "wait": {"type": "number", "description": "导航后额外等待秒数，默认 1.5。SPA 建议 2-3"},
            "force_refresh": {"type": "boolean", "description": "强制刷新，忽略缓存。默认 false"}
        },
        "required": ["url"]
    }
    is_destructive = False
    max_result_chars = 500

    async def execute(self, url: str, wait: float = 1.5, force_refresh: bool = False, **kwargs) -> str:
        manager = get_browser_manager()

        # ── 缓存检查 ──
        if not force_refresh:
            cached = manager.cache_get(url)
            if cached:
                age = int(time.time() - cached.timestamp)
                return (
                    f"[导航成功·缓存命中] 页面已加载（{age}秒前缓存）\n"
                    f"  标题: {cached.title}\n"
                    f"  URL: {cached.final_url}\n"
                    f"  💡 如需刷新，请使用 force_refresh=true"
                )

        def _sync(page):
            try:
                page.get(url, timeout=30)
                page.wait.doc_loaded()
                if wait > 0:
                    time.sleep(wait)
                final_url = page.url
                title = page.title
                # 写入缓存
                manager.cache_put(url=url, final_url=final_url, title=title)
                return (
                    f"[导航成功] 页面已加载\n"
                    f"  标题: {title}\n"
                    f"  URL: {final_url}"
                )
            except Exception as e:
                return f"[导航失败] {url}: {e}"

        try:
            page = await manager.get_page()
            return await asyncio.to_thread(_sync, page)
        except asyncio.CancelledError:
            return f"[导航取消] {url}: 操作被中断。"
        except Exception as e:
            # 如果是断连错误，尝试重连后再导航
            if manager._is_disconnected(e):
                try:
                    success = await manager.reconnect()
                    if success:
                        page = await manager.get_page()
                        result = await asyncio.to_thread(_sync, page)
                        return f"[浏览器已重连] {result}"
                    else:
                        return f"[导航失败] {url}: 浏览器重连失败"
                except Exception as reconnect_error:
                    return f"[导航失败] {url}: 重连时出错 - {reconnect_error}"
            else:
                return f"[导航失败] {url}: {e}"


class ClickElementTool(BaseTool):
    """点击页面上的指定元素"""

    name = "click_element"
    description = (
        "点击页面上匹配指定 CSS 选择器的元素。"
        "优先使用 JS 触发完整鼠标事件链，兼容 React/Vue/WebComponent。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "CSS 选择器（如 'button.submit', '#login-btn'）"},
            "wait_for": {"type": "string", "description": "点击前等待该选择器出现，留空则直接点击"},
            "timeout": {"type": "integer", "description": "wait_for 等待超时秒数，默认 10"}
        },
        "required": ["selector"]
    }
    is_destructive = True
    max_result_chars = 500

    async def execute(self, selector: str, wait_for: str = "", timeout: int = 10, **kwargs) -> str:
        manager = get_browser_manager()
        page = await manager.get_page()

        def _sync():
            try:
                # 等待目标元素出现（用 JS 轮询替代 DrissionPage wait，选择器语法统一）
                actual_wait = wait_for if wait_for else selector
                if not _js_wait_for(page, actual_wait, timeout):
                    return f"[点击失败] 等待元素超时 ({timeout}s): {actual_wait}"
                time.sleep(0.3)

                # === 策略 1：JS 事件链（mousedown → mouseup → click）===
                result = _js_click(page, selector)
                if result == "OK":
                    time.sleep(0.5)
                    # 点击可能触发页面变化，标记所有缓存为脏
                    manager.cache_mark_dirty()
                    return (
                        f"[点击成功] (JS事件链) 已点击: {selector}\n"
                        f"  当前页面: {page.title}\n"
                        f"  当前 URL: {page.url}"
                    )

                # === 策略 2：DrissionPage 原生点击（真实鼠标事件）===
                result2 = _dp_native_click(page, selector)
                if result2 == "OK":
                    time.sleep(0.5)
                    manager.cache_mark_dirty()
                    return (
                        f"[点击成功] (原生点击) 已点击: {selector}\n"
                        f"  当前页面: {page.title}\n"
                        f"  当前 URL: {page.url}"
                    )

                # === 策略 3：JS scrollIntoView + click（处理不可见元素）===
                escaped = selector.replace("'", "\\'")
                try:
                    page.run_js(
                        f"var el = document.querySelector('{escaped}');"
                        f"if(el){{el.scrollIntoView({{block:'center'}});el.click();}}"
                        f"else{{throw new Error('NOT_FOUND');}}"
                    )
                    time.sleep(0.5)
                    manager.cache_mark_dirty()
                    return (
                        f"[点击成功] (滚动+点击) 已点击: {selector}\n"
                        f"  当前页面: {page.title}\n"
                        f"  当前 URL: {page.url}"
                    )
                except Exception:
                    pass

                return f"[点击失败] 选择器 '{selector}': 所有策略均失败（JS事件链/原生点击/滚动点击）"
            except Exception as e:
                return f"[点击失败] 选择器 '{selector}': {e}"

        try:
            return await asyncio.to_thread(_sync)
        except asyncio.CancelledError:
            return f"[点击取消] 选择器 '{selector}': 操作被中断。"


class ExtractTextTool(BaseTool):
    """提取页面或元素的文本内容（支持结构化溢出分割 + 缓存）"""

    name = "extract_text"
    description = (
        "提取页面上指定元素的文本内容。"
        "通过 JS 直接读取真实渲染 DOM，不依赖 DrissionPage DOM 层，兼容 WebComponent/SPA。\n"
        "内置缓存：5 分钟内对同一页面的同一选择器重复提取，直接复用上次结果。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "CSS 选择器。留空则提取整个页面文本"},
            "wait_for": {"type": "string", "description": "提取前等待该选择器出现，留空跳过"},
            "timeout": {"type": "integer", "description": "wait_for 等待超时秒数，默认 10"},
            "force_refresh": {"type": "boolean", "description": "强制刷新，忽略缓存。默认 false"}
        },
        "required": []
    }
    is_destructive = False
    max_result_chars = 5000
    # 结构化溢出：每个段落文件的最大字符数
    _CHUNK_SIZE = 8000

    async def execute(self, selector: str = "", wait_for: str = "", timeout: int = 10,
                      force_refresh: bool = False, **kwargs) -> str:
        manager = get_browser_manager()

        # ── 缓存检查 ──
        if not force_refresh:
            page_url = await self._get_current_url(manager)
            if page_url:
                cached_text = manager.cache_get_text(page_url, selector)
                if cached_text is not None:
                    cached_entry = manager.cache_get(page_url)
                    title = cached_entry.title if cached_entry else page_url
                    return (
                        f"[文本提取成功·缓存命中] 来源: {title}\n"
                        f"  选择器: {selector or 'body (全页面)'}\n"
                        f"  字符数: {len(cached_text)}\n"
                        f"---内容开始---\n"
                        f"{cached_text}\n"
                        f"---内容结束---"
                    )

        page = await manager.get_page()

        def _sync():
            try:
                page.wait.doc_loaded()
                if wait_for:
                    if not page.wait.ele_displayed(wait_for, timeout=timeout):
                        return f"[提取失败] wait_for 等待超时: {wait_for}"
                    time.sleep(0.5)

                if selector:
                    text = _js_get_text(page, selector)
                else:
                    text = page.run_js("return document.body.innerText.replace(/\\s+/g,' ').trim();")

                # 写入缓存
                try:
                    current_url = page.url
                    manager.cache_put_text(current_url, selector, text)
                except Exception:
                    pass  # 缓存写入失败不影响主流程

                return (
                    f"[文本提取成功] 来源: {page.title}\n"
                    f"  选择器: {selector or 'body (全页面)'}\n"
                    f"  字符数: {len(text)}\n"
                    f"---内容开始---\n"
                    f"{text}\n"
                    f"---内容结束---"
                )
            except Exception as e:
                return f"[文本提取失败] {e}"

        try:
            return await asyncio.to_thread(_sync)
        except asyncio.CancelledError:
            return "[文本提取取消] 操作被中断。"

    @staticmethod
    async def _get_current_url(manager: DPBrowserManager) -> Optional[str]:
        """安全获取当前浏览器页面的 URL（用于缓存 key）"""
        try:
            page = await manager.get_page()
            url = await asyncio.to_thread(lambda: page.url)
            return url if url and url != "about:blank" else None
        except Exception:
            return None

    async def safe_execute(self, _worktree_path: str = "", _session_id: str = "", _agent_type: str = "", _context: Any = None, **kwargs) -> str:
        """
        重写溢出逻辑：对大文本按语义段落分割落盘，生成索引文件。
        
        与 BaseTool.safe_execute 的区别：
        - 不是将全文整块落盘为一个 spill_*.txt
        - 而是按空行分隔的段落分割，每段一个文件
        - 同时生成 index_*.md 索引文件，列出每段文件名、行数、首行摘要
        - Agent 可以按需精准读取某个段落，而非被迫读取全文
        """
        try:
            result = await self.execute(_worktree_path=_worktree_path, session_id=_session_id, **kwargs)
        except Exception as e:
            return f"[工具执行错误] {self.name}: {e}"

        # 不超限则原样返回
        if not (len(result) > self.max_result_chars and _worktree_path and _session_id):
            return result

        import time as _time
        from pathlib import Path as _Path

        data_dir = _Path(_worktree_path) / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        timestamp = int(_time.time())

        # ── 提取正文内容（跳过头部元信息行）──
        # result 格式: [文本提取成功] ...\n---内容开始---\n{正文}\n---内容结束---
        content_start_marker = "---内容开始---"
        content_end_marker = "---内容结束---"
        header_part = ""
        body_text = result

        start_idx = result.find(content_start_marker)
        end_idx = result.find(content_end_marker)
        if start_idx != -1 and end_idx != -1:
            header_part = result[:start_idx].strip()
            body_text = result[start_idx + len(content_start_marker):end_idx].strip()
        else:
            # 无标准标记，将整个 result 作为 body 处理
            body_text = result

        total_chars = len(body_text)

        # ── 按语义段落分割（空行分隔）──
        raw_paragraphs = body_text.split('\n\n')
        # 合并过短的段落，避免碎片化
        paragraphs: list[str] = []
        buffer = ""
        for para in raw_paragraphs:
            para = para.strip()
            if not para:
                continue
            if buffer:
                buffer += "\n\n" + para
            else:
                buffer = para
            # 当累计达到 _CHUNK_SIZE 的一半时输出一个 chunk
            if len(buffer) >= self._CHUNK_SIZE // 2:
                paragraphs.append(buffer)
                buffer = ""
        if buffer:
            paragraphs.append(buffer)

        # 如果合并后段落仍为空（极端情况），按固定大小切割
        if not paragraphs:
            for i in range(0, len(body_text), self._CHUNK_SIZE):
                paragraphs.append(body_text[i:i + self._CHUNK_SIZE])

        # ── 每段落盘为独立文件 ──
        chunk_files: list[dict] = []
        for i, para in enumerate(paragraphs, 1):
            chunk_filename = f"extract_part{i:03d}_{timestamp}.txt"
            chunk_path = data_dir / chunk_filename
            chunk_path.write_text(para, encoding="utf-8")
            line_count = para.count('\n') + 1
            # 首行摘要（截取前 80 字符）
            first_line = para.split('\n')[0][:80]
            chunk_files.append({
                "filename": chunk_filename,
                "chars": len(para),
                "lines": line_count,
                "summary": first_line,
            })

        # ── 生成索引文件 ──
        index_filename = f"index_extract_{timestamp}.md"
        index_path = data_dir / index_filename
        index_lines = [
            f"# 文本提取分割索引",
            f"",
            f"- 来源: {header_part or 'extract_text'}",
            f"- 总字符数: {total_chars}",
            f"- 分割段数: {len(chunk_files)}",
            f"- 生成时间: {timestamp}",
            f"",
            f"## 段落文件清单",
            f"",
        ]
        for cf in chunk_files:
            index_lines.append(
                f"| 文件 | 字符数 | 行数 | 首行摘要 |"
            )
            break  # 只输出一次表头
        index_lines.append("|------|--------|------|----------|")
        for cf in chunk_files:
            index_lines.append(
                f"| data/{cf['filename']} | {cf['chars']} | {cf['lines']} | {cf['summary']} |"
            )
        index_lines.append("")
        index_lines.append("使用 read_file 读取 data/ 下的文件获取对应段落的完整内容。")

        index_path.write_text('\n'.join(index_lines), encoding="utf-8")

        # ── 收集 data/ 下所有溢出文件构建待读清单 ──
        pending_files: list[str] = []
        try:
            for f in sorted(data_dir.glob("spill_*.txt")):
                pending_files.append(f.name)
            for f in sorted(data_dir.glob("extract_part*.txt")):
                pending_files.append(f.name)
        except Exception:
            pass

        file_index_str = ""
        if pending_files:
            file_index_str = "\n".join(f"  - {fn}" for fn in pending_files)

        # ── 构建截断摘要返回 ──
        truncated = result[:self.max_result_chars]

        # 构建段落表格摘要
        chunk_table_lines = []
        for cf in chunk_files:
            chunk_table_lines.append(
                f"  {cf['filename']}  ({cf['chars']}字符, {cf['lines']}行)  → {cf['summary']}"
            )
        chunk_table_str = '\n'.join(chunk_table_lines)

        return (
            f"{truncated}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📤 [大文本已结构化分割落盘] 共 {len(chunk_files)} 段\n"
            f"  总字符数: {total_chars}  |  索引文件: data/{index_filename}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📑 分段文件:\n{chunk_table_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📂 待读溢出文件清单 (共 {len(pending_files)} 个):\n{file_index_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ 根据需要使用 read_file 精准读取对应段落文件，无需读取全部！"
        )


class ScreenshotTool(BaseTool):
    """截取当前页面的屏幕截图（支持视觉分析）"""

    name = "screenshot"
    description = (
        "截取当前浏览器页面的屏幕截图并保存到 WorkTree。\n"
        "支持可选的视觉分析功能（使用 MiniMax M2.7 多模态模型）：\n"
        "- analyze=true: 自动分析截图内容，识别页面布局、交互元素和位置信息\n"
        "- find_element='描述': 在截图中查找特定元素（如'登录按钮'、'搜索框'）并返回位置信息\n"
        "视觉分析可以帮助你精确定位页面元素，避免盲目猜测 CSS 选择器。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "filename": {"type": "string", "description": "截图文件名（不含路径）"},
            "full_page": {"type": "boolean", "description": "是否截取整个页面，默认 false"},
            "analyze": {"type": "boolean", "description": "是否进行视觉分析，默认 false"},
            "find_element": {"type": "string", "description": "要查找的元素描述（如'登录按钮'），启用此参数会自动开启 analyze"}
        },
        "required": ["filename"]
    }
    is_destructive = False
    max_result_chars = 3000

    async def execute(self, filename: str = "screenshot.png", full_page: bool = False,
                      analyze: bool = False, find_element: str = "",
                      _worktree_path: str = "", **kwargs) -> str:
        page = await get_browser_manager().get_page()
        
        # 如果指定了 find_element，自动启用 analyze
        if find_element:
            analyze = True

        def _sync():
            try:
                page.wait.doc_loaded()
                time.sleep(0.5)

                if _worktree_path:
                    save_dir = Path(_worktree_path) / "screenshots"
                    save_dir.mkdir(parents=True, exist_ok=True)
                    filepath = save_dir / filename
                else:
                    filepath = Path(filename)

                page.get_screenshot(path=str(filepath), full_page=full_page)
                
                base_result = (
                    f"[截图成功]\n"
                    f"  页面: {page.title}\n"
                    f"  保存路径: {filepath}\n"
                    f"  全页面模式: {full_page}"
                )
                
                # 执行视觉分析（如果启用）
                if analyze:
                    vision_analyzer = get_vision_analyzer()
                    if not vision_analyzer.is_available():
                        return (
                            f"{base_result}\n\n"
                            f"[视觉分析] ⚠️ 未启用（需要在 config.json 中配置 api_key，或设置环境变量 ANTHROPIC_AUTH_TOKEN）"
                        )
                    
                    # 调用视觉分析
                    if find_element:
                        vision_result = vision_analyzer.analyze_for_click(
                            image_path=str(filepath),
                            target_description=find_element
                        )
                    else:
                        vision_result = vision_analyzer.analyze_screenshot(
                            image_path=str(filepath)
                        )
                    
                    if vision_result.get("success"):
                        analysis_text = vision_result.get("analysis", "")
                        location_hints = vision_result.get("location_hints", {})
                        
                        result = f"{base_result}\n\n"
                        result += "=" * 60 + "\n"
                        result += "[视觉分析结果]\n"
                        result += "=" * 60 + "\n"
                        
                        if find_element:
                            result += f"🎯 查找目标: {find_element}\n\n"
                            if location_hints:
                                result += "【快速定位提示】\n"
                                for key, value in location_hints.items():
                                    if value and value.strip():
                                        result += f"  • {key}: {value}\n"
                                result += "\n"
                        
                        result += "【详细分析】\n"
                        result += analysis_text + "\n"
                        result += "=" * 60 + "\n"
                        result += "\n💡 提示: 根据上述视觉分析结果，你可以更精确地构造 CSS 选择器或使用 run_js 定位元素。"
                        
                        return result
                    else:
                        error_msg = vision_result.get("error", "未知错误")
                        return (
                            f"{base_result}\n\n"
                            f"[视觉分析] ❌ 失败: {error_msg}"
                        )
                
                return base_result
                
            except Exception as e:
                return f"[截图失败] {e}"

        try:
            return await asyncio.to_thread(_sync)
        except asyncio.CancelledError:
            return "[截图取消] 操作被中断。"


class ScrollPageTool(BaseTool):
    """滚动页面"""

    name = "scroll_page"
    description = "滚动当前页面（direction: down/up，amount: 像素数，默认 500）。"
    input_schema = {
        "type": "object",
        "properties": {
            "direction": {"type": "string", "description": "方向: down 或 up", "enum": ["down", "up"]},
            "amount": {"type": "integer", "description": "滚动像素数，默认 500"}
        },
        "required": ["direction"]
    }
    is_destructive = False
    max_result_chars = 300

    async def execute(self, direction: str = "down", amount: int = 500, **kwargs) -> str:
        page = await get_browser_manager().get_page()

        def _sync():
            try:
                if direction == "down":
                    page.scroll.down(amount)
                else:
                    page.scroll.up(amount)
                time.sleep(0.5)
                pos = page.run_js("return window.scrollY;")
                height = page.run_js("return document.body.scrollHeight;")
                return (
                    f"[滚动成功] 方向: {direction}, 距离: {amount}px\n"
                    f"  当前位置: {pos}px / 总高度: {height}px"
                )
            except Exception as e:
                return f"[滚动失败] {e}"

        try:
            return await asyncio.to_thread(_sync)
        except asyncio.CancelledError:
            return "[滚动取消] 操作被中断。"


class FillFormTool(BaseTool):
    """填充表单字段"""

    name = "fill_form"
    description = (
        "在指定的表单字段中填入文本内容。"
        "通过 JS 直接操作 DOM，兼容所有 SPA/React/Vue。\n"
        "设置 enter_key=True 可在填写后自动按回车键提交（用于搜索框）。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "表单字段的 CSS 选择器"},
            "value": {"type": "string", "description": "要填入的文本内容"},
            "enter_key": {"type": "boolean", "description": "填写后自动按回车键提交，默认 False"}
        },
        "required": ["selector", "value"]
    }
    is_destructive = True
    max_result_chars = 300

    async def execute(self, selector: str, value: str, enter_key: bool = False, **kwargs) -> str:
        manager = get_browser_manager()
        page = await manager.get_page()

        def _sync():
            try:
                strategy_used = None

                # === 策略 1：JS 直接赋值（适合简单 HTML 表单）===
                result = _js_fill(page, selector, value)
                if result == "OK":
                    strategy_used = "JS赋值"
                
                # === 策略 2：DrissionPage 原生 input()（模拟真实键盘，适合 React 受控组件）===
                if strategy_used is None:
                    try:
                        el = page.ele(f'css:{selector}', timeout=5)
                        if el:
                            el.clear()
                            el.input(value)
                            strategy_used = "原生输入"
                    except Exception:
                        pass

                # === 策略 3：DrissionPage actions 键盘链（最底层真实键盘事件）===
                if strategy_used is None:
                    try:
                        el = page.ele(f'css:{selector}', timeout=5)
                        if el:
                            el.click()
                            time.sleep(0.3)
                            page.actions.type(value)
                            strategy_used = "键盘模拟"
                    except Exception:
                        pass

                if strategy_used is None:
                    return f"[填写失败] 选择器 '{selector}': 所有策略均失败（JS赋值/原生输入/键盘模拟）"

                # 填写操作可能触发页面变化，标记所有缓存为脏
                manager.cache_mark_dirty()

                # 处理回车提交
                if enter_key:
                    time.sleep(0.3)
                    enter_ok = False
                    # 优先 JS 回车
                    if _js_enter(page, selector) == "OK":
                        enter_ok = True
                    # fallback: DrissionPage 原生回车
                    if not enter_ok:
                        try:
                            el = page.ele(f'css:{selector}', timeout=3)
                            if el:
                                el.input('\n')
                                enter_ok = True
                        except Exception:
                            pass
                    time.sleep(1)
                    return (
                        f"[填写成功+回车] ({strategy_used}) 已在 '{selector}' 中填入并回车\n"
                        f"  内容长度: {len(value)} 字符\n"
                        f"  当前 URL: {page.url}"
                    )

                return (
                    f"[填写成功] ({strategy_used}) 已在 '{selector}' 中填入内容\n"
                    f"  内容长度: {len(value)} 字符"
                )
            except Exception as e:
                return f"[填写失败] 选择器 '{selector}': {e}"

        try:
            return await asyncio.to_thread(_sync)
        except asyncio.CancelledError:
            return f"[填写取消] 选择器 '{selector}': 操作被中断。"


class RunJSTool(BaseTool):
    """
    在浏览器中执行任意 JavaScript 代码。
    让 Agent 拥有 DOM 诊断能力：检查 iframe/Shadow DOM、元素数量、
    页面状态等，避免盲目猜测选择器导致的大量无效尝试。
    """

    name = "run_js"
    description = (
        "在当前浏览器页面中执行任意 JavaScript 代码并返回结果。\n"
        "用途示例：\n"
        "- 诊断 DOM 结构：`return document.querySelectorAll('input').length;`\n"
        "- 检查 iframe：`return document.querySelectorAll('iframe').length;`\n"
        "- 获取元素属性：`return document.querySelector('input')?.id;`\n"
        "- 复杂交互：在 Shadow DOM 或 iframe 内操作元素\n"
        "- 获取页面状态：`return document.readyState;`\n"
        "注意：使用 return 语句返回结果。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "要执行的 JavaScript 代码。使用 return 返回结果。"
            },
        },
        "required": ["code"]
    }
    is_destructive = True
    max_result_chars = 5000

    async def execute(self, code: str, **kwargs) -> str:
        manager = get_browser_manager()
        page = await manager.get_page()

        def _sync():
            try:
                result = page.run_js(code)
                result_str = str(result) if result is not None else "(undefined/null)"
                # JS 执行可能修改 DOM，标记缓存为脏
                manager.cache_mark_dirty()
                return (
                    f"[JS执行成功]\n"
                    f"  返回值类型: {type(result).__name__}\n"
                    f"  返回值: {result_str}"
                )
            except Exception as e:
                return f"[JS执行失败] {e}"

        try:
            return await asyncio.to_thread(_sync)
        except asyncio.CancelledError:
            return "[JS执行取消] 操作被中断。"


class WaitUserTool(BaseTool):
    """人工干预工具：暂停 Agent，等待用户在浏览器中完成操作后回车继续"""

    name = "wait_user"
    description = (
        "当你遇到需要用户手动干预的情况（登录、验证码、2FA 等）时使用此工具。"
        "它会暂停自动化流程，用户在浏览器完成操作并按回车后继续。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "告知用户需要做什么"}
        },
        "required": ["message"]
    }
    is_destructive = False
    max_result_chars = 500
    required_trust_level = TrustLevel.READONLY

    async def execute(self, message: str = "", **kwargs) -> str:
        manager = get_browser_manager()
        page = await manager.get_page()
        
        print("\n" + "!" * 60)
        print("  [WAIT] 人工干预请求")
        print(f"  Agent 消息: {message}")
        print("!" * 60)
        
        try:
            await asyncio.to_thread(input, "\n[完成手动操作后，请按回车键以继续自动化流程]...")
        except asyncio.CancelledError:
            return "[人工干预取消] 操作被中断。"
        
        # 尝试获取页面状态，如果断连则自动重连
        try:
            page, reconnected = await manager.get_page_with_reconnect()
            url = page.url
            title = page.title
            
            reconnect_notice = ""
            if reconnected:
                reconnect_notice = (
                    "\n⚠️ 注意: 浏览器连接已断开并自动重连。"
                    "当前页面可能不是你期望的页面，请使用 navigate 导航到目标 URL。\n"
                )
            
            return (
                f"[人工干预完成] 用户已确认操作完成。{reconnect_notice}\n"
                f"  当前最新页面: {title}\n"
                f"  当前最新 URL: {url}\n"
                f"👉 【极端重要】: 由于用户刚刚接管了浏览器并可能发生了页面跳转，你现在处于**全新的页面状态**！"
                f"此时你记忆中的所有 DOM 选择器大概率已经失效！\n"
                f"接下来的【第一步】，你必须优先使用 `extract_text` 重新审视当前页面的基础文本，或者用 `screenshot` 截取最新快照，或者用 `run_js` 获取目标区域，"
                f"**坚决禁止**盲目沿用被拦截前的选择器去无脑操作（否则必定陷入死循环重试）。"
            )
        except Exception as e:
            return f"[人工干预完成] 但获取页面状态失败: {e}。请使用 navigate 重新加载页面。"


def get_all_browser_tools() -> list[BaseTool]:
    return [
        NavigateTool(),
        ClickElementTool(),
        ExtractTextTool(),
        ScreenshotTool(),
        ScrollPageTool(),
        FillFormTool(),
        RunJSTool(),
        WaitUserTool(),
    ]
