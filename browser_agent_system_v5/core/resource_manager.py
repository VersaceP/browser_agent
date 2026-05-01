"""
resource_manager.py — 浏览器环境资源管理器

管理浏览器实例的生命周期：
- 测试模式（当前）: 直接 playwright.chromium.launch()
- 生产模式（未来）: 调用 ShadowPilot API 获取 ws_endpoint 后 connect_over_cdp()

与 HookRegistry 配合：
- SESSION_START 事件触发浏览器环境分配
- SESSION_END 时回收浏览器资源
"""

import asyncio
from typing import Any, Dict, Optional

from toolkits.browser_tools import DPBrowserManager, get_browser_manager


class ResourceManager:
    """
    浏览器资源管理器。
    
    职责：
    1. 管理浏览器实例的分配与回收
    2. 维护 profile_id → 浏览器实例的映射
    3. 提供资源使用统计
    """

    def __init__(self):
        # profile_id → DPBrowserManager 的映射
        self._browsers: Dict[str, DPBrowserManager] = {}
        self._lock = asyncio.Lock()

    async def acquire_browser(self, profile_id: str = "default") -> DPBrowserManager:
        """
        获取或创建浏览器实例。

        复用策略（重要）：
        - 浏览器实例在同一进程生命周期内跨任务复用，避免冷启动开销。
        - 单个 browser/verification agent 完成任务后【不释放】实例，
          下一个使用浏览器的 agent 直接继承登录状态、Cookie、已开页签。
        - 仅在实例失活（is_active=False，如崩溃 / 远端断开）时才创建新实例，
          并在覆盖前对旧实例做一次 best-effort close 释放残留进程/句柄。
        - 释放只发生在进程退出时（resource_manager.release_all()）。

        测试模式（当前实现）：profile_id 仅作为标识符，复用同一 Playwright 实例。
        生产模式（未来 ShadowPilot）：根据 profile_id 调用 API 拉起远程环境后 CDP 连接。

        :param profile_id: 浏览器环境 ID
        :return: 浏览器管理器实例
        """
        async with self._lock:
            if profile_id in self._browsers:
                manager = self._browsers[profile_id]
                if manager.is_active:
                    print(f"[ResourceManager] ♻️ 复用已有浏览器实例: {profile_id}")
                    return manager
                try:
                    await manager.close()
                    print(f"[ResourceManager] 🧹 旧浏览器实例已失活，已清理: {profile_id}")
                except Exception as e:
                    print(f"[ResourceManager] ⚠️ 旧浏览器实例 close 失败（已忽略）: {e}")

            manager = DPBrowserManager(profile_id=profile_id)
            await manager.get_page()
            self._browsers[profile_id] = manager

            print(f"[ResourceManager] 🚀 浏览器实例已分配: {profile_id}")
            return manager

    async def release_browser(self, profile_id: str = "default") -> None:
        """
        释放浏览器实例。
        
        :param profile_id: 浏览器环境 ID
        """
        async with self._lock:
            if profile_id in self._browsers:
                manager = self._browsers.pop(profile_id)
                await manager.close()
                print(f"[ResourceManager] 🔒 浏览器实例已回收: {profile_id}")

    async def release_all(self) -> None:
        """释放所有浏览器实例"""
        async with self._lock:
            for pid, manager in list(self._browsers.items()):
                try:
                    await manager.close()
                except Exception as e:
                    print(f"[ResourceManager] ⚠️ 回收 {pid} 时出错: {e}")
            self._browsers.clear()
            print("[ResourceManager] 🔒 所有浏览器实例已回收")

    def get_stats(self) -> Dict[str, Any]:
        """获取资源使用统计"""
        return {
            "active_browsers": len(self._browsers),
            "profile_ids": list(self._browsers.keys()),
        }
