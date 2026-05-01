"""
tool_registry.py — 全局工具池化路由中心

V4 核心设计：统一的工具注册、过滤、调度入口。
- register(): 注册 BaseTool 实例
- filter_tools(): 按 Agent 的 AgentDefinition 过滤可用工具
- get_schemas(): 导出 Anthropic tool schema 格式
- dispatch(): 统一执行入口，内置权限校验 + 输出截断
"""

from typing import Any, Dict, List, Optional

from toolkits.base_tool import BaseTool
from core.agent_definition import AgentDefinition, TrustLevel


class ToolRegistry:
    """
    全局工具池 — 所有 BaseTool 实例的注册表和调度中心。
    
    职责：
    1. 管理所有工具的注册
    2. 根据 AgentDefinition 的白名单/黑名单/信任等级过滤可用工具
    3. 导出 Anthropic Messages API 的 tool schemas
    4. 统一调度工具执行（权限校验 + 输出截断）
    """

    def __init__(self):
        self._tools: Dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """注册一个工具到全局池"""
        if tool.name in self._tools:
            print(f"[ToolRegistry] ⚠️ 工具 '{tool.name}' 已存在，覆盖注册")
        self._tools[tool.name] = tool

    def register_many(self, tools: List[BaseTool]) -> None:
        """批量注册工具"""
        for tool in tools:
            self.register(tool)

    def get_tool(self, name: str) -> Optional[BaseTool]:
        """按名称获取工具实例"""
        return self._tools.get(name)

    def filter_tools(self, agent_def: AgentDefinition) -> List[BaseTool]:
        """
        根据 AgentDefinition 过滤出该 Agent 可用的工具集。
        
        过滤规则（按优先级）：
        1. 黑名单优先：disallowed_tools 中的工具一律排除
        2. 白名单：如果 allowed_tools 非空，只保留白名单中的工具
        3. 信任等级：工具的 required_trust_level 不能高于 Agent 的 trust_level
        4. 只读保护：is_read_only 的 Agent 无法使用 is_destructive 的工具
        """
        filtered = []

        for name, tool in self._tools.items():
            # 规则 1：黑名单排除
            if name in agent_def.disallowed_tools:
                continue

            # 规则 2：白名单过滤（空白名单 = 允许所有）
            if agent_def.allowed_tools and name not in agent_def.allowed_tools:
                continue

            # 规则 3：信任等级检查
            if tool.required_trust_level > agent_def.trust_level:
                continue

            # 规则 4：只读保护
            if agent_def.is_read_only and tool.is_destructive:
                continue

            filtered.append(tool)

        return filtered

    def get_schemas(self, agent_def: AgentDefinition) -> List[Dict[str, Any]]:
        """
        导出该 Agent 可用工具的 Anthropic tool schema 列表。
        用于注入到 LLM 调用的 tools 参数。
        """
        tools = self.filter_tools(agent_def)
        return [tool.to_schema() for tool in tools]

    async def dispatch(
        self,
        tool_name: str,
        params: Dict[str, Any],
        agent_def: AgentDefinition,
        worktree_path: str = "",
        session_id: str = "",
        context: Any = None,
    ) -> str:
        """
        统一工具执行入口。
        
        执行流程：
        1. 查找工具是否存在
        2. 权限校验（信任等级 + 只读保护）
        3. 注入系统参数（worktree_path）
        4. 调用 safe_execute()（自带输出截断）
        5. 返回结果
        
        :param tool_name: 工具名称
        :param params: LLM 传入的工具参数
        :param agent_def: 当前 Agent 的定义
        :param worktree_path: WorkTree 沙箱路径
        :param session_id: 会话 ID
        :param context: 当前 Agent 的 TeammateContext（可选，用于进度板等跨工具状态传递）
        :return: 工具执行结果字符串
        """
        # 1. 查找工具
        tool = self.get_tool(tool_name)
        if not tool:
            return f"[系统错误] 工具 '{tool_name}' 不存在于注册表中"

        # 1.5 LLM 工具参数 JSON 解析失败的短路分支：
        # OpenAIProvider 在 JSON 解析失败时会塞入 _parse_error / _raw_arguments 哨兵，这里直接返回有意义的错误信息，避免下游 ** 展开触发 TypeError
        if isinstance(params, dict) and "_parse_error" in params:
            raw = str(params.get("_raw_arguments", ""))[:500]
            err = params["_parse_error"]
            return (
                f"[参数解析失败] LLM 返回的工具参数 JSON 无法解析: {err}。"
                f"原始参数片段: {raw}"
            )

        # 2. 权限校验
        if tool.required_trust_level > agent_def.trust_level:
            return (
                f"[权限拒绝] 工具 '{tool_name}' 需要 {tool.required_trust_level.name} 权限，"
                f"但当前 Agent '{agent_def.agent_type}' 仅有 {agent_def.trust_level.name} 权限"
            )

        if agent_def.is_read_only and tool.is_destructive:
            return (
                f"[只读拦截] Agent '{agent_def.agent_type}' 为只读模式，"
                f"禁止使用破坏性工具 '{tool_name}'"
            )

        # 3. 注入系统参数
        params.pop("_worktree_path", None)
        params.pop("_session_id", None)
        params.pop("_agent_type", None)
        params.pop("_context", None)

        # 4. 调用 safe_execute（自带截断保护）
        result = await tool.safe_execute(
            _worktree_path=worktree_path,
            _session_id=session_id,
            _agent_type=agent_def.agent_type,
            _context=context,
            **params
        )

        return result

    def list_tools(self) -> List[str]:
        """列出所有已注册的工具名称"""
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)
