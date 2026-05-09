import sys

# Windows 控制台 UTF-8 修复 — 子包级别保险(直接 import 子模块也能生效)
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
