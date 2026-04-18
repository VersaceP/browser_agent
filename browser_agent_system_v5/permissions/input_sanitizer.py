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

    # 使用 is_relative_to() 做语义级路径比较，防御"字符串前缀混淆"攻击。
    # 正确地验证 target 是否在 root 的语义子树下，不存在 session_1_fake 被判定为以 session_1 开头的漏洞。
    try:
        target.relative_to(root)
    except ValueError:
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


# ══════════════════════════════════════════════════════════════
#  L4 支付安全拦截
# ══════════════════════════════════════════════════════════════

# 支付相关的 CSS 选择器 / 文本关键词（中英文覆盖）
_PAYMENT_SELECTOR_PATTERNS = [
    # 英文关键词
    r"pay",
    r"checkout",
    r"purchase",
    r"buy[\s\-_]?now",
    r"place[\s\-_]?order",
    r"confirm[\s\-_]?order",
    r"submit[\s\-_]?payment",
    r"complete[\s\-_]?payment",
    r"add[\s\-_]?to[\s\-_]?cart",  # 加入购物车本身不危险，但可选拦截
    # 中文关键词
    r"支付",
    r"付款",
    r"结算",
    r"下单",
    r"确认订单",
    r"立即购买",
    r"提交订单",
]

# 密码 / 敏感信息输入字段相关关键词
_SENSITIVE_FIELD_PATTERNS = [
    # 英文
    r"password",
    r"passwd",
    r"pin[\s\-_]?code",
    r"cvv",
    r"cvc",
    r"card[\s\-_]?number",
    r"credit[\s\-_]?card",
    r"debit[\s\-_]?card",
    r"expir",                # expiry / expiration
    r"security[\s\-_]?code",
    r"payment[\s\-_]?method",
    r"billing",
    # 中文
    r"密码",
    r"支付密码",
    r"交易密码",
    r"银行卡",
    r"信用卡",
    r"卡号",
    r"有效期",
    r"安全码",
    r"验证码",
]


def sanitize_payment_action(tool_name: str, tool_input: dict) -> None:
    """
    支付行为拦截：检测浏览器操作中是否涉及支付/资金相关的敏感动作。

    拦截场景：
    - click_element: 选择器命中支付按钮关键词
    - fill_form: 选择器或填入值命中密码/卡号等敏感字段

    :param tool_name: 工具名称
    :param tool_input: 工具入参字典
    :raises SanitizationError: 检测到支付行为时抛出
    """
    if tool_name == "click_element":
        selector = tool_input.get("selector", "")
        _check_patterns(selector, _PAYMENT_SELECTOR_PATTERNS, "点击支付相关按钮")

    elif tool_name == "fill_form":
        selector = tool_input.get("selector", "")
        value = tool_input.get("value", "")
        # 检测字段名是否涉及敏感信息
        _check_patterns(selector, _SENSITIVE_FIELD_PATTERNS, "向敏感支付字段填写内容")
        # 检测填入的值是否像银行卡号（连续 13-19 位纯数字）
        if re.match(r"^\d{13,19}$", value.replace(" ", "").replace("-", "")):
            raise SanitizationError(
                "[L4 支付安全拦截] 检测到疑似银行卡号输入，资金敏感操作已被系统主动拦截！\n"
                "👉 请勿强行自动化输入。请立即调用 `wait_user` 工具挂起线程，通知用户在打开的浏览器页面中手动完成付款和卡号填写。"
            )


def _check_patterns(text: str, patterns: list, action_desc: str) -> None:
    """对文本进行关键词模式匹配，命中则抛出拦截异常"""
    if not text:
        return
    text_lower = text.lower()
    for pattern in patterns:
        if re.search(pattern, text_lower, re.IGNORECASE):
            raise SanitizationError(
                f"[L4 支付安全拦截] 禁止自动{action_desc}！\n"
                f"触发敏感保护，目标: '{text}'。\n"
                f"👉 涉及资金/账户安全的敏感操作必须由人类手动完成。请立刻调用 `wait_user` 工具，请用户在此页面手动操作并确认。"
            )
