# 实现计划：浏览器代理技能注入系统

## 概述

本实现计划将设计文档转换为可执行的编码任务，用于构建浏览器代理技能注入系统。该系统将通用浏览器自动化能力与特定网站知识分离，通过将策略外部化为可复用的 Skill 文件并在运行时动态注入。

**关键架构决策：**
- Skills 通过 `build_dynamic_context()` 作为动态上下文注入（而非 system prompt）
- 无独立 ContextAugmenter 组件 - 集成到 `prompt_builder.py`
- 添加 `PRE_SKILL_INJECT` 和 `POST_SKILL_INJECT` Hook 事件
- 创建 `_security_validator.md` 系统技能用于验证
- 直接架构迁移（无向后兼容性）

**实现语言：** Python

---

## 任务列表

### 阶段 1：核心基础设施 (P0)

- [x] 1. 创建 Skill 数据模型和文件解析器
  - [x] 1.1 创建 `browser_agent_system_v5/core/skill_registry.py` 并定义 Skill 数据类
    - 定义 `Skill` 数据类，包含字段：name, version, target_websites, keywords, description, author, content
    - 使用 `yaml` 库实现 YAML frontmatter 解析器
    - 实现 Markdown 内容提取器
    - 添加必需字段验证（name, version, target_websites, description）
    - 添加内容长度验证（最大 10KB 文件大小，最大 2000 tokens 内容）
    - _需求: 2.2, 2.3, 2.4, 2.5, 2.6, 9.1, 9.2, 9.3_
  
  - [ ]* 1.2 编写 Skill 解析的单元测试
    - 测试有效技能文件解析
    - 测试缺少必需字段
    - 测试无效 YAML frontmatter
    - 测试内容长度限制
    - 测试格式错误的 markdown
    - _需求: 2.6, 9.1, 9.2, 9.3_

- [x] 2. 实现 SkillRegistry 类
  - [x] 2.1 实现 SkillRegistry 初始化和目录扫描
    - 创建 `SkillRegistry.__init__(base_dir: str)` 方法
    - 实现 `load_all()` 方法扫描 `skills/browser/` 目录
    - 使用任务 1.1 的 Skill 解析器解析每个 `.md` 文件
    - 构建内存注册表：`Dict[str, Skill]` 按技能名称索引
    - 记录加载结果（成功/失败及原因）
    - 跳过系统技能（以 `_` 开头的文件）不自动注入
    - _需求: 3.1, 3.2, 3.3, 3.4_
  
  - [x] 2.2 实现技能选择算法
    - 创建 `select_skills(task: str, explicit_skills: List[str] = None) -> List[Skill]` 方法
    - 实现 URL 域名提取并与 `target_websites` 匹配
    - 实现关键词匹配（与技能 `keywords` 字段匹配，不区分大小写，支持中英文）
    - 支持通过 `explicit_skills` 参数显式指定技能名称
    - 返回所有匹配的技能，按名称排序
    - 限制总注入内容为 8000 tokens
    - _需求: 5.1, 5.2, 5.3, 5.4, 5.5_
  
  - [x] 2.3 实现辅助方法
    - 创建 `get_skill(name: str) -> Optional[Skill]` 方法
    - 创建 `list_skills() -> List[str]` 方法
    - 创建 `reload()` 方法用于热重载所有技能
    - _需求: 3.5, 8.1, 8.2, 11.3_
  
  - [ ]* 2.4 编写 SkillRegistry 的单元测试
    - 测试从目录加载技能
    - 测试通过 URL 域名选择技能
    - 测试通过关键词选择技能
    - 测试显式技能选择
    - 测试热重载功能
    - 测试 token 限制强制执行
    - _需求: 3.1, 3.2, 3.3, 5.1, 5.2, 5.3, 5.5_

- [ ] 3. 检查点 - 确保所有测试通过
  - 确保所有测试通过，如有问题请询问用户。

### 阶段 2：Prompt Builder 集成 (P0)

- [x] 4. 扩展 prompt_builder.py 添加技能格式化功能
  - [x] 4.1 实现 format_skills() 函数
    - 在 `prompt_builder.py` 中创建 `format_skills(skills: List[Skill]) -> str` 函数
    - 使用清晰的边界标记格式化技能：`━━━ 专业技能知识库 ━━━`
    - 每个技能格式化为：`【技能 N: {name} v{version}】\n{content}`
    - 添加安全边界标记以防止提示词注入
    - 返回准备注入的格式化字符串
    - _需求: 4.3, 6.1.2 (策略 1), 8.1_
  
  - [x] 4.2 扩展 build_dynamic_context() 接受 skills 参数
    - 向 `build_dynamic_context()` 函数添加 `skills: str = ""` 参数
    - 在任务和环境信息后追加格式化的技能部分
    - 保持现有结构：任务 → 环境 → 技能
    - _需求: 4.3, 4.4_
  
  - [ ]* 4.3 编写 prompt 格式化的单元测试
    - 测试 format_skills() 处理单个技能
    - 测试 format_skills() 处理多个技能
    - 测试 format_skills() 处理空列表
    - 测试 build_dynamic_context() 使用 skills 参数
    - 验证边界标记存在
    - _需求: 4.3, 4.4_

- [ ] 5. 检查点 - 确保所有测试通过
  - 确保所有测试通过，如有问题请询问用户。

### 阶段 3：Hook 系统扩展 (P1)

- [x] 6. 向 hook_registry.py 添加新的 Hook 事件
  - [x] 6.1 扩展 HookEvent 枚举添加技能注入事件
    - 向 `HookEvent` 枚举添加 `PRE_SKILL_INJECT = "pre_skill_inject"`
    - 向 `HookEvent` 枚举添加 `POST_SKILL_INJECT = "post_skill_inject"`
    - 更新文档字符串记录新事件
    - _需求: 4.4_
  
  - [ ]* 6.2 编写新 Hook 事件的单元测试
    - 测试 PRE_SKILL_INJECT 事件注册和触发
    - 测试 POST_SKILL_INJECT 事件注册和触发
    - 测试 PRE_SKILL_INJECT 的 Hook 阻止行为
    - _需求: 4.4_

- [x] 7. 创建安全验证 Hook 处理器
  - [x] 7.1 在 main.py 中实现 skill_security_hook() 函数
    - 创建 `async def skill_security_hook(payload: dict) -> HookResult` 函数
    - 从 payload 提取 `selected_skills`
    - 对每个技能检查可疑模式：
      - 提示词覆盖关键词："ignore previous", "override", "system prompt"
      - 过长内容（> 2000 tokens）
      - 可疑格式（过多特殊字符）
      - 敏感操作关键词："execute", "eval", "delete"
    - 如果不安全返回 `HookResult(action=HookAction.BLOCK, reason=...)`
    - 如果安全返回 `HookResult(action=HookAction.ALLOW)`
    - _需求: 6.1.2 (策略 3, 4), 6.2.1_
  
  - [x] 7.2 在 main.py 中实现 skill_audit_hook() 函数
    - 创建 `async def skill_audit_hook(payload: dict) -> HookResult` 函数
    - 从 payload 提取 `injected_skills` 和 `total_tokens`
    - 记录技能注入详情用于审计跟踪
    - 返回 `HookResult(action=HookAction.ALLOW)`
    - _需求: 6.2.1_
  
  - [ ]* 7.3 编写 Hook 处理器的单元测试
    - 测试 skill_security_hook() 处理安全技能
    - 测试 skill_security_hook() 处理恶意技能（提示词注入尝试）
    - 测试 skill_audit_hook() 日志行为
    - _需求: 6.1.2, 6.2.1_

- [ ] 8. 检查点 - 确保所有测试通过
  - 确保所有测试通过，如有问题请询问用户。

### 阶段 4：执行循环集成 (P0)

- [x] 9. 将技能注入集成到 execution_loop.py
  - [x] 9.1 向 execute_turn() 函数添加技能注入逻辑
    - 在文件顶部导入 `SkillRegistry` 和 `format_skills`
    - 在 `execute_turn()` 函数签名中接受 `skill_registry: SkillRegistry` 参数
    - 在 SESSION_START Hook 之后（Turn 1 之前），检查 `agent_def.agent_type == "browser"` 且 `turns == 0`
    - 调用 `skill_registry.select_skills(context.task)` 获取相关技能
    - 构建 PRE_SKILL_INJECT payload 包含 selected_skills
    - 触发 PRE_SKILL_INJECT Hook 并检查 BLOCK 动作
    - 如果允许，调用 `format_skills(selected_skills)` 格式化技能
    - 通过上下文初始化将格式化的技能传递给第一条 user message
    - 触发 POST_SKILL_INJECT Hook 包含注入的技能名称和 token 数量
    - _需求: 4.1, 4.2, 4.4, 6.2.1_
  
  - [x] 9.2 修改上下文初始化以包含技能
    - 创建第一条 user message（Turn 0）时，包含格式化的技能
    - 使用 `build_dynamic_context(task, worktree_path, env_vars, skills=formatted_skills)`
    - 确保技能只注入一次（仅第一个 Turn）
    - _需求: 4.1, 4.2, 4.3_
  
  - [ ]* 9.3 编写技能注入流程的集成测试
    - 测试 Browser Agent 的完整技能注入流程
    - 测试非浏览器 agent 跳过技能注入
    - 测试技能注入仅在 Turn 0 发生
    - 测试 PRE_SKILL_INJECT Hook 阻止
    - 测试 POST_SKILL_INJECT Hook 执行
    - _需求: 4.1, 4.2, 4.4_

- [ ] 10. 检查点 - 确保所有测试通过
  - 确保所有测试通过，如有问题请询问用户。

### 阶段 5：主系统集成 (P0)

- [x] 11. 更新 main.py 初始化 SkillRegistry
  - [x] 11.1 在 initialize_system() 函数中初始化 SkillRegistry
    - 在文件顶部导入 `SkillRegistry`
    - 在 `initialize_system()` 中创建 `skill_registry = SkillRegistry(base_dir="skills/browser")`
    - 调用 `skill_registry.load_all()` 加载所有技能
    - 记录加载结果（加载的技能数量、任何失败）
    - 将 `skill_registry` 传递给 `AgentSpawner` 构造函数
    - _需求: 3.1, 3.2, 3.3_
  
  - [x] 11.2 在 initialize_system() 中注册技能 Hook 处理器
    - 为 PRE_SKILL_INJECT 事件注册 `skill_security_hook`
    - 为 POST_SKILL_INJECT 事件注册 `skill_audit_hook`
    - 记录 Hook 注册
    - _需求: 6.2.2_
  
  - [x] 11.3 更新 AgentSpawner 接受并传递 skill_registry
    - 向 `AgentSpawner.__init__()` 添加 `skill_registry: SkillRegistry` 参数
    - 存储为实例变量 `self.skill_registry`
    - 将 `skill_registry` 传递给 `execute_turn()` 调用
    - _需求: 4.1, 4.2_

- [ ] 12. 检查点 - 确保所有测试通过
  - 确保所有测试通过，如有问题请询问用户。

### 阶段 6：Browser Agent 系统提示词精简 (P0)

- [x] 13. 重构 agent_definition.py 中的 Browser Agent system_prompt
  - [x] 13.1 从 Browser Agent prompt 中移除特定网站内容
    - 移除京东特定搜索 URL 模式：`https://search.jd.com/Search?keyword={keyword}&enc=utf-8`
    - 移除淘宝特定搜索 URL 模式：`https://s.taobao.com/search?q={keyword}`
    - 移除京东商品页评论加载策略
    - 移除任何其他特定网站的 CSS 选择器或操作序列
    - _需求: 1.1, 1.2, 1.3_
  
  - [x] 13.2 保留通用浏览器自动化策略
    - 保留 DOM 诊断策略（run_js 示例）
    - 保留通用搜索框操作最佳实践
    - 保留通用反爬虫策略（等待时间、验证检测）
    - 保留通用工作流程策略（使用 wait_user 处理登录/验证码）
    - 保留所有工具能力描述
    - _需求: 1.4, 1.5, 10.2_
  
  - [ ]* 13.3 验证精简 prompt 后的 Browser Agent 功能
    - 测试 Browser Agent 仍能执行基本导航
    - 测试 Browser Agent 仍能提取文本
    - 测试 Browser Agent 仍能处理表单
    - 测试 Browser Agent 保持所有核心自动化能力
    - _需求: 1.5, 10.1, 10.4, 10.5_

- [ ] 14. 检查点 - 确保所有测试通过
  - 确保所有测试通过，如有问题请询问用户。

### 阶段 7：示例技能迁移 (P2)

- [x] 15. 创建技能仓库目录结构
  - [x] 15.1 创建 skills 目录结构
    - 创建目录 `browser_agent_system_v5/skills/browser/`
    - 确保目录包含在版本控制中
    - _需求: 2.1_

- [x] 16. 创建技能模板文件
  - [x] 16.1 创建 _template.md 技能模板
    - 创建 `browser_agent_system_v5/skills/browser/_template.md`
    - 包含完整的 YAML frontmatter，包含所有字段和示例值
    - 包含 Markdown 内容结构，包含章节和注释
    - 为每个元数据字段添加解释性注释
    - 提供 CSS 选择器、操作序列和 JavaScript 代码片段的示例
    - _需求: 12.1, 12.2, 12.3_

- [x] 17. 迁移京东技能
  - [x] 17.1 创建 jd_ecommerce.md 技能文件
    - 创建 `browser_agent_system_v5/skills/browser/jd_ecommerce.md`
    - 添加 YAML frontmatter 元数据：
      - name: jd_ecommerce
      - version: 1.0.0
      - target_websites: [jd.com, search.jd.com]
      - keywords: [京东, JD, 京东商城]
      - description: 京东电商平台自动化策略
      - author: system
    - 从 agent_definition.py 迁移直接搜索 URL 模式
    - 迁移商品页异步评论加载策略
    - 迁移评论标签切换选择器和技术
    - 包含任何京东特定的反爬虫策略
    - 如需要添加评论提取的 JavaScript 代码片段
    - _需求: 6.1, 6.2, 6.3, 6.4, 6.5, 2.7_
  
  - [ ]* 17.2 测试京东技能注入和功能
    - 测试 SkillRegistry 加载技能
    - 测试任务提及"京东"或"JD"时选择技能
    - 测试任务包含 jd.com URL 时选择技能
    - 测试注入后 Browser Agent 可以访问京东特定知识
    - _需求: 6.5_

- [x] 18. 迁移淘宝技能
  - [x] 18.1 创建 taobao_ecommerce.md 技能文件
    - 创建 `browser_agent_system_v5/skills/browser/taobao_ecommerce.md`
    - 添加 YAML frontmatter 元数据：
      - name: taobao_ecommerce
      - version: 1.0.0
      - target_websites: [taobao.com, s.taobao.com]
      - keywords: [淘宝, Taobao, 淘宝网]
      - description: 淘宝电商平台自动化策略
      - author: system
    - 从 agent_definition.py 迁移直接搜索 URL 模式
    - 迁移任何淘宝特定的反爬虫策略
    - 包含任何淘宝特定的选择器或技术
    - _需求: 7.1, 7.2, 7.3_
  
  - [ ]* 18.2 测试淘宝技能注入和功能
    - 测试 SkillRegistry 加载技能
    - 测试任务提及"淘宝"或"Taobao"时选择技能
    - 测试任务包含 taobao.com URL 时选择技能
    - 测试注入后 Browser Agent 可以访问淘宝特定知识
    - _需求: 7.3_

- [ ] 19. 检查点 - 确保所有测试通过
  - 确保所有测试通过，如有问题请询问用户。

### 阶段 8：安全验证器技能 (P1)

- [x] 20. 创建安全验证器系统技能
  - [x] 20.1 创建 _security_validator.md 系统技能
    - 创建 `browser_agent_system_v5/skills/browser/_security_validator.md`
    - 添加 YAML frontmatter 元数据：
      - name: _security_validator
      - version: 1.0.0
      - description: 用于验证第三方技能的系统级技能
      - author: system
    - 记录检测规则：
      - 提示词覆盖关键词检测
      - 过长内容检测（> 2000 tokens）
      - 可疑格式检测
      - 敏感操作关键词检测
    - 定义风险等级：low, medium, high
    - 定义输出格式：{safe: bool, issues: List[str], risk_level: str}
    - _需求: 6.1.2 (策略 3), 6.1.3_
  
  - [ ]* 20.2 测试安全验证器技能
    - 测试验证器检测提示词注入尝试
    - 测试验证器检测过长内容
    - 测试验证器检测可疑模式
    - 测试验证器允许安全技能
    - _需求: 6.1.3_

- [ ] 21. 检查点 - 确保所有测试通过
  - 确保所有测试通过，如有问题请询问用户。

### 阶段 9：文档和示例 (P2)

- [ ] 22. 创建技能文档
  - [ ] 22.1 记录技能文件格式和注入机制
    - 创建文档文件解释 YAML frontmatter 格式
    - 记录所有元数据字段及其用途
    - 解释技能选择算法
    - 记录安全机制
    - 提供技能创建工作流程示例
    - _需求: 12.4, 12.5_
  
  - [ ] 22.2 添加内联代码文档
    - 为 SkillRegistry 类添加全面的文档字符串
    - 为 format_skills() 函数添加文档字符串
    - 为 Hook 处理器添加文档字符串
    - 在 execution_loop.py 中记录技能注入流程
    - _需求: 12.4_

### 阶段 10：增强功能 (P3)

- [ ] 23. 实现技能使用统计
  - [ ] 23.1 向 SkillRegistry 添加技能使用跟踪
    - 添加 `_usage_stats: Dict[str, int]` 跟踪技能注入次数
    - 在 `select_skills()` 中选择技能时增加计数器
    - 创建 `get_usage_stats() -> Dict[str, int]` 方法
    - 创建 `reset_usage_stats()` 方法
    - _需求: 增强功能_
  
  - [ ]* 23.2 编写使用统计的测试
    - 测试技能选择时使用计数器增加
    - 测试 get_usage_stats() 返回正确计数
    - 测试 reset_usage_stats() 清除计数器
    - _需求: 增强功能_

- [ ] 24. 实现技能版本控制支持
  - [ ] 24.1 向 SkillRegistry 添加版本比较逻辑
    - 当多个文件匹配相同技能名称时，解析版本号
    - 加载最高版本号（语义化版本：MAJOR.MINOR.PATCH）
    - 记录版本选择决策
    - _需求: 11.1, 11.2, 11.5_
  
  - [ ] 24.2 向技能模板添加变更日志部分
    - 更新 `_template.md` 添加变更日志部分
    - 提供示例变更日志条目
    - 记录版本更新最佳实践
    - _需求: 11.4_
  
  - [ ]* 24.3 编写版本比较的测试
    - 测试存在多个版本时加载最高版本
    - 测试语义化版本解析
    - 测试版本日志记录
    - _需求: 11.1, 11.5_

- [ ] 25. 最终检查点 - 确保所有测试通过
  - 确保所有测试通过，如有问题请询问用户。

---

## 注意事项

- 标记 `*` 的任务是可选的，可以跳过以加快 MVP 开发
- 每个任务引用特定需求以实现可追溯性
- 检查点确保增量验证
- 实现遵循设计文档中的 P0 → P1 → P2 → P3 优先级顺序
- 所有文件路径相对于 `browser_agent_system_v5/` 目录
- 技能仅为 Browser Agent 注入，不适用于其他 agent 类型
- 系统技能（以 `_` 为前缀）从自动注入中排除，但用于验证
- 热重载支持允许在不重启系统的情况下更新技能

---

## 成功标准

当满足以下条件时实现完成：

1. SkillRegistry 可以从 `skills/browser/` 目录加载和解析技能文件
2. 根据任务描述（URL 域名或关键词）自动选择技能
3. 选定的技能被格式化并注入到 Browser Agent 的动态上下文中
4. PRE_SKILL_INJECT 和 POST_SKILL_INJECT Hooks 正确触发
5. 安全验证防止恶意技能注入
6. Browser Agent 的系统提示词已精简（移除特定网站内容）
7. 京东和淘宝技能已迁移并正常工作
8. 保留所有核心浏览器自动化能力
9. 系统通过所有单元和集成测试
10. 文档完整并提供示例
