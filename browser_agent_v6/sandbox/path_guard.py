"""路径白名单 sanitizer — 防止 LLM 代码读写沙箱外文件。

用法:
    from sandbox.path_guard import PathGuard
    guard = PathGuard(worktree="/path/to/wt", shared_dirs=["/path/to/shared"])
    abs_path = guard.resolve("data/x.json", mode="write")    # OK
    guard.resolve("../../../etc/passwd", mode="write")       # raises PathEscapeError

关键设计 — 用 Path.relative_to 做语义级校验,不是字符串前缀比较
(防御 'session_1' 被前缀混淆为 'session_1_fake' 的攻击)。
"""
from pathlib import Path
from typing import List, Optional, Sequence


class PathEscapeError(PermissionError):
    """LLM 代码尝试访问沙箱外路径"""


class PathGuard:
    """单 session 的路径白名单。

    Args:
        worktree: 主工作区(读写都允许)
        shared_dirs: 额外允许的目录(通常是 session 共享区,只允许读;写另说)
        readonly_extra: 仅允许读取的额外目录(如 sibling worktree)
    """

    def __init__(
        self,
        worktree: str,
        shared_dirs: Optional[Sequence[str]] = None,
        readonly_extra: Optional[Sequence[str]] = None,
    ):
        self.worktree = Path(worktree).resolve()
        self.worktree.mkdir(parents=True, exist_ok=True)
        self.shared = [Path(p).resolve() for p in (shared_dirs or [])]
        self.readonly_extra = [Path(p).resolve() for p in (readonly_extra or [])]

    def resolve(self, relative_or_abs: str, mode: str = "read") -> Path:
        """规整路径到沙箱内的绝对路径。

        Args:
            relative_or_abs: LLM 提供的路径(相对则按 worktree 解析,绝对则原样)
            mode: 'read' 或 'write'

        Returns:
            Path 对象(绝对)

        Raises:
            PathEscapeError: 解析后逃出所有白名单
            ValueError: mode 非法
        """
        if mode not in ("read", "write"):
            raise ValueError(f"mode 必须是 'read' 或 'write',got {mode!r}")
        if not isinstance(relative_or_abs, str) or not relative_or_abs.strip():
            raise ValueError(f"路径必须是非空字符串,got {relative_or_abs!r}")

        p = Path(relative_or_abs)
        # 相对路径按 worktree 解析,绝对路径原样
        target = (self.worktree / p).resolve() if not p.is_absolute() else p.resolve()

        # 写操作:只允许 worktree 内
        if mode == "write":
            if not self._is_within(target, self.worktree):
                # 写到 shared/ 也允许(跨 agent 数据交付)
                if not any(self._is_within(target, s) for s in self.shared):
                    raise PathEscapeError(
                        f"[L3 路径拦截] 不允许写到沙箱外: {relative_or_abs!r} → {target}"
                    )
            return target

        # 读操作:worktree + shared + readonly_extra 都允许
        all_ok = [self.worktree, *self.shared, *self.readonly_extra]
        if not any(self._is_within(target, base) for base in all_ok):
            raise PathEscapeError(
                f"[L3 路径拦截] 不允许读沙箱外: {relative_or_abs!r} → {target}; "
                f"允许的根: {[str(b) for b in all_ok]}"
            )
        return target

    @staticmethod
    def _is_within(target: Path, base: Path) -> bool:
        """语义级路径包含校验,防字符串前缀混淆"""
        try:
            target.relative_to(base)
            return True
        except ValueError:
            return False


# ──────────────────────────
# 自检
# ──────────────────────────

if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as wt, tempfile.TemporaryDirectory() as shared:
        guard = PathGuard(worktree=wt, shared_dirs=[shared])
        print(f"[path_guard] worktree={wt}")
        print(f"[path_guard] shared={shared}")

        # 合法
        print(f"  ✅ resolve('data/x.json', write) → {guard.resolve('data/x.json', 'write')}")
        print(f"  ✅ resolve('result.txt', read) → {guard.resolve('result.txt', 'read')}")

        # 路径穿越
        for bad in ("../../etc/passwd", "/etc/passwd", "..\\..\\windows\\system.ini"):
            try:
                guard.resolve(bad, "write")
                print(f"  ❌ {bad!r} 未被拦截")
            except PathEscapeError as e:
                print(f"  ✅ blocked write {bad!r}")

        # 字符串前缀混淆攻击 — wt + '_fake' 不应被当作 wt 内
        fake = wt + "_fake"
        Path(fake).mkdir(parents=True, exist_ok=True)
        try:
            guard.resolve(fake + "/x.txt", "write")
            print("  ❌ 字符串前缀混淆未被拦截!")
        except PathEscapeError:
            print(f"  ✅ blocked prefix-confusion {fake}/x.txt")
        Path(fake).rmdir()
