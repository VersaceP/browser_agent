"""
hook_registry.py — 5 大生命周期 Hook 事件总线

V4 核心设计：通过 Hook 机制实现执行流的可扩展拦截。
5 个核心事件：
- SESSION_START: 会话启动（用于资源分配，如拉起浏览器环境）
- PRE_TOOL_EXECUTE: 工具执行前（用于安全校验，InputSanitizer 挂载点）
- POST_TOOL_EXECUTE: 工具执行后（用于日志记录、数据清洗）
- PRE_COMPACT: 上下文压缩前（用于自定义压缩逻辑）
- PRE_TURN_COMPLETE: Turn 结束前（用于触发 Verification Agent）

Hook 处理器可以返回 allow/block/modify 三种动作来控制执行流。
"""

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional


class HookEvent(Enum):
    """5 个核心生命周期事件"""
    SESSION_START = "session_start"          # 会话启动
    PRE_TOOL_EXECUTE = "pre_tool_execute"    # 工具执行前
    POST_TOOL_EXECUTE = "post_tool_execute"  # 工具执行后
    PRE_COMPACT = "pre_compact"              # 上下文压缩前
    PRE_TURN_COMPLETE = "pre_turn_complete"  # Turn 结束前


class HookAction(Enum):
    """Hook 处理器的返回动作"""
    ALLOW = "allow"    # 放行，继续执行
    BLOCK = "block"    # 拦截，中止当前操作
    MODIFY = "modify"  # 修改 payload 后继续执行


@dataclass
class HookResult:
    """
    Hook 处理器的返回结果。
    
    - action: 决定执行流的走向（allow/block/modify）
    - reason: 拦截/修改的原因说明
    - updated_payload: 修改后的 payload（仅 action=MODIFY 时有效）
    """
    action: HookAction = HookAction.ALLOW
    reason: str = ""
    updated_payload: Optional[Dict[str, Any]] = None


# Hook 处理器的类型签名：接收 payload 字典，返回 HookResult
HookHandler = Callable[[Dict[str, Any]], Coroutine[Any, Any, HookResult]]


class HookRegistry:
    """
    生命周期拦截器事件总线。
    
    使用方式：
    1. 在初始化阶段注册 Hook 处理器:
       hook_registry.register(HookEvent.PRE_TOOL_EXECUTE, sanitizer_hook)
    
    2. 在执行引擎中触发事件:
       result = await hook_registry.emit(HookEvent.PRE_TOOL_EXECUTE, payload)
       if result.action == HookAction.BLOCK:
           return result.reason  # 中止工具执行
    
    同一事件可注册多个处理器，按注册顺序依次执行。
    任何一个处理器返回 BLOCK 则立即中止后续处理器。
    """

    def __init__(self):
        self._handlers: Dict[HookEvent, List[HookHandler]] = {
            event: [] for event in HookEvent
        }

    def register(self, event: HookEvent, handler: HookHandler) -> None:
        """
        注册 Hook 处理器。
        
        :param event: 要监听的生命周期事件
        :param handler: 异步处理函数，签名: async def handler(payload: dict) -> HookResult
        """
        self._handlers[event].append(handler)

    async def emit(self, event: HookEvent, payload: Dict[str, Any] = None) -> HookResult:
        """
        触发生命周期事件，依次执行所有注册的处理器。
        
        执行规则：
        - 按注册顺序依次执行
        - 任何处理器返回 BLOCK → 立即停止，返回该结果
        - 处理器返回 MODIFY → 更新 payload 后传递给后续处理器
        - 所有处理器都返回 ALLOW → 最终返回 ALLOW
        
        :param event: 触发的事件类型
        :param payload: 传递给处理器的数据负载
        :return: 最终的 Hook 处理结果
        """
        if payload is None:
            payload = {}

        handlers = self._handlers.get(event, [])
        if not handlers:
            return HookResult(action=HookAction.ALLOW)

        current_payload = payload.copy()

        for handler in handlers:
            try:
                result = await handler(current_payload)
            except Exception as e:
                print(f"[HookRegistry] ❌ Hook 处理器异常 ({event.value}): {e}")
                continue

            if result.action == HookAction.BLOCK:
                return result

            if result.action == HookAction.MODIFY and result.updated_payload:
                current_payload = result.updated_payload

        return HookResult(action=HookAction.ALLOW, updated_payload=current_payload)

    def list_handlers(self, event: HookEvent) -> int:
        """返回指定事件的处理器数量"""
        return len(self._handlers.get(event, []))

    def clear(self, event: Optional[HookEvent] = None) -> None:
        """清除指定事件（或所有事件）的处理器"""
        if event:
            self._handlers[event] = []
        else:
            for e in HookEvent:
                self._handlers[e] = []
