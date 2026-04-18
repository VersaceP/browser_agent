# 设计文档：浏览器代理技能注入系统

## 1. 架构概览

### 1.1 系统目标
将通用的浏览器自动化能力与特定网站的知识分离，使得：
- Browser Agent 的 System Prompt 保持精简、通用、可维护
- 特定网站的策略（CSS 选择器、操作序列、反爬虫技巧）外部化为可复用的 Skill 文件
- 支持动态加载和热重载 Skills，无需重启系统

### 1.2 核心设计原则

#### 原则 1：System Prompt 静态化（Prompt Cache 保护）
- **System Prompt = 静态**：Agent 人格 + 安全边界 + 通用能力描述
- **动态上下文 = 可变**：任务描述 + 环境信息 + 注入的 Skills
- **目的**：保持 System Prompt 不变，充分利用 Anthropic Prompt Cache 机制，减少 Token 消耗

#### 原则 2：Skills 作为动态上下文注入
- Skills 通过 `build_dynamic_context()` 注入到第一条 user message
- Skills 类似工具（Tools），是任务相关的额外知识
- 每次任务执行时根据需要选择性注入相关 Skills

#### 原则 3：Hook 驱动的安全验证
- 新增 `PRE_SKILL_INJECT` 和 `POST_SKILL_INJECT` Hook 事件
- 在技能注入前进行安全验证，防止提示词注入攻击
- 使用系统级安全检测 Skill (`_security_validator.md`) 验证第三方 Skills

#### 原则 4：集成而非独立组件
- 不创建独立的 ContextAugmenter 组件
- 直接在 `prompt_builder.py` 中实现技能格式化逻辑
- 保持架构简洁，减少抽象层级

### 1.3 架构图

```
┌─────────────────────────────────────────────────────────────┐
│                     Browser Agent 执行流程                    │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  1. AgentSpawner.spawn(agent_type="browser", task=...)     │
│                          ↓                                  │
│  2. SkillRegistry.select_skills(task) → [skill1, skill2]   │
│                          ↓                                  │
│  3. Hook: PRE_SKILL_INJECT (安全验证)                        │
│                          ↓                                  │
│  4. prompt_builder.format_skills([skill1, skill2])         │
│                          ↓                                  │
│  5. build_dynamic_context(task, skills=formatted_skills)   │
│                          ↓                                  │
│  6. Hook: POST_SKILL_INJECT (审计日志)                       │
│                          ↓                                  │
│  7. execution_loop.execute_turn(context, ...)              │
│     - System Prompt: 静态（可缓存）                          │
│     - First User Message: 任务 + 环境 + Skills              │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. 核心架构决策

### 决策 1：Skills 注入位置
**决策**：通过 `build_dynamic_context()` 将 Skills 注入到第一条 user message

**理由**：
- 保持 System Prompt 静态不变，保护 Prompt Cache
- Skills 是任务相关的动态知识，应该与任务描述一起注入
- 避免破坏 Anthropic 的缓存机制

**实现位置**：`core/prompt_builder.py`

### 决策 2：无独立 ContextAugmenter 组件
**决策**：直接在 `prompt_builder.py` 中实现技能格式化逻辑

**理由**：
- 减少抽象层级，保持代码简洁
- 技能格式化逻辑与 Prompt 构建紧密相关
- 避免过度设计

### 决策 3：Hook 驱动的安全机制
**决策**：新增 `PRE_SKILL_INJECT` 和 `POST_SKILL_INJECT` Hook 事件

**理由**：
- 与现有 Hook 机制一致，保持架构统一
- 支持可扩展的安全验证策略
- 便于审计和日志记录

**新增 Hook 事件**：
- `PRE_SKILL_INJECT`：技能注入前，用于安全验证
- `POST_SKILL_INJECT`：技能注入后，用于审计日志

### 决策 4：安全检测 Skill
**决策**：创建系统级 `_security_validator.md` Skill 用于验证第三方 Skills

**理由**：
- 防止恶意 Skill 文件注入提示词攻击
- 使用 Skill 验证 Skill，保持机制一致
- 系统级 Skill（以 `_` 开头）不会被自动注入，仅用于内部验证

### 决策 5：直接架构迁移
**决策**：不考虑向后兼容性，直接迁移到新架构

**理由**：
- 简化实现，避免维护两套系统
- 精简后的 System Prompt 保留所有通用能力
- 特定网站策略迁移到 Skill 文件后功能不变

---

## 3. 技能文件格式

### 3.1 文件结构
每个 Skill 文件采用 **YAML frontmatter + Markdown 内容** 格式：

```markdown
---
name: jd_ecommerce
version: 1.0.0
target_websites:
  - jd.com
  - search.jd.com
keywords:
  - 京东
  - JD
  - 京东商城
description: 京东电商平台自动化策略
author: system
---

# 京东电商自动化策略

## 搜索功能
- 直接搜索 URL: `https://search.jd.com/Search?keyword={keyword}&enc=utf-8`
- 避免使用搜索框输入，直接构造 URL 更可靠

## 商品详情页
...
```

### 3.2 元数据字段定义

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `name` | string | ✅ | 技能唯一标识符（kebab-case） |
| `version` | string | ✅ | 语义化版本号（MAJOR.MINOR.PATCH） |
| `target_websites` | list[string] | ✅ | 目标网站域名列表 |
| `keywords` | list[string] | ❌ | 关键词列表，用于任务匹配 |
| `description` | string | ✅ | 技能描述 |
| `author` | string | ❌ | 作者信息 |

### 3.3 内容部分规范
- 使用 Markdown 格式编写
- 包含特定网站的 CSS 选择器、操作序列、反爬虫策略
- 可包含 JavaScript 代码片段（用于 `run_js` 工具）
- 内容长度限制：每个 Skill 最多 2000 tokens

### 3.4 文件命名规范
- 普通 Skill：`{name}.md`（如 `jd_ecommerce.md`）
- 系统 Skill：`_{name}.md`（如 `_security_validator.md`，以 `_` 开头）
- 模板文件：`_template.md`

---

## 4. 技能注册与发现

### 4.1 SkillRegistry 类设计
负责技能的加载、管理和选择。

**核心职责**：
- 扫描技能仓库目录
- 解析技能文件元数据和内容
- 维护技能内存注册表
- 根据任务描述选择相关技能
- 支持热重载

**存储位置**：`browser_agent_system_v5/core/skill_registry.py`

### 4.2 技能仓库目录结构
```
browser_agent_system_v5/
└── skills/
    └── browser/
        ├── _template.md              # 技能模板
        ├── _security_validator.md    # 系统安全检测 Skill
        ├── jd_ecommerce.md           # 京东电商 Skill
        └── taobao_ecommerce.md       # 淘宝电商 Skill
```

### 4.3 技能加载流程
1. 系统启动时，`SkillRegistry` 扫描 `skills/browser/` 目录
2. 解析每个 `.md` 文件的 YAML frontmatter
3. 验证必需字段（name, version, target_websites, description）
4. 验证内容部分不为空且长度 ≤ 10KB
5. 将验证通过的技能加载到内存注册表
6. 记录加载失败的技能及原因

### 4.4 热重载机制
- 支持运行时重新加载技能文件
- 提供 `reload()` 方法手动触发重载
- 下次 Agent 生成时自动使用最新版本

---

## 5. 技能选择策略

### 5.1 选择算法
根据任务描述自动选择相关技能，支持以下匹配策略：

#### 策略 1：URL 域名匹配
- 从任务描述中提取 URL
- 提取域名（如 `jd.com`）
- 与技能的 `target_websites` 字段匹配

#### 策略 2：关键词匹配
- 从任务描述中提取关键词
- 与技能的 `keywords` 字段匹配
- 支持中英文关键词（如 "京东" / "JD"）

#### 策略 3：显式指定
- 支持通过参数显式指定技能名称
- 用于测试或特殊场景

### 5.2 多技能注入策略
- 当多个技能匹配时，注入所有匹配的技能
- 按技能名称字母顺序排序
- 限制总注入内容长度（所有技能总和 ≤ 8000 tokens）

### 5.3 无匹配技能处理
- 当没有技能匹配时，Browser Agent 仅使用核心 System Prompt 执行
- 不影响基本功能

---

## 6. 安全机制设计

### 6.1 提示词注入攻击防御

#### 6.1.1 攻击向量分析
恶意 Skill 文件可能包含：
- 覆盖系统提示词的指令（如 "忽略之前的所有指令"）
- 注入恶意工具调用
- 泄露系统内部信息
- 绕过安全边界

#### 6.1.2 防御策略

**策略 1：内容转义**
- 将 Skill 内容包裹在明确的边界标记中
- 使用特殊分隔符标识 Skill 内容区域

**策略 2：长度限制**
- 单个 Skill 文件最大 10KB
- 单个 Skill 内容最多 2000 tokens
- 所有注入 Skills 总和最多 8000 tokens

**策略 3：安全检测 Skill**
- 使用 `_security_validator.md` 系统 Skill 验证第三方 Skills
- 检测可疑指令模式（如 "ignore", "override", "system prompt"）
- 检测异常长度或格式

**策略 4：Hook 拦截**
- 在 `PRE_SKILL_INJECT` Hook 中执行安全验证
- 验证失败时拒绝注入，返回 `HookAction.BLOCK`

#### 6.1.3 安全检测 Skill (`_security_validator.md`)
系统级 Skill，用于验证第三方 Skills 的安全性。

**检测规则**：
1. 检测覆盖指令关键词（"ignore previous", "override", "system prompt"）
2. 检测异常长度（> 2000 tokens）
3. 检测可疑格式（如包含大量特殊字符）
4. 检测敏感操作指令（如 "execute", "eval", "delete"）

**返回结果**：
- `safe: true/false`
- `issues: [...]`（检测到的问题列表）
- `risk_level: low/medium/high`

### 6.2 Hook 机制

#### 6.2.1 新增 Hook 事件

**PRE_SKILL_INJECT**
- **触发时机**：技能选择完成后、格式化注入前
- **Payload**：
  ```python
  {
      "agent_type": "browser",
      "session_id": "...",
      "task": "...",
      "selected_skills": [skill1, skill2, ...],
      "skill_names": ["jd_ecommerce", "taobao_ecommerce"]
  }
  ```
- **用途**：安全验证、技能过滤

**POST_SKILL_INJECT**
- **触发时机**：技能注入到动态上下文后
- **Payload**：
  ```python
  {
      "agent_type": "browser",
      "session_id": "...",
      "injected_skills": ["jd_ecommerce"],
      "total_tokens": 1500
  }
  ```
- **用途**：审计日志、统计分析

#### 6.2.2 Hook 处理器注册
在 `main.py` 的 `initialize_system()` 中注册：

```python
async def skill_security_hook(payload: dict) -> HookResult:
    """PRE_SKILL_INJECT 安全验证 Hook"""
    selected_skills = payload.get("selected_skills", [])
    
    for skill in selected_skills:
        # 使用 _security_validator.md 验证
        is_safe = validate_skill_content(skill)
        if not is_safe:
            return HookResult(
                action=HookAction.BLOCK,
                reason=f"Skill '{skill.name}' 未通过安全验证"
            )
    
    return HookResult(action=HookAction.ALLOW)

# 注册
hook_registry.register(HookEvent.PRE_SKILL_INJECT, skill_security_hook)
```

---

## 7. 集成点

### 7.1 prompt_builder.py 修改

#### 新增函数：`format_skills()`
```python
def format_skills(skills: List[Skill]) -> str:
    """
    格式化技能内容为注入字符串。
    
    :param skills: 技能对象列表
    :return: 格式化后的技能内容字符串
    """
    # 实现细节见 tasks.md
```

#### 修改函数：`build_dynamic_context()`
```python
def build_dynamic_context(
    task: str, 
    worktree_path: str = "", 
    env_vars: Dict[str, str] = None,
    skills: str = ""  # 新增参数
) -> str:
    """
    装配动态上下文信息（任务描述 + 环境信息 + Skills）。
    
    :param task: 任务描述
    :param worktree_path: WorkTree 沙箱路径
    :param env_vars: 环境变量
    :param skills: 格式化后的技能内容（新增）
    :return: 动态上下文字符串
    """
    # 实现细节见 tasks.md
```

### 7.2 execution_loop.py 修改

在 `execute_turn()` 函数中，第一个 Turn 注入 Skills：

```python
async def execute_turn(...):
    # ... 现有代码 ...
    
    # ── 0.5 技能注入（仅第一个 Turn）──
    if turns == 0 and agent_def.agent_type == "browser":
        # 选择相关技能
        selected_skills = skill_registry.select_skills(context.task)
        
        # 触发 PRE_SKILL_INJECT Hook
        skill_payload = {
            "agent_type": agent_def.agent_type,
            "session_id": context.session_id,
            "task": context.task,
            "selected_skills": selected_skills,
        }
        skill_result = await hook_registry.emit(HookEvent.PRE_SKILL_INJECT, skill_payload)
        
        if skill_result.action == HookAction.BLOCK:
            yield {"event": "skill_inject_blocked", "reason": skill_result.reason}
            return
        
        # 格式化并注入技能
        formatted_skills = format_skills(selected_skills)
        # ... 将 formatted_skills 添加到第一条 user message
        
        # 触发 POST_SKILL_INJECT Hook
        await hook_registry.emit(HookEvent.POST_SKILL_INJECT, {
            "agent_type": agent_def.agent_type,
            "session_id": context.session_id,
            "injected_skills": [s.name for s in selected_skills],
        })
    
    # ... 现有代码 ...
```

### 7.3 hook_registry.py 修改

在 `HookEvent` 枚举中添加新事件：

```python
class HookEvent(Enum):
    """生命周期事件"""
    SESSION_START = "session_start"
    PRE_SKILL_INJECT = "pre_skill_inject"      # 新增
    POST_SKILL_INJECT = "post_skill_inject"    # 新增
    PRE_TOOL_EXECUTE = "pre_tool_execute"
    POST_TOOL_EXECUTE = "post_tool_execute"
    PRE_COMPACT = "pre_compact"
    POST_COMPACT = "post_compact"
    PRE_TURN_COMPLETE = "pre_turn_complete"
    POST_AGENT_COMPLETE = "post_agent_complete"
```

### 7.4 main.py 修改

在 `initialize_system()` 中：

```python
async def initialize_system(llm_provider: BaseLLMProvider) -> AgentSpawner:
    # ... 现有代码 ...
    
    # 初始化 SkillRegistry
    skill_registry = SkillRegistry(base_dir="skills/browser")
    skill_registry.load_all()
    
    # 注册 Skill 安全 Hook
    hook_registry.register(HookEvent.PRE_SKILL_INJECT, skill_security_hook)
    hook_registry.register(HookEvent.POST_SKILL_INJECT, skill_audit_hook)
    
    # ... 现有代码 ...
```

### 7.5 agent_definition.py 修改

精简 Browser Agent 的 `system_prompt`，移除特定网站策略：

**移除内容**：
- 京东/淘宝特定的搜索 URL 模式
- 商品详情页的评论加载策略
- 特定网站的反爬虫技巧

**保留内容**：
- 通用 DOM 诊断策略
- 通用搜索框操作最佳实践
- 通用防反爬策略
- 通用工作流程策略

---

## 8. 示例技能迁移

### 8.1 京东电商技能 (`jd_ecommerce.md`)
从 `agent_definition.py` 中迁移京东特定策略：
- 直接搜索 URL 模式
- 商品详情页异步评论加载
- 评论标签切换选择器

### 8.2 淘宝电商技能 (`taobao_ecommerce.md`)
从 `agent_definition.py` 中迁移淘宝特定策略：
- 直接搜索 URL 模式
- 淘宝特定反爬虫策略

### 8.3 技能模板 (`_template.md`)
提供标准模板，包含：
- 完整的 YAML frontmatter 示例
- Markdown 内容结构示例
- 注释说明

### 8.4 安全检测技能 (`_security_validator.md`)
系统级技能，用于验证第三方 Skills：
- 检测规则定义
- 风险等级评估
- 问题报告格式

---

## 9. 数据流图

### 9.1 技能加载流程
```
系统启动
    ↓
SkillRegistry.load_all()
    ↓
扫描 skills/browser/ 目录
    ↓
解析每个 .md 文件
    ↓
验证元数据和内容
    ↓
加载到内存注册表
    ↓
记录加载结果
```

### 9.2 技能注入时序图
```
AgentSpawner.spawn()
    ↓
SkillRegistry.select_skills(task)
    ↓
Hook: PRE_SKILL_INJECT (安全验证)
    ↓
format_skills(selected_skills)
    ↓
build_dynamic_context(task, skills=formatted)
    ↓
Hook: POST_SKILL_INJECT (审计)
    ↓
execution_loop.execute_turn()
    ↓
第一条 user message 包含 Skills
```

### 9.3 安全验证流程
```
PRE_SKILL_INJECT Hook 触发
    ↓
遍历 selected_skills
    ↓
对每个 Skill 调用 _security_validator
    ↓
检测可疑模式
    ↓
评估风险等级
    ↓
返回验证结果
    ↓
如果不安全 → HookAction.BLOCK
如果安全 → HookAction.ALLOW
```

---

## 10. API 设计

### 10.1 SkillRegistry 类接口

```python
class SkillRegistry:
    """技能注册表"""
    
    def __init__(self, base_dir: str):
        """初始化技能注册表"""
        
    def load_all(self) -> Dict[str, Any]:
        """加载所有技能文件"""
        
    def reload(self) -> Dict[str, Any]:
        """重新加载所有技能"""
        
    def select_skills(self, task: str, explicit_skills: List[str] = None) -> List[Skill]:
        """根据任务描述选择相关技能"""
        
    def get_skill(self, name: str) -> Optional[Skill]:
        """根据名称获取技能"""
        
    def list_skills(self) -> List[str]:
        """列出所有已加载的技能名称"""
```

### 10.2 format_skills() 函数签名

```python
def format_skills(skills: List[Skill]) -> str:
    """
    格式化技能内容为注入字符串。
    
    :param skills: 技能对象列表
    :return: 格式化后的技能内容字符串
    
    格式示例：
    ━━━ 专业技能知识库 ━━━
    
    【技能 1: jd_ecommerce v1.0.0】
    {skill content}
    
    【技能 2: taobao_ecommerce v1.0.0】
    {skill content}
    """
```

### 10.3 Hook 处理器接口

```python
async def skill_security_hook(payload: dict) -> HookResult:
    """
    PRE_SKILL_INJECT 安全验证 Hook。
    
    :param payload: {
        "agent_type": str,
        "session_id": str,
        "task": str,
        "selected_skills": List[Skill],
        "skill_names": List[str]
    }
    :return: HookResult(action=ALLOW/BLOCK, reason=...)
    """

async def skill_audit_hook(payload: dict) -> HookResult:
    """
    POST_SKILL_INJECT 审计日志 Hook。
    
    :param payload: {
        "agent_type": str,
        "session_id": str,
        "injected_skills": List[str],
        "total_tokens": int
    }
    :return: HookResult(action=ALLOW)
    """
```

---

## 11. 实现优先级

### P0（核心功能）
1. SkillRegistry 类实现
2. 技能文件格式解析
3. prompt_builder.py 集成
4. execution_loop.py 集成
5. 精简 Browser Agent system_prompt

### P1（安全机制）
6. Hook 事件添加
7. 安全检测 Skill 实现
8. PRE_SKILL_INJECT Hook 处理器

### P2（示例迁移）
9. 京东电商 Skill 迁移
10. 淘宝电商 Skill 迁移
11. 技能模板创建

### P3（增强功能）
12. 热重载机制
13. POST_SKILL_INJECT Hook 处理器
14. 技能使用统计

---

## 12. 测试策略

### 12.1 单元测试
- SkillRegistry 加载和解析
- 技能选择算法
- format_skills() 格式化输出
- 安全检测规则

### 12.2 集成测试
- 完整的技能注入流程
- Hook 触发和拦截
- 多技能同时注入
- 无匹配技能场景

### 12.3 安全测试
- 恶意 Skill 文件注入
- 提示词覆盖攻击
- 长度限制验证
- 边界标记绕过

### 12.4 性能测试
- 技能加载时间
- 技能选择性能
- Prompt Cache 命中率
- Token 消耗对比

---

## 13. 未来扩展

### 13.1 技能市场
- 支持从远程仓库下载 Skills
- 技能评分和评论系统
- 技能依赖管理

### 13.2 智能技能推荐
- 基于任务历史推荐相关 Skills
- 自动学习用户偏好
- A/B 测试不同 Skills 效果

### 13.3 多 Agent 技能共享
- 支持 Coding Agent、Lead Agent 等其他 Agent 使用 Skills
- 跨 Agent 技能复用
- 技能权限管理

---

## 附录 A：术语表

| 术语 | 定义 |
|------|------|
| Skill | 包含特定网站自动化策略的独立文件 |
| SkillRegistry | 技能注册表，负责加载和管理 Skills |
| System Prompt | Agent 的静态人格定义，保持不变以利用 Prompt Cache |
| Dynamic Context | 动态上下文，包含任务描述、环境信息和注入的 Skills |
| PRE_SKILL_INJECT | 技能注入前的 Hook 事件，用于安全验证 |
| POST_SKILL_INJECT | 技能注入后的 Hook 事件，用于审计日志 |
| Prompt Injection | 提示词注入攻击，通过恶意输入覆盖系统指令 |

---

## 附录 B：参考资料

- Anthropic Prompt Caching 文档
- YAML Frontmatter 规范
- 提示词注入攻击防御最佳实践
- V4 系统架构文档
