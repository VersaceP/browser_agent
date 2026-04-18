# 项目文件树 (2026-04-15 更新)

```
browser_agent_system_v5/
│
├── 📁 core/                          # 核心模块
│   ├── agent_definition.py           ✅ 修改（删除网站特定策略 + 新增视觉分析说明）
│   ├── agent_spawner.py              ✅ 修改（传递 skill_registry）
│   ├── execution_loop.py             ✅ 修改（技能注入逻辑）
│   ├── hook_registry.py              ✅ 修改（新增 PRE/POST_SKILL_INJECT 事件）
│   ├── prompt_builder.py             ✅ 修改（format_skills + 技能注入）
│   ├── skill_registry.py             ✅ 新建（技能注册表核心）
│   ├── tool_registry.py
│   └── worktree_manager.py
│
├── 📁 toolkits/                      # 工具集
│   ├── base_tool.py
│   ├── browser_tools.py              ✅ 修改（ScreenshotTool 集成视觉分析）
│   ├── vision_helper.py              ✅ 新建（视觉分析核心模块）
│   ├── coding_tools.py
│   ├── lead_tools.py
│   └── verification_tools.py
│
├── 📁 skills/                        # 技能库
│   └── 📁 browser/                   # 浏览器技能
│       ├── _template.md              ✅ 新建（技能模板）
│       ├── _security_validator.md    ✅ 新建（安全验证技能）
│       ├── jd_ecommerce.md           ✅ 新建（京东电商技能）
│       └── taobao_ecommerce.md       ✅ 新建（淘宝电商技能）
│
├── 📁 tests/                         # 测试文件
│   ├── test_skills.py                ✅ 新建（技能系统测试）
│   ├── test_vision_integration.py    ✅ 新建（视觉分析测试）
│   ├── test_v4_updates.py
│   ├── test_v42_fixes.py
│   └── _verify.py
│
├── 📁 docs/                          # 文档
│   ├── 📁 vision_analysis/           # 视觉分析文档
│   │   ├── VISION_ANALYSIS_GUIDE.md          ✅ 新建（使用指南）
│   │   ├── VISION_INTEGRATION_SUMMARY.md     ✅ 新建（实现总结）
│   │   └── QUICK_START_VISION.md             ✅ 新建（快速开始）
│   ├── 📁 skills/                    # 技能文档（预留）
│   ├── CHANGELOG_2026-04-15.md       ✅ 新建（完整改动日志）
│   ├── CHANGES_QUICK_REFERENCE.md    ✅ 新建（改动快速参考）
│   ├── FILE_TREE.md                  ✅ 新建（本文档）
│   ├── architecture_v4.md
│   ├── implementation_plan_v4_merged.md
│   └── implementation_plan.md.resolved
│
├── 📁 integrations/                  # 集成模块
│   └── anthropic_client.py
│
├── 📁 permissions/                   # 权限管理
│   └── permission_manager.py
│
├── 📁 debug/                         # 调试工具
│   └── (调试相关文件)
│
├── 📁 worktrees/                     # 工作树（Agent 沙箱）
│   └── (动态生成的工作目录)
│
├── 📁 browser_profiles/              # 浏览器配置文件
│   └── default/
│       └── (Chrome 用户数据)
│
├── 📄 main.py                        ✅ 修改（初始化 SkillRegistry + Hook 注册）
├── 📄 config.json                    # 系统配置
├── 📄 requirements.txt               # Python 依赖
├── 📄 .gitignore
└── 📄 README.md
```

## 图例

- ✅ **新建** - 本次任务新创建的文件
- ✅ **修改** - 本次任务修改的文件
- 📁 - 目录
- 📄 - 文件

## 核心模块说明

### core/ - 核心引擎
- `skill_registry.py` - 技能注册表，负责加载、匹配、注入技能
- `prompt_builder.py` - 提示词构建器，负责格式化技能并注入到动态上下文
- `execution_loop.py` - 执行循环，在首轮注入技能
- `hook_registry.py` - Hook 注册表，新增技能注入相关事件
- `agent_definition.py` - Agent 定义，删除硬编码策略，新增视觉分析说明
- `agent_spawner.py` - Agent 派生器，传递 skill_registry

### toolkits/ - 工具集
- `vision_helper.py` - 视觉分析模块，封装 MiniMax M2.7 API
- `browser_tools.py` - 浏览器工具，ScreenshotTool 集成视觉分析

### skills/browser/ - 浏览器技能库
- `_template.md` - 技能文件模板
- `_security_validator.md` - 系统安全验证技能（不自动注入）
- `jd_ecommerce.md` - 京东电商自动化技能
- `taobao_ecommerce.md` - 淘宝/天猫电商自动化技能

### tests/ - 测试套件
- `test_skills.py` - 技能系统单元测试
- `test_vision_integration.py` - 视觉分析集成测试

### docs/ - 文档中心
- `vision_analysis/` - 视觉分析相关文档
- `CHANGELOG_2026-04-15.md` - 完整改动日志
- `CHANGES_QUICK_REFERENCE.md` - 改动快速参考
- `FILE_TREE.md` - 项目文件树（本文档）

## 文件统计

- **总文件数**：约 50+ 个
- **本次新建**：12 个
- **本次修改**：8 个
- **核心模块**：6 个
- **工具模块**：2 个
- **技能文件**：4 个
- **测试文件**：2 个
- **文档文件**：6 个

## 代码行数统计

- **新增代码**：约 2000 行
- **修改代码**：约 500 行
- **删除代码**：约 200 行（删除硬编码策略）
- **净增加**：约 2300 行

## 依赖关系

```
main.py
  ├─> SkillRegistry (core/skill_registry.py)
  ├─> HookRegistry (core/hook_registry.py)
  └─> AgentSpawner (core/agent_spawner.py)
        └─> execution_loop (core/execution_loop.py)
              ├─> SkillRegistry.select_skills()
              ├─> prompt_builder.build_dynamic_context()
              │     └─> prompt_builder.format_skills()
              └─> HookRegistry.trigger()

ScreenshotTool (toolkits/browser_tools.py)
  └─> VisionAnalyzer (toolkits/vision_helper.py)
        └─> anthropic.Anthropic (MiniMax M2.7 API)
```

## 配置文件

### 技能文件格式
```yaml
---
name: "技能名称"
description: "技能描述"
keywords: ["关键词1", "关键词2"]
url_patterns: ["正则表达式1", "正则表达式2"]
priority: 10
---

# Markdown 内容
技能的具体策略和指导...
```

### 环境变量
```bash
# 视觉分析（可选）
export ANTHROPIC_AUTH_TOKEN="your_minimax_api_key"
export ANTHROPIC_BASE_URL="https://api.minimaxi.com"
```

## 更新历史

- **2026-04-15**: 完成 Skill Injection System + Vision Analysis Integration
- **2026-04-14**: 完成 Phase 1-6 (Skill Injection 核心功能)
- **2026-04-13**: 创建 Spec 文档
