"""Browser Agent v6 - 融合 tool-call + code-as-tool 的精简多 agent 系统。"""
import sys

__version__ = "0.6.0"


# Windows 控制台默认 cp936/cp1252,无法 encode emoji 和中文混排 —
# 强制 stdout/stderr 走 UTF-8(errors=replace 防止罕见字符再爆)
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
