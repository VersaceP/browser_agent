"""SkillRegistry — 站点策略库,按需加载(不预注入大段 prompt)。

设计 vs v5:
- v5:spawn 时按 task 关键词预注入整段 markdown 到 system prompt(可能 8000 tokens)
- v6:goto() 返回值带 available_skills 列表,LLM 看到再 read_skill 拉具体内容

文件格式(YAML frontmatter + Markdown):
    ---
    name: taaft_aitools
    target_websites:
      - theresanaiforthat.com
    keywords:
      - taaft
      - trending ai
    description: TAAFT 站点抓取策略 — URL 模式 / 详情页 selector / 反爬注意事项
    ---

    # TAAFT 抓取要点
    ...
"""
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


@dataclass
class Skill:
    """站点策略数据"""
    name: str
    description: str
    content: str
    target_websites: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    file_path: str = ""

    @property
    def estimated_tokens(self) -> int:
        return len(self.content) // 4


class SkillParseError(Exception):
    pass


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def parse_skill_file(path: Path) -> Skill:
    """解析单个 skill 文件"""
    raw = path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        raise SkillParseError(f"{path.name}: 缺 YAML frontmatter (---...---)")

    fm_text, body = m.group(1), m.group(2).strip()

    # 解析 frontmatter — 优先 yaml 库,没有则用极简 KV 解析
    meta = {}
    if _HAS_YAML:
        try:
            meta = yaml.safe_load(fm_text) or {}
        except yaml.YAMLError as e:
            raise SkillParseError(f"{path.name}: YAML 解析失败 — {e}") from e
    else:
        # 极简 KV / list 解析(应付简单格式)
        current_key = None
        for line in fm_text.splitlines():
            if not line.strip() or line.strip().startswith("#"):
                continue
            if line.startswith("  -"):
                if current_key:
                    meta.setdefault(current_key, []).append(line.split("-", 1)[1].strip())
            elif ":" in line:
                k, v = line.split(":", 1)
                k, v = k.strip(), v.strip()
                if v:
                    meta[k] = v
                else:
                    current_key = k
                    meta[k] = []

    # 字段校验
    name = meta.get("name", path.stem)
    description = meta.get("description", "")
    if not description:
        raise SkillParseError(f"{path.name}: frontmatter 必须含 description")
    if not body:
        raise SkillParseError(f"{path.name}: 正文为空")

    targets = meta.get("target_websites") or []
    if isinstance(targets, str):
        targets = [targets]
    keywords = meta.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [keywords]

    return Skill(
        name=str(name),
        description=str(description),
        content=body,
        target_websites=[str(t) for t in targets],
        keywords=[str(k).lower() for k in keywords],
        file_path=str(path),
    )


class SkillRegistry:
    """加载 + 匹配 skill 的注册表"""

    def __init__(self, skills_dir: Optional[Path] = None):
        self.skills_dir = Path(skills_dir) if skills_dir else (
            Path(__file__).resolve().parents[1] / "skills" / "browser"
        )
        self._skills: Dict[str, Skill] = {}

    def load_all(self) -> Dict[str, int]:
        """扫描 skills_dir,加载所有 .md(跳过 _ 前缀的模板文件)。

        Returns: {"loaded": N, "failed": N, "errors": {filename: msg}}
        """
        loaded = 0
        failed = 0
        errors: Dict[str, str] = {}
        if not self.skills_dir.exists():
            return {"loaded": 0, "failed": 0, "errors": {"_dir_missing": str(self.skills_dir)}}
        for path in sorted(self.skills_dir.glob("*.md")):
            if path.name.startswith("_"):
                continue  # 跳过 _template.md 等
            try:
                skill = parse_skill_file(path)
                self._skills[skill.name] = skill
                loaded += 1
            except SkillParseError as e:
                failed += 1
                errors[path.name] = str(e)
        return {"loaded": loaded, "failed": failed, "errors": errors}

    def list_all(self) -> List[Skill]:
        return list(self._skills.values())

    def get(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    # ─────────── 匹配 ───────────

    def match_by_url(self, url: str) -> List[Skill]:
        """url 匹配:host 包含 target_websites 任一项"""
        if not url:
            return []
        host = (urlparse(url).hostname or "").lower()
        if not host:
            return []
        out = []
        for s in self._skills.values():
            for t in s.target_websites:
                t = t.lower().lstrip("*.")  # 容忍 *.example.com
                if t in host:
                    out.append(s)
                    break
        return out

    def match_by_keywords(self, text: str) -> List[Skill]:
        """task 文本匹配 keywords"""
        if not text:
            return []
        low = text.lower()
        out = []
        for s in self._skills.values():
            if any(k in low for k in s.keywords):
                out.append(s)
        return out

    def match(self, url: str = "", task: str = "") -> List[Skill]:
        """url + task 联合匹配,去重保留顺序"""
        seen = set()
        out = []
        for s in self.match_by_url(url) + self.match_by_keywords(task):
            if s.name not in seen:
                seen.add(s.name)
                out.append(s)
        return out


# ──────────── 进程全局单例(让 helpers 通过它访问)────────────

_REGISTRY: Optional[SkillRegistry] = None


def set_registry(reg: SkillRegistry) -> None:
    """主进程启动 / worker 子进程启动时 inject"""
    global _REGISTRY
    _REGISTRY = reg


def get_registry() -> Optional[SkillRegistry]:
    return _REGISTRY


def auto_load(skills_dir: Optional[Path] = None) -> Dict[str, int]:
    """便捷:创建 + load + 设全局,一步到位。

    Returns: load 统计
    """
    reg = SkillRegistry(skills_dir=skills_dir)
    result = reg.load_all()
    set_registry(reg)
    return result
