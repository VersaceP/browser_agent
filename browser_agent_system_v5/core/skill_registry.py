"""
skill_registry.py — 技能注册表和管理系统

负责加载、解析、管理和选择浏览器自动化技能文件。
技能文件采用 YAML frontmatter + Markdown 内容格式。
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

import yaml


@dataclass
class Skill:
    """
    技能数据模型。
    
    表示一个浏览器自动化技能，包含元数据和内容。
    技能文件格式：YAML frontmatter + Markdown 内容。
    
    Attributes:
        name: 技能唯一标识符（kebab-case）
        version: 语义化版本号（MAJOR.MINOR.PATCH）
        target_websites: 目标网站域名列表
        keywords: 关键词列表，用于任务匹配
        description: 技能描述
        author: 作者信息
        content: Markdown 格式的技能内容
        file_path: 技能文件路径（用于调试）
    """
    name: str
    version: str
    target_websites: List[str]
    description: str
    content: str
    keywords: List[str] = field(default_factory=list)
    author: str = ""
    file_path: str = ""
    
    def __post_init__(self):
        """验证必需字段"""
        if not self.name:
            raise ValueError("Skill name is required")
        if not self.version:
            raise ValueError("Skill version is required")
        if not self.target_websites:
            raise ValueError("Skill target_websites is required")
        if not self.description:
            raise ValueError("Skill description is required")
        if not self.content:
            raise ValueError("Skill content is required")


class SkillParseError(Exception):
    """技能文件解析错误"""
    pass


def parse_skill_file(file_path: str) -> Skill:
    """
    解析技能文件（YAML frontmatter + Markdown 内容）。
    
    :param file_path: 技能文件路径
    :return: Skill 对象
    :raises SkillParseError: 解析失败时抛出
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        raise SkillParseError(f"Failed to read file {file_path}: {e}")
    
    # 检查文件大小（最大 10KB）
    if len(content.encode('utf-8')) > 10 * 1024:
        raise SkillParseError(f"File {file_path} exceeds 10KB size limit")
    
    # 解析 YAML frontmatter
    # 格式: ---\n<yaml content>\n---\n<markdown content>
    frontmatter_pattern = r'^---\s*\n(.*?)\n---\s*\n(.*)$'
    match = re.match(frontmatter_pattern, content, re.DOTALL)
    
    if not match:
        raise SkillParseError(f"File {file_path} does not contain valid YAML frontmatter")
    
    yaml_content = match.group(1)
    markdown_content = match.group(2).strip()
    
    # 解析 YAML
    try:
        metadata = yaml.safe_load(yaml_content)
    except yaml.YAMLError as e:
        raise SkillParseError(f"Invalid YAML in {file_path}: {e}")
    
    if not isinstance(metadata, dict):
        raise SkillParseError(f"YAML frontmatter in {file_path} must be a dictionary")
    
    # 验证必需字段
    required_fields = ['name', 'version', 'target_websites', 'description']
    for field_name in required_fields:
        if field_name not in metadata:
            raise SkillParseError(f"Missing required field '{field_name}' in {file_path}")
    
    # 验证 target_websites 是列表
    if not isinstance(metadata['target_websites'], list):
        raise SkillParseError(f"Field 'target_websites' must be a list in {file_path}")
    
    # 验证内容不为空
    if not markdown_content:
        raise SkillParseError(f"Skill content is empty in {file_path}")
    
    # 验证内容长度（最大 2000 tokens，粗略估计：1 token ≈ 4 字符）
    estimated_tokens = len(markdown_content) / 4
    if estimated_tokens > 2000:
        raise SkillParseError(f"Skill content in {file_path} exceeds 2000 tokens (estimated: {estimated_tokens:.0f})")
    
    # 创建 Skill 对象
    try:
        skill = Skill(
            name=metadata['name'],
            version=metadata['version'],
            target_websites=metadata['target_websites'],
            description=metadata['description'],
            content=markdown_content,
            keywords=metadata.get('keywords', []),
            author=metadata.get('author', ''),
            file_path=file_path,
        )
        return skill
    except (ValueError, TypeError) as e:
        raise SkillParseError(f"Failed to create Skill object from {file_path}: {e}")


class SkillRegistry:
    """
    技能注册表。
    
    负责加载、管理和选择浏览器自动化技能。
    
    Attributes:
        base_dir: 技能仓库基础目录
        _skills: 技能注册表 {skill_name: Skill}
        _load_errors: 加载失败的文件及错误信息
    """
    
    def __init__(self, base_dir: str):
        """
        初始化技能注册表。
        
        :param base_dir: 技能仓库目录路径（相对或绝对路径）
        """
        self.base_dir = Path(base_dir)
        self._skills: Dict[str, Skill] = {}
        self._load_errors: Dict[str, str] = {}
    
    def load_all(self) -> Dict[str, any]:
        """
        加载所有技能文件。
        
        扫描 base_dir 目录，解析所有 .md 文件（除了系统技能）。
        系统技能（以 _ 开头）不会被自动注入，但会被加载用于验证。
        
        :return: 加载结果统计 {
            'loaded': int,  # 成功加载的技能数量
            'failed': int,  # 加载失败的文件数量
            'errors': Dict[str, str]  # 失败文件及错误信息
        }
        """
        self._skills.clear()
        self._load_errors.clear()
        
        if not self.base_dir.exists():
            print(f"[SkillRegistry] ⚠️ 技能目录不存在: {self.base_dir}")
            return {'loaded': 0, 'failed': 0, 'errors': {}}
        
        # 扫描所有 .md 文件
        skill_files = list(self.base_dir.glob('*.md'))
        
        for file_path in skill_files:
            try:
                skill = parse_skill_file(str(file_path))
                self._skills[skill.name] = skill
                print(f"[SkillRegistry] ✅ 加载技能: {skill.name} v{skill.version}")
            except SkillParseError as e:
                self._load_errors[file_path.name] = str(e)
                print(f"[SkillRegistry] ❌ 加载失败: {file_path.name} - {e}")
            except Exception as e:
                self._load_errors[file_path.name] = f"Unexpected error: {e}"
                print(f"[SkillRegistry] ❌ 加载失败: {file_path.name} - {e}")
        
        result = {
            'loaded': len(self._skills),
            'failed': len(self._load_errors),
            'errors': self._load_errors.copy()
        }
        
        print(f"[SkillRegistry] 📊 加载完成: {result['loaded']} 个技能, {result['failed']} 个失败")
        return result
    
    def reload(self) -> Dict[str, any]:
        """
        重新加载所有技能（热重载）。
        
        :return: 加载结果统计
        """
        print("[SkillRegistry] 🔄 重新加载所有技能...")
        return self.load_all()
    
    def get_skill(self, name: str) -> Optional[Skill]:
        """
        根据名称获取技能。
        
        :param name: 技能名称
        :return: Skill 对象，如果不存在返回 None
        """
        return self._skills.get(name)
    
    def list_skills(self) -> List[str]:
        """
        列出所有已加载的技能名称。
        
        :return: 技能名称列表
        """
        return list(self._skills.keys())
    
    def _extract_domain(self, url: str) -> Optional[str]:
        """
        从 URL 提取域名。
        
        :param url: URL 字符串
        :return: 域名（如 'jd.com'），提取失败返回 None
        """
        try:
            parsed = urlparse(url)
            domain = parsed.netloc or parsed.path
            # 移除 www. 前缀
            if domain.startswith('www.'):
                domain = domain[4:]
            return domain if domain else None
        except Exception:
            return None
    
    def _match_domain(self, domain: str, target_websites: List[str]) -> bool:
        """
        检查域名是否匹配目标网站列表。
        
        支持精确匹配和子域名匹配。
        例如: 'search.jd.com' 匹配 'jd.com'
        
        :param domain: 待匹配的域名
        :param target_websites: 目标网站列表
        :return: 是否匹配
        """
        for target in target_websites:
            if domain == target or domain.endswith('.' + target):
                return True
        return False
    
    def select_skills(
        self, 
        task: str, 
        explicit_skills: Optional[List[str]] = None
    ) -> List[Skill]:
        """
        根据任务描述选择相关技能。
        
        选择策略：
        1. 如果提供 explicit_skills，只返回这些技能（忽略任务描述）
        2. 从任务描述中提取 URL，匹配 target_websites
        3. 从任务描述中提取关键词，匹配 keywords（不区分大小写）
        4. 返回所有匹配的技能，按名称排序
        5. 限制总内容长度不超过 8000 tokens
        
        系统技能（以 _ 开头）不会被自动选择。
        
        :param task: 任务描述
        :param explicit_skills: 显式指定的技能名称列表
        :return: 匹配的技能列表
        """
        # 策略 1: 显式指定技能
        if explicit_skills:
            selected = []
            for name in explicit_skills:
                skill = self.get_skill(name)
                if skill:
                    selected.append(skill)
                else:
                    print(f"[SkillRegistry] ⚠️ 显式指定的技能不存在: {name}")
            return selected
        
        matched_skills = []
        
        # 策略 2: URL 域名匹配
        # 提取任务中的所有 URL
        url_pattern = r'https?://[^\s<>"{}|\\^`\[\]]+'
        urls = re.findall(url_pattern, task)
        
        for url in urls:
            domain = self._extract_domain(url)
            if domain:
                for skill in self._skills.values():
                    # 跳过系统技能
                    if skill.name.startswith('_'):
                        continue
                    
                    if self._match_domain(domain, skill.target_websites):
                        if skill not in matched_skills:
                            matched_skills.append(skill)
                            print(f"[SkillRegistry] 🎯 URL 匹配: {skill.name} (域名: {domain})")
        
        # 策略 3: 关键词匹配
        task_lower = task.lower()
        for skill in self._skills.values():
            # 跳过系统技能
            if skill.name.startswith('_'):
                continue
            
            # 跳过已匹配的技能
            if skill in matched_skills:
                continue
            
            # 检查关键词
            for keyword in skill.keywords:
                if keyword.lower() in task_lower:
                    matched_skills.append(skill)
                    print(f"[SkillRegistry] 🎯 关键词匹配: {skill.name} (关键词: {keyword})")
                    break
        
        # 按名称排序
        matched_skills.sort(key=lambda s: s.name)
        
        # 限制总内容长度（8000 tokens ≈ 32000 字符）
        total_chars = sum(len(s.content) for s in matched_skills)
        if total_chars > 32000:
            print(f"[SkillRegistry] ⚠️ 技能内容过长 ({total_chars} 字符)，进行截断...")
            # 简单策略：按顺序保留技能直到达到限制
            filtered_skills = []
            current_chars = 0
            for skill in matched_skills:
                if current_chars + len(skill.content) <= 32000:
                    filtered_skills.append(skill)
                    current_chars += len(skill.content)
                else:
                    print(f"[SkillRegistry] ⚠️ 跳过技能: {skill.name} (超出长度限制)")
            matched_skills = filtered_skills
        
        print(f"[SkillRegistry] 📋 选择了 {len(matched_skills)} 个技能: {[s.name for s in matched_skills]}")
        return matched_skills
