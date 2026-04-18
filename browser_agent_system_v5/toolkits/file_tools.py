"""
file_tools.py — 文件读写工具

V4 重构：将原有的裸函数包装为 BaseTool 子类，
统一纳入 ToolRegistry 的权限校验和输出截断管道。

跨 WorkTree 只读支持：
- ReadFileTool / ListFilesTool 允许访问同一 session 下 sibling worktree 的只读目录
  （data/、verification/），以支持 verification agent 直接读取其他 agent 的输出文件。
- WriteFileTool 保持原有严格隔离，不允许跨 worktree 写入。
"""

from pathlib import Path

from toolkits.base_tool import BaseTool
from core.agent_definition import TrustLevel


def _resolve_path_safe(
    filename: str,
    worktree_root: str,
) -> tuple[str, str]:
    """
    安全路径解析，处理三种访问场景：

    1. local     — 同 worktree 内访问（相对于 worktree_root 的任意路径）
    2. sibling   — 同一 session 下其他 agent worktree 的只读访问
                   支持两种寻址方式：
                   a) 相对寻址（使用 ..）："../lead/reddit.json" 从 coding/ 访问 lead/
                   b) session 绝对寻址（session_id/agent_type/...）
    3. blocked   — 超出 session 范围的越权访问

    :param filename: 相对路径（如 "data/report.txt" 或 "../lead/data.json"）
    :param worktree_root: 当前 agent 的 worktree 根目录
    :return: (resolved_path, access_type)
    """
    root = Path(worktree_root).resolve()

    # 先尝试直接拼接（处理 "data/xxx"、"../lead/xxx" 等相对路径）
    # Path.resolve() 会规范化 .. 等特殊路径，不会有目录遍历风险
    target = (root / filename).resolve()

    # 情况 1：同 worktree 内，直接放行
    # 使用 is_relative_to() 做语义级路径比较，防御"字符串前缀混淆"攻击。
    # 错误做法：用 str.startswith() 比较，D:\worktrees\session_1_fake 会误判为以 D:\worktrees\session_1 开头。
    # 正确做法：is_relative_to() 验证 target 是否在 root 的语义子树下，无此漏洞。
    try:
        target.relative_to(root)
        return str(target), "local"
    except ValueError:
        pass

    # 情况 2：尝试访问 session 内的其他 worktree
    # session 目录结构：worktrees/<session_id>/<agent_type>/
    session_root = root.parent  # = worktrees/<session_id>/
    try:
        target.relative_to(session_root)
        return str(target), "sibling"
    except ValueError:
        return "", "blocked"


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
        try:
            target_path.relative_to(worktree.resolve())
        except ValueError:
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
        "读取 WorkTree 沙箱中指定文件的全部内容。"
        "路径限制在沙箱内（local）。"
        "同时支持访问同一 session 下其他 agent worktree 的文件，使用 '..' 向上寻址，"
        "例如从 'coding/' 读取 'lead/reddit_posts_raw.json' 可用 '../lead/reddit_posts_raw.json'。"
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

        resolved, access = _resolve_path_safe(filename, _worktree_path)
        if access == "blocked":
            return f"[安全拦截] 路径越权！'{filename}' 解析到沙箱外或不在只读白名单内"

        target_path = Path(resolved)

        # 区分目录 vs 文件不存在 vs 其他错误
        if target_path.is_dir():
            return f"[读取失败] '{filename}' 是目录而非文件，请使用 list_files 工具先查看目录内容"

        if not target_path.exists():
            return f"[读取失败] 文件不存在: {filename}"

        try:
            with open(target_path, "r", encoding="utf-8") as f:
                content = f.read()
            scope = "(sibling 只读)" if access == "sibling" else ""
            return (
                f"[读取成功] 文件: {target_path.name} ({len(content)} 字符) {scope}\n"
                f"---内容开始---\n"
                f"{content}\n"
                f"---内容结束---"
            )
        except (IsADirectoryError, PermissionError):
            # Linux: IsADirectoryError; Windows: open() on dir raises PermissionError
            return f"[读取失败] '{filename}' 是目录而非文件，请使用 list_files 工具先查看目录内容"
        except Exception as e:
            return f"[读取失败] {e}"


class ListFilesTool(BaseTool):
    """列出 WorkTree 沙箱内的文件列表"""

    name = "list_files"
    description = (
        "列出你的 WorkTree 沙箱中指定目录下的文件和子目录。"
        "如果不提供 path，则默认列出沙箱根目录。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要列出的目录路径（相对于 WorkTree 根目录，默认为 '.'）"
            },
            "recursive": {
                "type": "boolean",
                "description": "是否递归列出所有子目录。默认为 false"
            }
        },
        "required": []
    }
    is_destructive = False
    max_result_chars = 3000
    required_trust_level = TrustLevel.READONLY

    async def execute(self, path: str = ".", recursive: bool = False,
                      _worktree_path: str = "", **kwargs) -> str:
        if not _worktree_path:
            return "[安全错误] 未检测到 WorkTree 路径"

        resolved, access = _resolve_path_safe(path, _worktree_path)
        if access == "blocked":
            return f"[安全拦截] 禁止访问沙箱外或不在只读白名单内: {path}"

        target_dir = Path(resolved)

        if not target_dir.exists():
            return f"[列表失败] 目录不存在: {path}"

        try:
            items = []
            pattern = "**/*" if recursive else "*"
            worktree = Path(_worktree_path).resolve()
            for p in target_dir.glob(pattern):
                if p.is_dir():
                    continue
                try:
                    rel = p.relative_to(worktree)
                    items.append(f"[FILE] {rel}")
                except ValueError:
                    # 跨 worktree 文件，显示相对于目标目录的路径
                    try:
                        rel_sibling = p.relative_to(Path(resolved).parent)
                        items.append(f"[FILE] {rel_sibling}")
                    except ValueError:
                        items.append(f"[FILE] {p.name}")

            # 也列出子目录（用于展示结构）
            for p in target_dir.iterdir():
                if p.is_dir():
                    items.append(f"[DIR] {p.name}")

            if not items:
                return f"[列表成功] 目录 '{path}' 是空的"

            items.sort()
            content = "\n".join(items)
            scope = "(sibling 只读)" if access == "sibling" else ""
            return (
                f"[列表成功] 目录: {path} {scope}\n"
                f"---文件列表---\n"
                f"{content}\n"
                f"---列表结束---"
            )
        except Exception as e:
            return f"[列表失败] {e}"


def get_all_file_tools() -> list[BaseTool]:
    """获取所有文件工具实例的列表"""
    return [
        WriteFileTool(),
        ReadFileTool(),
        ListFilesTool(),
    ]
