# 改动快速参考 (2026-04-15)

## 📋 改动文件清单

### ✅ 上一轮任务：Skill Injection System

#### 新建文件 (7个)
1. `core/skill_registry.py` - 技能注册表核心
2. `skills/browser/_template.md` - 技能模板
3. `skills/browser/jd_ecommerce.md` - 京东技能
4. `skills/browser/taobao_ecommerce.md` - 淘宝技能
5. `skills/browser/_security_validator.md` - 安全验证
6. `tests/test_skills.py` - 技能测试
7. `.kiro/specs/browser-agent-skill-injection/` - 完整 Spec 文档

#### 修改文件 (6个)
1. `core/prompt_builder.py` - 新增 `format_skills()` + 修改 `build_dynamic_context()`
2. `core/hook_registry.py` - 新增 `PRE_SKILL_INJECT` 和 `POST_SKILL_INJECT` 事件
3. `main.py` - 初始化 `SkillRegistry` + 注册 Hook 处理器
4. `core/execution_loop.py` - 首轮注入技能逻辑
5. `core/agent_spawner.py` - 传递 `skill_registry` 参数
6. `core/agent_definition.py` - 删除京东/淘宝硬编码策略

---

### ✅ 本轮任务：Vision Analysis Integration

#### 新建文件 (5个)
1. `toolkits/vision_helper.py` - 视觉分析核心模块
2. `tests/test_vision_integration.py` - 视觉分析测试
3. `docs/vision_analysis/VISION_ANALYSIS_GUIDE.md` - 使用指南
4. `docs/vision_analysis/VISION_INTEGRATION_SUMMARY.md` - 实现总结
5. `docs/vision_analysis/QUICK_START_VISION.md` - 快速开始

#### 修改文件 (2个)
1. `toolkits/browser_tools.py` - `ScreenshotTool` 集成视觉分析
2. `core/agent_definition.py` - Browser Agent 新增视觉分析说明

---

## 🔍 关键改动位置

### 1. core/skill_registry.py (新建)
```python
class SkillRegistry:
    def load_all(self) -> None
    def select_skills(self, task_description: str, current_url: str = None) -> List[Skill]
    def reload(self) -> None
```

### 2. core/prompt_builder.py
**位置**：约第 50 行
```python
def format_skills(skills: List[Skill]) -> str:
    # 格式化技能为 Markdown
```

**位置**：约第 150 行
```python
def build_dynamic_context(..., injected_skills: List[Skill] = None):
    # 注入技能到动态上下文
```

### 3. core/hook_registry.py
**位置**：约第 15 行
```python
class HookEvent(str, Enum):
    PRE_SKILL_INJECT = "pre_skill_inject"    # 新增
    POST_SKILL_INJECT = "post_skill_inject"  # 新增
```

### 4. main.py
**位置**：约第 10 行
```python
from core.skill_registry import SkillRegistry  # 新增导入
```

**位置**：约第 50 行
```python
# 初始化技能注册表
skill_registry = SkillRegistry()
skill_registry.load_all()

# 注册 Hook 处理器
hook_registry.register(HookEvent.PRE_SKILL_INJECT, pre_skill_inject_handler)
hook_registry.register(HookEvent.POST_SKILL_INJECT, post_skill_inject_handler)

# 传递给 AgentSpawner
spawner = AgentSpawner(..., skill_registry=skill_registry)
```

### 5. core/execution_loop.py
**位置**：约第 100 行
```python
async def agent_execution_loop(..., skill_registry: Optional[SkillRegistry] = None):
    # 第一轮循环时注入技能（仅 Browser Agent）
    if turn == 0 and agent_def.agent_type == "browser" and skill_registry:
        injected_skills = skill_registry.select_skills(...)
        hook_registry.trigger(HookEvent.PRE_SKILL_INJECT, ...)
    
    # 构建动态上下文（注入技能）
    dynamic_context = build_dynamic_context(..., injected_skills=injected_skills)
```

### 6. core/agent_spawner.py
**位置**：约第 30 行
```python
class AgentSpawner:
    def __init__(self, ..., skill_registry: Optional[SkillRegistry] = None):
        self.skill_registry = skill_registry
```

**位置**：约第 80 行
```python
async def spawn(self, ...):
    async for event in agent_execution_loop(..., skill_registry=self.skill_registry):
        yield event
```

### 7. core/agent_definition.py
**位置**：约第 105-150 行
- ❌ 删除：京东和淘宝的硬编码策略（URL、选择器、特定流程）
- ✅ 保留：通用自动化策略（DOM 诊断、防反爬、工作流程）
- ✅ 新增：视觉分析能力说明（约第 115 行）

### 8. toolkits/vision_helper.py (新建)
```python
class VisionAnalyzer:
    def __init__(self)
    def is_available(self) -> bool
    def analyze_screenshot(self, image_path: str, ...) -> Dict[str, Any]
    def analyze_for_click(self, image_path: str, target_description: str) -> Dict[str, Any]

def get_vision_analyzer() -> VisionAnalyzer
```

### 9. toolkits/browser_tools.py
**位置**：约第 20 行
```python
from toolkits.vision_helper import get_vision_analyzer  # 新增导入
```

**位置**：约第 250-320 行 (ScreenshotTool 类)
- 修改 `description`：说明支持视觉分析
- 修改 `input_schema`：新增 `analyze` 和 `find_element` 参数
- 修改 `max_result_chars`：500 → 3000
- 修改 `execute()` 方法：集成视觉分析逻辑

---

## 📊 改动统计

| 类别 | 上一轮 | 本轮 | 总计 |
|------|--------|------|------|
| 新建文件 | 7 | 5 | 12 |
| 修改文件 | 6 | 2 | 8 |
| 删除文件 | 0 | 0 | 0 |
| 新增代码行 | ~1200 | ~800 | ~2000 |

---

## 🎯 核心功能

### Skill Injection System
- **触发时机**：Browser Agent 首次执行时
- **匹配规则**：关键词匹配 + URL 模式匹配
- **注入位置**：动态上下文（WorkTree 之后，任务描述之前）
- **安全机制**：PRE_SKILL_INJECT Hook + 内容长度限制

### Vision Analysis
- **触发方式**：`screenshot(analyze=True)` 或 `screenshot(find_element='描述')`
- **API 调用**：MiniMax M2.7 多模态模型
- **返回信息**：元素位置、视觉特征、推荐选择器
- **配置要求**：环境变量 `ANTHROPIC_AUTH_TOKEN`

---

## 🧪 测试命令

```bash
# 测试技能注入
cd browser_agent_system_v5
python tests/test_skills.py

# 测试视觉分析
python tests/test_vision_integration.py
```

---

## 📚 相关文档

- 完整改动日志：[CHANGELOG_2026-04-15.md](./CHANGELOG_2026-04-15.md)
- 视觉分析指南：[vision_analysis/VISION_ANALYSIS_GUIDE.md](./vision_analysis/VISION_ANALYSIS_GUIDE.md)
- 视觉分析总结：[vision_analysis/VISION_INTEGRATION_SUMMARY.md](./vision_analysis/VISION_INTEGRATION_SUMMARY.md)
- 快速开始：[vision_analysis/QUICK_START_VISION.md](./vision_analysis/QUICK_START_VISION.md)
