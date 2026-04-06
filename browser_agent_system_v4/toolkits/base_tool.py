"""
base_tool.py — 工具契约基类

V4 核心设计：所有工具必须继承 BaseTool 并声明自己的"契约"：
- name: 工具名称（LLM 调用时使用）
- description: 功能描述（LLM 理解用）
- input_schema: JSON Schema 定义输入参数
- is_destructive: 是否有破坏性副作用
- max_result_chars: 单次输出最大字符数（超限触发第一级压缩落盘）
- required_trust_level: 所需最低信任等级

safe_execute() 封装了自动截断逻辑，确保海量 DOM/JSON 不会直接撑爆上下文。
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from core.agent_definition import TrustLevel


class BaseTool(ABC):
    """
    工具契约基类 — 所有工具的强制性接口。
    
    子类必须实现:
    - name, description, input_schema 属性
    - execute(**kwargs) 方法
    
    框架自动提供:
    - safe_execute() — 带输出截断和溢出落盘的安全执行入口
    - to_schema() — 导出为 Anthropic tool schema 格式
    """

    # ── 子类必须重写的属性 ──
    name: str = ""
    description: str = ""
    input_schema: Dict[str, Any] = {}

    # ── 契约声明（子类可选重写） ──
    is_destructive: bool = False                    # 是否有破坏性副作用
    max_result_chars: int = 3000                    # 单次输出字符上限
    required_trust_level: TrustLevel = TrustLevel.WRITE  # 所需最低信任等级

    @abstractmethod
    async def execute(self, **kwargs) -> str:
        """
        工具的核心执行逻辑。子类必须实现。
        
        :return: 工具执行结果的字符串表示
        """
        ...

    async def safe_execute(self, worktree_path: str = "", task_id: str = "", **kwargs) -> str:
        """
        安全执行入口：自动截断超限输出并落盘。
        
        这是 V4 两级压缩管道的第一级入口：
        1. 调用子类的 execute() 获取原始结果
        2. 如果结果超过 max_result_chars，将完整内容写入 WorkTree
        3. 返回截断摘要 + 文件引用路径
        
        :param worktree_path: WorkTree 沙箱路径（用于溢出落盘）
        :param task_id: 任务 ID（用于文件命名）
        :return: 安全截断后的结果字符串
        """
        try:
            result = await self.execute(**kwargs)
        except Exception as e:
            return f"[工具执行错误] {self.name}: {e}"

        # 检查是否需要截断落盘
        if len(result) > self.max_result_chars and worktree_path:
            from core.worktree import WorkTreeManager
            import time

            # 生成唯一文件名
            spill_filename = f"spill_{self.name}_{int(time.time())}.txt"
            wt = WorkTreeManager()
            saved_path = wt.save_spilled_data(task_id, spill_filename, result)

            # 返回截断摘要
            truncated = result[:self.max_result_chars]
            return (
                f"{truncated}\n\n"
                f"...[输出已截断，完整内容({len(result)}字符)已保存到: {spill_filename}]\n"
                f"[可使用 read_file 工具读取完整内容]"
            )

        return result

    def to_schema(self) -> Dict[str, Any]:
        """
        导出为 Anthropic Messages API 的 tool schema 格式。
        
        返回格式:
        {
            "name": "tool_name",
            "description": "tool description",
            "input_schema": {
                "type": "object",
                "properties": {...},
                "required": [...]
            }
        }
        """
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }
