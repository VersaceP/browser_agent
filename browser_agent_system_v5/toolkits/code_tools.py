"""
code_tools.py — Python 沙箱代码执行工具

V4 重构自 portable_sandbox_tools.py，包装为 BaseTool 子类。
在 WorkTree 隔离沙箱内通过子进程执行 Python 脚本。
required_trust_level = TrustLevel.ADMIN，仅 Coding Agent 可调用。
"""

import os
import re
import sys
import subprocess
from pathlib import Path


from toolkits.base_tool import BaseTool
from core.agent_definition import TrustLevel

# ── 超时策略常量 ──
_MIN_TIMEOUT = 30       # 最小超时（秒）
_MAX_TIMEOUT = 120      # 最大超时（秒）
_DEFAULT_TIMEOUT = 30   # 默认超时（秒）
# 每千字符脚本增加的超时秒数
_TIMEOUT_PER_KCHAR = 5
# 检测大文件目录时，每个 MB 增加的超时秒数
_TIMEOUT_PER_MB = 10


def _estimate_timeout(script_content: str, worktree: Path) -> int:
    """
    根据脚本大小和数据量动态估算超时时间。

    策略：
    1. 基础超时 30 秒
    2. 脚本每增加 1000 字符 +5 秒（大脚本通常包含更多逻辑）
    3. WorkTree/data/ 目录下文件总量每 1MB +10 秒（大数据文件需要更长处理时间）
    4. 结果被夹在 [_MIN_TIMEOUT, _MAX_TIMEOUT] 区间内
    """
    timeout = _DEFAULT_TIMEOUT

    # 脚本大小因子
    script_chars = len(script_content)
    timeout += (script_chars // 1000) * _TIMEOUT_PER_KCHAR

    # 数据量因子：扫描 data/ 目录
    data_dir = worktree / "data"
    if data_dir.exists():
        try:
            total_size = sum(f.stat().st_size for f in data_dir.rglob("*") if f.is_file())
            total_mb = total_size / (1024 * 1024)
            timeout += int(total_mb) * _TIMEOUT_PER_MB
        except Exception:
            pass

    return max(_MIN_TIMEOUT, min(timeout, _MAX_TIMEOUT))


# ── 网络限制策略 ──
# 完全禁止的网络库（无合法用途于沙箱内）
# urllib 也完全禁止：urlopen("file:///etc/passwd") 可读取任意系统文件，绕过 read_file 的沙箱限制
_BLOCKED_NETWORK_PATTERNS = [
    r"\bimport\s+requests\b",
    r"\bfrom\s+requests\b",
    r"\bimport\s+httpx\b",
    r"\bfrom\s+httpx\b",
    r"\bimport\s+aiohttp\b",
    r"\bfrom\s+aiohttp\b",
    r"\bimport\s+scrapy\b",
    r"\bfrom\s+scrapy\b",
    r"\bimport\s+selenium\b",
    r"\bfrom\s+selenium\b",
    r"\bimport\s+urllib\b",
    r"\bfrom\s+urllib\b",
]


class RunPythonTool(BaseTool):
    """在隔离沙箱中执行 Python 脚本（支持动态超时）"""

    name = "run_python"
    description = (
        "在你的 WorkTree 沙箱中执行指定的 Python 脚本文件。"
        "脚本必须已通过 write_file 工具写入到沙箱内。"
        "执行环境的工作目录被锁定为 WorkTree 根目录。"
        "超时限制为 30-120 秒（根据脚本大小和数据量自动调整）："
        "简单脚本 30 秒；大脚本或处理大数据文件时最长 120 秒。"
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

        # ── 读取脚本内容 ──
        try:
            script_content = script_path.read_text(encoding="utf-8")
        except Exception as e:
            return f"[读取失败] 无法读取脚本内容: {e}"

        # ── 安全审查：禁止所有网络库（含 urllib） ──
        for pattern in _BLOCKED_NETWORK_PATTERNS:
            if re.search(pattern, script_content):
                return (
                    f"[L5 职能越权拦截] 脚本 '{script_name}' 中检测到网络请求库 ({pattern})！\n"
                    f"Coding Agent 禁止使用 Python 网络库（含 urllib：file:// 可读取任意系统文件，绕过沙箱限制）。\n"
                    f"如需读取本地文件，请使用 open() 函数。\n"
                    f"如需获取网页内容，请通过 Lead Agent 派遣 Browser Agent 执行。"
                )

        # ── 动态超时估算 ──
        timeout = _estimate_timeout(script_content, worktree)

        # 使用当前 Python 解释器在沙箱内执行
        interpreter = sys.executable

        try:
            result = subprocess.run(
                [interpreter, str(script_path)],
                cwd=str(worktree),      # 锁定工作目录为 WorkTree
                capture_output=True,
                text=True,
                timeout=timeout,        # 动态超时保护
                env={                   # 最小化环境变量
                    "PATH": os.environ.get("PATH", ""),
                    "PYTHONPATH": "",
                    "HOME": str(worktree),
                }
            )

            output = f"[Python 沙箱执行结果] 退出码: {result.returncode}  超时限制: {timeout}s\n"
            if result.stdout:
                output += f"--- STDOUT ---\n{result.stdout}\n"
            if result.stderr:
                output += f"--- STDERR ---\n{result.stderr}\n"
            if not result.stdout and not result.stderr:
                output += "(无输出)\n"

            return output

        except subprocess.TimeoutExpired:
            return (
                f"[超时终止] 脚本执行超过 {timeout} 秒限制，已被强制终止。"
                "请优化代码效率或拆分为更小的任务。"
            )
        except Exception as e:
            return f"[执行崩溃] 沙箱执行异常: {e}"


def get_all_code_tools() -> list[BaseTool]:
    """获取所有代码执行工具实例的列表"""
    return [
        RunPythonTool(),
    ]
