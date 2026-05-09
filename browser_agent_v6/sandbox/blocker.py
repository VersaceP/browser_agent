"""Import 拦截器 — 在 worker 启动时安装,把禁用的网络库换成抛异常的 stub。

这是 v5 的 "禁止用 requests" 从 prompt 提示升级到运行时强制 — 修了
task_1777979388 的根因(coding agent 用 requests.get() 绕过 browser)。

注意:第三方库(如 DrissionPage)内部合法使用 requests 时不应被拦。
拦截只针对"业务代码"(<sandbox> exec 的 LLM 代码 + 非白名单模块)。
"""
import inspect
import sys
from importlib.abc import MetaPathFinder
from typing import List, Tuple


class BlockedImportError(ImportError):
    """LLM 代码 import 了禁用模块"""


# 白名单 caller 前缀 — 这些库内部 import 网络栈视为合法,放行
# 它们对 LLM 不可见(LLM 不知道这些路径),不构成攻击面
_TRUSTED_CALLERS: Tuple[str, ...] = (
    "DrissionPage",         # 浏览器自动化 — 我们就靠它
    "websocket",            # CDP WebSocket
    "websockets",
    "anthropic",            # 主进程 LLM 客户端(以防 worker 偶然加载)
    "openai",
    "tiktoken",
    "pandas",               # 数据处理可能内部用 urllib3
    "numpy",
)


def _is_trusted_caller() -> bool:
    """检查调用栈,有任何 trusted module 在 frame 里即放行。

    LLM 直接 import → caller 是 <sandbox> module(__name__='__sandbox__')→ 不放行
    DrissionPage 内部 import → caller frame 的 __name__ 以 'DrissionPage' 开头 → 放行
    """
    # frame 0 = _is_trusted_caller, 1 = find_spec, 2+ = importlib internals + 真实 caller
    for frame_info in inspect.stack()[2:]:
        mod_name = frame_info.frame.f_globals.get("__name__", "")
        if not mod_name:
            continue
        # 跳过 importlib 内部
        if mod_name.startswith(("importlib.", "_frozen_importlib")):
            continue
        # 命中可信库 → 放行
        if any(mod_name.startswith(prefix) for prefix in _TRUSTED_CALLERS):
            return True
        # 命中 sandbox 业务代码 → 拦截
        if mod_name in ("__sandbox__", "__main__"):
            return False
        # 其他模块(标准库等)继续往上看
    # 走完整个栈都没命中,默认拦截
    return False


class _BlockedFinder(MetaPathFinder):
    """sys.meta_path finder,优先级最高。

    匹配到禁用模块时:
    - caller 是可信库 → 返回 None(让默认 finder 处理,正常加载)
    - caller 是业务代码 → raise BlockedImportError
    """

    def __init__(self, blocked: List[str]):
        # 同时拦截顶层和子模块:'urllib' 也拦 'urllib.request'
        self.blocked = set(blocked)

    def find_spec(self, fullname, path=None, target=None):
        top = fullname.split(".", 1)[0]
        if top in self.blocked or fullname in self.blocked:
            if _is_trusted_caller():
                return None  # 放行给默认 finder
            raise BlockedImportError(
                f"import '{fullname}' blocked by sandbox. "
                f"Reason: network access must go through browser helpers "
                f"(goto/click/js), NOT requests/httpx/urllib."
            )
        return None  # 非禁用模块,让默认 finder 处理


def install(blocked: List[str]) -> None:
    """安装拦截器到 sys.meta_path 最前。

    必须在任何业务 import 之前调用。后续任何 import 命中黑名单都会
    raise BlockedImportError(继承 ImportError,LLM 写 try/except ImportError 也能捕获)。
    """
    # 先清掉 sys.modules 里已加载的禁用模块,防止用 importlib.reload 绕过
    for name in list(sys.modules):
        top = name.split(".", 1)[0]
        if top in blocked or name in blocked:
            del sys.modules[name]

    sys.meta_path.insert(0, _BlockedFinder(blocked))


# ────────────────────────
# 自检 — python -m sandbox.blocker
# ──────────────────────────

if __name__ == "__main__":
    install(["requests", "httpx", "aiohttp", "urllib", "socket"])
    print("[blocker] 已安装拦截:requests/httpx/aiohttp/urllib/socket")

    for mod in ("requests", "httpx", "urllib.request", "socket"):
        try:
            __import__(mod)
            print(f"  ❌ {mod} import 未被拦截")
        except BlockedImportError as e:
            print(f"  ✅ {mod}: {e}")

    # 正常 import 不应受影响
    import json, re, math
    print(f"  ✅ 标准库 json/re/math 仍可用 (json={json.__name__})")
