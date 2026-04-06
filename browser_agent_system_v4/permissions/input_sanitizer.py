"""
input_sanitizer.py — L3 安全校验层

挂载于 HookRegistry 的 PRE_TOOL_EXECUTE 钩子，
在工具执行前对输入参数进行安全审查：
- 路径穿越防御（../../../ 攻击）
- URL 协议白名单（仅允许 http/https）
- Shell 命令危险字符拦截
"""

import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse


class SanitizationError(Exception):
    """安全校验失败时抛出的异常"""
    pass


def sanitize_path(path: str, worktree_root: str) -> str:
    """
    路径穿越防御：确保目标路径不会逃出 WorkTree 沙箱。
    
    :param path: 用户/LLM 提供的相对路径
    :param worktree_root: 当前任务的 WorkTree 根目录
    :return: 安全解析后的绝对路径字符串
    :raises SanitizationError: 路径逃逸时抛出
    """
    if not worktree_root:
        raise SanitizationError("[L3 安全拦截] 未检测到 WorkTree 根目录，拒绝路径操作")

    root = Path(worktree_root).resolve()
    target = (root / path).resolve()

    if not str(target).startswith(str(root)):
        raise SanitizationError(
            f"[L3 安全拦截] 路径穿越攻击！目标 '{path}' 解析到沙箱外: {target}"
        )

    return str(target)


def sanitize_url(url: str) -> str:
    """
    URL 协议白名单校验：仅允许 http/https 协议。
    
    拦截 file://、javascript:、data: 等危险协议，
    防止通过浏览器工具读取本地文件系统或执行注入代码。
    
    :param url: 待校验的 URL
    :return: 通过校验的 URL
    :raises SanitizationError: 协议不在白名单时抛出
    """
    allowed_protocols = {"http", "https"}

    try:
        parsed = urlparse(url)
    except Exception:
        raise SanitizationError(f"[L3 安全拦截] 无法解析 URL: {url}")

    if parsed.scheme not in allowed_protocols:
        raise SanitizationError(
            f"[L3 安全拦截] 禁止的 URL 协议 '{parsed.scheme}://'。"
            f"仅允许: {', '.join(allowed_protocols)}"
        )

    if not parsed.netloc:
        raise SanitizationError(f"[L3 安全拦截] URL 缺少有效域名: {url}")

    return url


def sanitize_shell_input(cmd: str) -> str:
    """
    Shell 命令危险字符拦截。
    
    禁止在命令参数中出现管道符、重定向、命令链接等 Shell 元字符，
    防止通过 code_tools 进行命令注入。
    
    :param cmd: 待校验的命令/参数字符串
    :return: 通过校验的字符串
    :raises SanitizationError: 包含危险字符时抛出
    """
    # 危险的 Shell 元字符与关键词
    dangerous_patterns = [
        r"[|;&`$]",           # 管道、分号、后台、反引号、变量展开
        r">{1,2}",            # 输出重定向
        r"<",                 # 输入重定向
        r"\$\(",              # 命令替换 $(...)
        r"rm\s+(-rf?|--)",    # rm 危险命令
        r"chmod\s",           # 权限修改
        r"curl\s",            # 网络请求
        r"wget\s",            # 网络下载
        r"eval\s",            # 动态执行
    ]

    for pattern in dangerous_patterns:
        if re.search(pattern, cmd, re.IGNORECASE):
            raise SanitizationError(
                f"[L3 安全拦截] 命令包含危险字符/关键词: '{cmd}'"
            )

    return cmd
