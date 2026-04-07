"""
browser_tools.py — 基于 DrissionPage 的真实浏览器工具集

V4 核心浏览器操作工具，使用 DrissionPage 替换了原有的 Playwright，
以获取更原生的防反爬脱敏能力（绕过 Cloudflare 等）：
- NavigateTool: 打开指定 URL
- ClickElementTool: 点击页面元素
- ExtractTextTool: 提取页面/元素文本
- ScreenshotTool: 截图保存到 WorkTree
- ScrollPageTool: 滚动页面
- FillFormTool: 填充表单字段

内部通过 DPBrowserManager 管理浏览器生命周期。
注意：DrissionPage 是同步库，我们在工具内部使用 asyncio.to_thread() 
保障纯函数执行流在此层的异步安全性。
"""

import asyncio
import time
from pathlib import Path
from typing import Any, Dict, Optional

from toolkits.base_tool import BaseTool
from core.agent_definition import TrustLevel


class DPBrowserManager:
    """
    DrissionPage 浏览器实例管理器。
    
    负责浏览器（CDP）连接的生命周期管理：
    - 延迟初始化（首次使用时才启动浏览器）
    - 单例模式（同一任务复用同一浏览器实例）
    - 安全关闭（确保无头/有头浏览器进程已被正确回收）
    """

    def __init__(self, profile_id: str = "default"):
        self.profile_id = profile_id
        self._page = None
        self._lock = asyncio.Lock()

    async def get_page(self) -> Any:
        """
        获取当前活跃的 ChromiumPage 实例。
        如果浏览器尚未启动，自动初始化。
        """
        async with self._lock:
            if self._page is None:
                await asyncio.to_thread(self._launch)
            return self._page

    def _launch(self) -> None:
        """启动 DrissionPage 浏览器实例 (带增强指纹与代理)"""
        try:
            from DrissionPage import ChromiumPage, ChromiumOptions
        except ImportError:
            raise ImportError("未安装 DrissionPage，请执行 pip install DrissionPage")

        co = ChromiumOptions()
        
        # 1. 配置代理 (Clash/VPN 端口)
        proxy_server = "http://127.0.0.1:7890"
        co.set_proxy(proxy_server)
        
        # 2. 配置用户数据目录 (Session 隔离与指纹模拟)
        profile_path = Path("./browser_profiles") / self.profile_id
        profile_path.mkdir(parents=True, exist_ok=True)
        co.set_user_data_path(str(profile_path.absolute()))

        # 3. 常规风控优化
        co.headless(False)  # 有头模式对 Reddit 等网站更友好
        co.mute(True)
        co.set_argument('--no-sandbox')
        co.set_argument('--disable-gpu')

        self._page = ChromiumPage(co)
        print(f"[BrowserManager] 🥷 浏览器扩展模式启动 | 代理: {proxy_server} | Profile: {self.profile_id}")

    async def close(self) -> None:
        """安全关闭浏览器实例并回收资源"""
        if self._page:
            try:
                await asyncio.to_thread(self._page.quit)
            except Exception as e:
                print(f"[BrowserManager] ⚠️ 关闭时发生轻微异常: {e}")
            self._page = None
            print("[BrowserManager] 🔒 浏览器实例已安全关闭")

    @property
    def is_active(self) -> bool:
        return self._page is not None


# ── 全局浏览器管理器单例 ──
_browser_manager = DPBrowserManager()


def get_browser_manager() -> DPBrowserManager:
    """获取全局浏览器管理器单例"""
    return _browser_manager


# ══════════════════════════════════════════════════════════════
#  以下是 6 个基于 BaseTool 契约的浏览器工具实现
# ══════════════════════════════════════════════════════════════


class NavigateTool(BaseTool):
    """导航到指定 URL"""

    name = "navigate"
    description = (
        "打开浏览器并导航到指定的 URL。返回页面标题和当前 URL。"
        "仅支持 http/https 协议。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "要导航到的目标 URL（必须以 http:// 或 https:// 开头）"
            }
        },
        "required": ["url"]
    }
    is_destructive = False
    max_result_chars = 500

    async def execute(self, url: str, **kwargs) -> str:
        page = await get_browser_manager().get_page()

        def _sync():
            try:
                page.get(url, timeout=30)
                # 等待 DOM 加载，DrissionPage 默认 get 后基本已稳定
                return (
                    f"[导航成功] 页面已加载\n"
                    f"  标题: {page.title}\n"
                    f"  URL: {page.url}"
                )
            except Exception as e:
                return f"[导航失败] {url}: {e}"

        return await asyncio.to_thread(_sync)


class ClickElementTool(BaseTool):
    """点击页面上的指定元素"""

    name = "click_element"
    description = (
        "点击页面上匹配指定 CSS 选择器的元素。"
        "点击后会等待页面响应。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "要点击的元素的 CSS 选择器（如 'button.submit', '#login-btn'）"
            }
        },
        "required": ["selector"]
    }
    is_destructive = True
    max_result_chars = 500

    async def execute(self, selector: str, **kwargs) -> str:
        page = await get_browser_manager().get_page()

        def _sync():
            try:
                ele = page.ele(selector)
                if not ele:
                    return f"[点击失败] 选择器 '{selector}': 未找到匹配元素"
                
                ele.click(by_js=False) # 优先物理点击以符合真实操作
                return (
                    f"[点击成功] 已点击元素: {selector}\n"
                    f"  当前页面: {page.title}\n"
                    f"  当前 URL: {page.url}"
                )
            except Exception as e:
                return f"[点击失败] 选择器 '{selector}': {e}"

        return await asyncio.to_thread(_sync)


class ExtractTextTool(BaseTool):
    """提取页面或指定元素的文本内容"""

    name = "extract_text"
    description = (
        "提取页面上指定元素的文本内容。"
        "如果不指定 selector，则提取整个页面的 body 文本。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "要提取文本的 CSS/XPath 选择器。留空则提取整个 body 文本"
            }
        },
        "required": []
    }
    is_destructive = False
    max_result_chars = 5000

    async def execute(self, selector: str = "", **kwargs) -> str:
        page = await get_browser_manager().get_page()

        def _sync():
            try:
                if selector:
                    ele = page.ele(selector)
                    if not ele:
                        return f"[提取失败] 未找到匹配选择器的元素: {selector}"
                    text = ele.text
                else:
                    ele = page.ele('body')
                    text = ele.text if ele else ""

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

        return await asyncio.to_thread(_sync)


class ScreenshotTool(BaseTool):
    """截取当前页面的屏幕截图"""

    name = "screenshot"
    description = (
        "截取当前浏览器页面的屏幕截图并保存到 WorkTree。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "截图的文件名（不含路径，如 'page_screenshot.png'）"
            },
            "full_page": {
                "type": "boolean",
                "description": "是否截取整个页面。默认为 false"
            }
        },
        "required": ["filename"]
    }
    is_destructive = False
    max_result_chars = 500

    async def execute(self, filename: str = "screenshot.png", full_page: bool = False,
                      _worktree_path: str = "", **kwargs) -> str:
        page = await get_browser_manager().get_page()

        def _sync():
            try:
                if _worktree_path:
                    save_dir = Path(_worktree_path) / "screenshots"
                    save_dir.mkdir(parents=True, exist_ok=True)
                    filepath = save_dir / filename
                else:
                    filepath = Path(filename)

                # DrissionPage 命名参数和 playwright 略有不同
                page.get_screenshot(path=str(filepath), full_page=full_page)
                return (
                    f"[截图成功]\n"
                    f"  页面: {page.title}\n"
                    f"  保存路径: {filepath}\n"
                    f"  全页面模式: {full_page}"
                )
            except Exception as e:
                return f"[截图失败] {e}"

        return await asyncio.to_thread(_sync)


class ScrollPageTool(BaseTool):
    """滚动页面"""

    name = "scroll_page"
    description = (
        "滚动当前页面。direction 指定方向（down/up），"
        "amount 指定像素距离（默认 500）。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "direction": {
                "type": "string",
                "description": "滚动方向: 'down' 向下, 'up' 向上",
                "enum": ["down", "up"]
            },
            "amount": {
                "type": "integer",
                "description": "滚动像素数。默认 500"
            }
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
                scroll_pos = page.run_js("return window.scrollY;")
                page_height = page.run_js("return document.body.scrollHeight;")

                return (
                    f"[滚动成功] 方向: {direction}, 距离: {amount}px\n"
                    f"  当前位置: {scroll_pos}px / 总高度: {page_height}px"
                )
            except Exception as e:
                return f"[滚动失败] {e}"

        return await asyncio.to_thread(_sync)


class FillFormTool(BaseTool):
    """填充表单字段"""

    name = "fill_form"
    description = (
        "在指定的表单字段中填入文本内容。"
        "使用选择器定位输入框。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "表单字段的 CSS/XPath 选择器"
            },
            "value": {
                "type": "string",
                "description": "要填入的文本内容"
            }
        },
        "required": ["selector", "value"]
    }
    is_destructive = True
    max_result_chars = 300

    async def execute(self, selector: str, value: str, **kwargs) -> str:
        page = await get_browser_manager().get_page()

        def _sync():
            try:
                ele = page.ele(selector)
                if not ele:
                    return f"[填写失败] 选择器 '{selector}': 未找到匹配元素"
                ele.input(value, clear=True)
                return (
                    f"[填写成功] 已在 '{selector}' 中填入内容\n"
                    f"  内容长度: {len(value)} 字符"
                )
            except Exception as e:
                return f"[填写失败] 选择器 '{selector}': {e}"

        return await asyncio.to_thread(_sync)


def get_all_browser_tools() -> list[BaseTool]:
    """获取所有浏览器工具实例的列表"""
    return [
        NavigateTool(),
        ClickElementTool(),
        ExtractTextTool(),
        ScreenshotTool(),
        ScrollPageTool(),
        FillFormTool(),
    ]
