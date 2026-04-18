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
from typing import Any, Dict

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
    max_result_chars: int = 1500                    # 单次输出字符上限
    required_trust_level: TrustLevel = TrustLevel.WRITE  # 所需最低信任等级

    @abstractmethod
    async def execute(self, **kwargs) -> str:
        """
        工具的核心执行逻辑。子类必须实现。
        
        :return: 工具执行结果的字符串表示
        """
        ...

    async def safe_execute(self, _worktree_path: str = "", _session_id: str = "", _agent_type: str = "", _context: Any = None, **kwargs) -> str:
        """
        安全执行入口：自动截断超限输出并落盘。
        
        这是 V4 两级压缩管道的第一级入口：
        1. 调用子类的 execute() 获取原始结果
        2. 如果结果超过 max_result_chars，将完整内容写入 WorkTree
        3. 返回截断摘要 + 结构化溢出文件索引（待读清单）
        
        :param _worktree_path: WorkTree 沙箱路径（用于判断落盘权限）
        :param _session_id: 会话 ID（用于文件物理隔离）
        :param _agent_type: 调用者 Agent 类型（用于隔离层级）
        :param _context: 当前 Agent 的 TeammateContext（可选，用于进度板等跨工具状态传递）
        :return: 安全截断后的结果字符串
        """
        try:
            result = await self.execute(_worktree_path=_worktree_path, session_id=_session_id, _context=_context, **kwargs)
        except Exception as e:
            return f"[工具执行错误] {self.name}: {e}"

        # 检查是否需要截断落盘
        if len(result) > self.max_result_chars and _worktree_path and _session_id:
            import time
            from pathlib import Path

            # 生成唯一文件名（纳秒时间戳 + 4位随机后缀，防止同纳秒碰撞）
            import random
            spill_filename = f"spill_{self.name}_{time.time_ns()}_{random.randint(0, 9999):04d}.txt"
            
            # 使用上下文注入的 worktree_path 直接落盘，不再实例化 WorkTreeManager
            data_dir = Path(_worktree_path) / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            saved_path = data_dir / spill_filename
            
            saved_path.write_text(result, encoding="utf-8")

            # 构建结构化溢出索引摘要
            truncated = result[:self.max_result_chars]
            total_chars = len(result)
            line_count = result.count('\n') + 1

            # 提取内容摘要（取截断部分的首尾行作为概览）
            lines = result.split('\n')
            preview_lines = []
            if len(lines) <= 6:
                preview_lines = lines
            else:
                # 首部3行 + 尾部3行
                preview_lines = lines[:3] + ["..."] + lines[-3:]
            content_preview = '\n'.join(preview_lines)[:500]

            # 收集 data/ 目录下已有的所有溢出文件，构建待读清单
            pending_files = []
            try:
                for f in sorted(data_dir.glob("spill_*.txt")):
                    pending_files.append(f.name)
            except Exception:
                pass

            # 构建待读清单
            file_index_str = ""
            if pending_files:
                file_index_str = "\n".join(f"  - {fn}" for fn in pending_files)

            return (
                f"{truncated}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📤 [输出已截断] 完整内容已自动落盘\n"
                f"  总字符数: {total_chars}  |  总行数: {line_count}  |  截断至: {self.max_result_chars} 字符\n"
                f"  落盘文件: data/{spill_filename}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📋 内容概览:\n{content_preview}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📂 待读溢出文件清单 (共 {len(pending_files)} 个):\n{file_index_str}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⚠️ 你必须使用 read_file 读取上述文件获取完整数据，不要重复执行相同操作！"
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
