"""
code_tools.py — Python 沙箱代码执行工具

V4 重构自 portable_sandbox_tools.py，包装为 BaseTool 子类。
在 WorkTree 隔离沙箱内通过子进程执行 Python 脚本。
required_trust_level = TrustLevel.ADMIN，仅 Coding Agent 可调用。
"""

import sys
import subprocess
from pathlib import Path
from typing import Any, Dict

from toolkits.base_tool import BaseTool
from core.agent_definition import TrustLevel


class RunPythonTool(BaseTool):
    """在隔离沙箱中执行 Python 脚本"""

    name = "run_python"
    description = (
        "在你的 WorkTree 沙箱中执行指定的 Python 脚本文件。"
        "脚本必须已通过 write_file 工具写入到沙箱内。"
        "执行环境的工作目录被锁定为 WorkTree 根目录。"
        "有 30 秒超时限制，超时将被强制终止。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "script_name": {
                "type": "string",
                "description": "要执行的 Python 脚本文件名（相对于 WorkTree 根目录，如 'process.py'）"
            }
        },
        "required": ["script_name"]
    }
    is_destructive = True
    max_result_chars = 5000
    required_trust_level = TrustLevel.ADMIN  # 仅 Coding Agent 可调用

    async def execute(self, script_name: str = "",
                      _worktree_path: str = "", **kwargs) -> str:
        if not _worktree_path:
            return "[安全错误] 未检测到 WorkTree 执行环境，拒绝代码执行"

        worktree = Path(_worktree_path)
        script_path = (worktree / script_name).resolve()

        # 路径穿越防御
        if not str(script_path).startswith(str(worktree.resolve())):
            return f"[安全拦截] 路径越权！严禁执行沙箱外的脚本: {script_name}"

        if not script_path.exists():
            return f"[执行失败] 脚本不存在: {script_name}。请先用 write_file 写入脚本。"

        if not script_name.endswith(".py"):
            return f"[安全拦截] 仅允许执行 .py 文件，拒绝: {script_name}"

        # 使用当前 Python 解释器在沙箱内执行
        interpreter = sys.executable

        try:
            result = subprocess.run(
                [interpreter, str(script_path)],
                cwd=str(worktree),      # 锁定工作目录为 WorkTree
                capture_output=True,
                text=True,
                timeout=30,             # 30 秒超时保护
                env={                   # 最小化环境变量
                    "PATH": "",
                    "PYTHONPATH": "",
                    "HOME": str(worktree),
                }
            )

            output = f"[Python 沙箱执行结果] 退出码: {result.returncode}\n"
            if result.stdout:
                output += f"--- STDOUT ---\n{result.stdout}\n"
            if result.stderr:
                output += f"--- STDERR ---\n{result.stderr}\n"
            if not result.stdout and not result.stderr:
                output += "(无输出)\n"

            return output

        except subprocess.TimeoutExpired:
            return (
                "[超时终止] 脚本执行超过 30 秒限制，已被强制终止。"
                "请优化代码效率或拆分为更小的任务。"
            )
        except Exception as e:
            return f"[执行崩溃] 沙箱执行异常: {e}"


def get_all_code_tools() -> list[BaseTool]:
    """获取所有代码执行工具实例的列表"""
    return [
        RunPythonTool(),
    ]
