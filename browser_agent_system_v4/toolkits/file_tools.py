"""
file_tools.py — 文件读写工具

V4 重构：将原有的裸函数包装为 BaseTool 子类，
统一纳入 ToolRegistry 的权限校验和输出截断管道。
"""

from pathlib import Path
from typing import Any, Dict

from toolkits.base_tool import BaseTool
from core.agent_definition import TrustLevel


class WriteFileTool(BaseTool):
    """在 WorkTree 沙箱内写入文件"""

    name = "write_file"
    description = (
        "在你的专属 WorkTree 沙箱中创建或覆盖一个文件。"
        "必须提供文件名和内容。路径会被限制在沙箱内，禁止越界。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "文件名（相对于 WorkTree 根目录，如 'data.json' 或 'scripts/process.py'）"
            },
            "content": {
                "type": "string",
                "description": "要写入的文件内容"
            }
        },
        "required": ["filename", "content"]
    }
    is_destructive = True
    max_result_chars = 500
    required_trust_level = TrustLevel.WRITE

    async def execute(self, filename: str = "", content: str = "",
                      _worktree_path: str = "", **kwargs) -> str:
        if not _worktree_path:
            return "[安全错误] 未检测到 WorkTree 路径，拒绝写入操作"

        worktree = Path(_worktree_path)
        target_path = (worktree / filename).resolve()

        # 路径穿越防御
        if not str(target_path).startswith(str(worktree.resolve())):
            return f"[安全拦截] 路径越权！'{filename}' 解析到沙箱外: {target_path}"

        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with open(target_path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"[写入成功] 文件已保存: {target_path.name} ({len(content)} 字符)"
        except Exception as e:
            return f"[写入失败] {e}"


class ReadFileTool(BaseTool):
    """读取 WorkTree 沙箱内的文件"""

    name = "read_file"
    description = (
        "读取你的 WorkTree 沙箱中指定文件的全部内容。"
        "路径会被限制在沙箱内，禁止越界读取。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "文件名（相对于 WorkTree 根目录）"
            }
        },
        "required": ["filename"]
    }
    is_destructive = False
    max_result_chars = 5000  # 读取允许较大输出
    required_trust_level = TrustLevel.READONLY  # Verification Agent 也可以使用

    async def execute(self, filename: str = "",
                      _worktree_path: str = "", **kwargs) -> str:
        if not _worktree_path:
            return "[安全错误] 未检测到 WorkTree 路径，拒绝读取操作"

        worktree = Path(_worktree_path)
        target_path = (worktree / filename).resolve()

        # 路径穿越防御
        if not str(target_path).startswith(str(worktree.resolve())):
            return f"[安全拦截] 路径越权！'{filename}' 解析到沙箱外"

        if not target_path.exists():
            return f"[读取失败] 文件不存在: {filename}"

        try:
            with open(target_path, "r", encoding="utf-8") as f:
                content = f.read()
            return (
                f"[读取成功] 文件: {target_path.name} ({len(content)} 字符)\n"
                f"---内容开始---\n"
                f"{content}\n"
                f"---内容结束---"
            )
        except Exception as e:
            return f"[读取失败] {e}"


def get_all_file_tools() -> list[BaseTool]:
    """获取所有文件工具实例的列表"""
    return [
        WriteFileTool(),
        ReadFileTool(),
    ]
