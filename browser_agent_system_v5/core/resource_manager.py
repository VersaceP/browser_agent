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
        
        测试模式（当前实现）：
        - 直接使用全局 PlaywrightBrowserManager
        - profile_id 仅作为标识符
        
        生产模式（未来 ShadowPilot）：
        - 根据 profile_id 调用 ShadowPilot API 拉起环境
        - 获取 ws_endpoint 后通过 CDP 连接
        
        :param profile_id: 浏览器环境 ID
        :return: 浏览器管理器实例
        """
        async with self._lock:
            if profile_id in self._browsers:
                manager = self._browsers[profile_id]
                if manager.is_active:
                    print(f"[ResourceManager] ♻️ 复用已有浏览器实例: {profile_id}")
                    return manager

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
