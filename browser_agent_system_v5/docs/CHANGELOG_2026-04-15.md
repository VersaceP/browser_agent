# Browser Agent 改动汇总 (2026-04-15)

## 任务概述

### 上一轮任务：Browser Agent Skill Injection System (技能注入系统)
**目标**：实现动态技能注入机制，让 Browser Agent 能够根据任务自动加载网站特定的自动化策略

### 本轮任务：Vision Analysis Integration (视觉分析集成)
**目标**：集成 MiniMax M2.7 多模态视觉能力，让 Agent 能够"看到"截图内容并精确定位页面元素

---

## 上一轮任务改动详情 (Skill Injection System)

### 1. 新增文件

#### `browser_agent_system_v5/core/skill_registry.py` ✅ 新建
**功能**：技能注册表核心模块
- `Skill` dataclass：技能数据结构
  - `name`: 技能名称
  - `description`: 技能描述
  - `keywords`: 关键词列表（用于匹配）
  - `url_patterns`: URL 模式列表（用于匹配）
  - `content`: 技能内容（Markdown）
  - `priority`: 优先级
  - `is_system`: 是否为系统技能
- `parse_skill_file()` 函数：解析 YAML frontmatter + Markdown
- `SkillRegistry` 类：
  - `load_all()`: 从 `skills/browser/` 加载所有技能
  - `select_skills()`: 根据任务描述和 URL 选择匹配的技能
  - `reload()`: 重新加载技能
- 验证规则：
  - 单个技能文件 ≤ 10KB
  - 单个技能内容 ≤ 2000 tokens
  - 总注入内容 ≤ 8000 tokens
  - 系统技能（`_` 前缀）不自动注入

#### `browser_agent_system_v5/skills/browser/_template.md` ✅ 新建
**功能**：技能文件模板
- 完整的 YAML frontmatter 示例
- Markdown 内容结构示例
- 注释说明各字段用途

#### `browser_agent_system_v5/skills/browser/jd_ecommerce.md` ✅ 新建
**功能**：京东电商自动化技能
- 关键词：京东、JD、jd.com
- URL 模式：`.*jd\.com.*`, `.*item\.jd\.com.*`
- 策略内容：
  - 直接 URL 搜索（绕过搜索框）
  - 异步评论加载处理
  - 评论 Tab 切换策略

#### `browser_agent_system_v5/skills/browser/taobao_ecommerce.md` ✅ 新建
**功能**：淘宝/天猫电商自动化技能
- 关键词：淘宝、天猫、Taobao、Tmall
- URL 模式：`.*taobao\.com.*`, `.*tmall\.com.*`
- 策略内容：
  - 直接 URL 搜索（绕过搜索框）
  - 滑块验证码处理
  - 登录要求处理

#### `browser_agent_system_v5/skills/browser/_security_validator.md` ✅ 新建
**功能**：系统安全验证技能（不自动注入）
- 4 条检测规则：
  1. Prompt Override 检测
  2. 内容长度检测
  3. 可疑格式检测
  4. 敏感关键词检测
- 用于 PRE_SKILL_INJECT Hook

#### `browser_agent_system_v5/tests/test_skills.py` ✅ 新建
**功能**：技能系统单元测试
- 测试技能加载
- 测试关键词匹配
- 测试 URL 匹配
- 测试无匹配情况

### 2. 修改文件

#### `browser_agent_system_v5/core/prompt_builder.py` ✅ 修改
**改动位置**：
- **新增函数** `format_skills(skills: List[Skill]) -> str` (约第 50 行)
  - 格式化技能列表为 Markdown
  - 包含技能名称、描述、内容
- **修改函数** `build_dynamic_context()` (约第 150 行)
  - 新增参数 `injected_skills: List[Skill] = None`
  - 在动态上下文中注入技能内容
  - 技能内容位于 WorkTree 信息之后、任务描述之前

**关键代码**：
```python
def format_skills(skills: List[Skill]) -> str:
    """格式化技能列表为 Markdown"""
    if not skills:
        return ""
    
    sections = []
    sections.append("# 🎯 专项技能指导\n")
    sections.append("以下是针对当前任务自动加载的专项技能，请优先参考这些策略：\n")
    
    for skill in skills:
        sections.append(f"## {skill.name}")
        if skill.description:
            sections.append(f"**描述**: {skill.description}\n")
        sections.append(skill.content)
        sections.append("\n---\n")
    
    return "\n".join(sections)

def build_dynamic_context(..., injected_skills: List[Skill] = None):
    # ... 原有代码 ...
    
    # 注入技能（如果有）
    if injected_skills:
        skill_content = format_skills(injected_skills)
        sections.append(skill_content)
    
    # ... 原有代码 ...
```

#### `browser_agent_system_v5/core/hook_registry.py` ✅ 修改
**改动位置**：
- **修改 enum** `HookEvent` (约第 15 行)
  - 新增 `PRE_SKILL_INJECT = "pre_skill_inject"`
  - 新增 `POST_SKILL_INJECT = "post_skill_inject"`

**关键代码**：
```python
class HookEvent(str, Enum):
    # ... 原有事件 ...
    PRE_SKILL_INJECT = "pre_skill_inject"
    POST_SKILL_INJECT = "post_skill_inject"
```

#### `browser_agent_system_v5/main.py` ✅ 修改
**改动位置**：
- **导入语句** (约第 10 行)
  - 新增 `from core.skill_registry import SkillRegistry`
- **main() 函数** (约第 50 行)
  - 初始化 `SkillRegistry`
  - 注册 PRE_SKILL_INJECT 和 POST_SKILL_INJECT Hook 处理器
  - 将 `skill_registry` 传递给 `AgentSpawner`

**关键代码**：
```python
from core.skill_registry import SkillRegistry

async def main():
    # ... 原有代码 ...
    
    # 初始化技能注册表
    skill_registry = SkillRegistry()
    skill_registry.load_all()
    
    # 注册 Hook 处理器
    def pre_skill_inject_handler(event_data: dict):
        # 安全验证逻辑
        pass
    
    def post_skill_inject_handler(event_data: dict):
        # 日志记录逻辑
        pass
    
    hook_registry.register(HookEvent.PRE_SKILL_INJECT, pre_skill_inject_handler)
    hook_registry.register(HookEvent.POST_SKILL_INJECT, post_skill_inject_handler)
    
    # 创建 AgentSpawner
    spawner = AgentSpawner(
        # ... 原有参数 ...
        skill_registry=skill_registry
    )
```

#### `browser_agent_system_v5/core/execution_loop.py` ✅ 修改
**改动位置**：
- **agent_execution_loop() 函数** (约第 100 行)
  - 新增参数 `skill_registry: Optional[SkillRegistry] = None`
  - 在第一轮循环时（仅 Browser Agent）：
    1. 调用 `skill_registry.select_skills()` 选择技能
    2. 触发 PRE_SKILL_INJECT Hook
    3. 将技能注入到 `build_dynamic_context()`
    4. 触发 POST_SKILL_INJECT Hook

**关键代码**：
```python
async def agent_execution_loop(
    # ... 原有参数 ...
    skill_registry: Optional[SkillRegistry] = None
):
    # ... 原有代码 ...
    
    # 第一轮循环时注入技能（仅 Browser Agent）
    injected_skills = []
    if turn == 0 and agent_def.agent_type == "browser" and skill_registry:
        # 选择技能
        injected_skills = skill_registry.select_skills(
            task_description=task_description,
            current_url=None  # 首次执行时没有 URL
        )
        
        # 触发 PRE_SKILL_INJECT Hook
        if injected_skills:
            hook_registry.trigger(HookEvent.PRE_SKILL_INJECT, {
                "skills": injected_skills,
                "agent_type": agent_def.agent_type
            })
    
    # 构建动态上下文（注入技能）
    dynamic_context = build_dynamic_context(
        # ... 原有参数 ...
        injected_skills=injected_skills
    )
    
    # 触发 POST_SKILL_INJECT Hook
    if injected_skills:
        hook_registry.trigger(HookEvent.POST_SKILL_INJECT, {
            "skills": injected_skills,
            "agent_type": agent_def.agent_type
        })
```

#### `browser_agent_system_v5/core/agent_spawner.py` ✅ 修改
**改动位置**：
- **AgentSpawner.__init__()** (约第 30 行)
  - 新增参数 `skill_registry: Optional[SkillRegistry] = None`
  - 保存为实例变量 `self.skill_registry`
- **AgentSpawner.spawn()** (约第 80 行)
  - 将 `skill_registry` 传递给 `agent_execution_loop()`

**关键代码**：
```python
class AgentSpawner:
    def __init__(
        self,
        # ... 原有参数 ...
        skill_registry: Optional[SkillRegistry] = None
    ):
        # ... 原有代码 ...
        self.skill_registry = skill_registry
    
    async def spawn(self, ...):
        # ... 原有代码 ...
        
        async for event in agent_execution_loop(
            # ... 原有参数 ...
            skill_registry=self.skill_registry
        ):
            yield event
```

#### `browser_agent_system_v5/core/agent_definition.py` ✅ 修改
**改动位置**：
- **Browser Agent system_prompt** (约第 105-150 行)
  - 删除了京东和淘宝的网站特定策略
  - 删除了硬编码的 URL 和选择器
  - 保留了通用的自动化策略

**删除的内容**：
```python
# 删除前：
"【京东特定策略】\n"
"- 搜索框选择器：#key\n"
"- 直接 URL 搜索：https://search.jd.com/Search?keyword=商品名\n"
"- 评论区异步加载，需等待 2-3 秒\n"
# ... 更多京东特定内容 ...

"【淘宝特定策略】\n"
"- 搜索框选择器：#q\n"
"- 直接 URL 搜索：https://s.taobao.com/search?q=商品名\n"
"- 滑块验证码处理：调用 wait_user\n"
# ... 更多淘宝特定内容 ...
```

**保留的内容**：
```python
# 保留：通用策略
"【DOM 诊断策略（最重要！）】\n"
"【搜索框操作最佳实践】\n"
"【防反爬策略】\n"
"【工作流程策略】\n"
```

---

## 本轮任务改动详情 (Vision Analysis Integration)

### 1. 新增文件

#### `browser_agent_system_v5/toolkits/vision_helper.py` ✅ 新建
**功能**：视觉分析核心模块
- `VisionAnalyzer` 类：
  - `__init__()`: 初始化，读取环境变量 `ANTHROPIC_AUTH_TOKEN` 和 `ANTHROPIC_BASE_URL`
  - `is_available()`: 检查 API 密钥是否配置
  - `_encode_image()`: 将图片编码为 base64
  - `_get_image_media_type()`: 根据扩展名获取 media type
  - `analyze_screenshot()`: 通用截图分析
  - `analyze_for_click()`: 专门用于点击操作的元素定位
  - `_build_default_analysis_prompt()`: 构建默认分析提示词
  - `_build_element_location_prompt()`: 构建元素定位提示词
  - `_extract_location_hints()`: 从分析结果中提取定位提示
- `get_vision_analyzer()`: 全局单例函数

**关键代码**：
```python
class VisionAnalyzer:
    def __init__(self):
        self.api_key = os.getenv("ANTHROPIC_AUTH_TOKEN", "")
        self.base_url = os.getenv("ANTHROPIC_BASE_URL", "https://api.minimaxi.com")
        
        if self.api_key:
            self.client = anthropic.Anthropic(
                api_key=self.api_key,
                base_url=self.base_url
            )
    
    def analyze_screenshot(self, image_path: str, query: str = "", find_element: str = ""):
        # 编码图片
        image_data = self._encode_image(image_path)
        
        # 调用 MiniMax M2.7 API
        message = self.client.messages.create(
            model="MiniMax-M2.7",
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "data": image_data}},
                    {"type": "text", "text": prompt}
                ]
            }]
        )
        
        return {"success": True, "analysis": response_text}
```

#### `browser_agent_system_v5/tests/test_vision_integration.py` ✅ 新建
**功能**：视觉分析集成测试
- 测试视觉分析器状态
- 测试浏览器启动
- 测试页面导航
- 测试基础截图
- 测试视觉分析截图
- 测试元素查找

#### `browser_agent_system_v5/docs/vision_analysis/VISION_ANALYSIS_GUIDE.md` ✅ 新建
**功能**：视觉分析使用指南
- 配置说明
- 使用方法
- 使用场景
- 工作流程建议
- 性能考虑
- 故障排除

#### `browser_agent_system_v5/docs/vision_analysis/VISION_INTEGRATION_SUMMARY.md` ✅ 新建
**功能**：视觉分析实现总结
- 技术架构
- 使用示例
- 配置要求
- 测试方法
- 性能影响
- 后续优化建议

#### `browser_agent_system_v5/docs/vision_analysis/QUICK_START_VISION.md` ✅ 新建
**功能**：视觉分析快速开始
- 1分钟快速配置
- 常用命令
- 典型工作流
- 故障排除

### 2. 修改文件

#### `browser_agent_system_v5/toolkits/browser_tools.py` ✅ 修改
**改动位置 1**：文件头部导入 (约第 20 行)
```python
# 新增导入
from toolkits.vision_helper import get_vision_analyzer
```

**改动位置 2**：`ScreenshotTool` 类 (约第 250-320 行)
- **修改 description**：
  ```python
  # 修改前：
  description = "截取当前浏览器页面的屏幕截图并保存到 WorkTree。"
  
  # 修改后：
  description = (
      "截取当前浏览器页面的屏幕截图并保存到 WorkTree。\n"
      "支持可选的视觉分析功能（使用 MiniMax M2.7 多模态模型）：\n"
      "- analyze=true: 自动分析截图内容，识别页面布局、交互元素和位置信息\n"
      "- find_element='描述': 在截图中查找特定元素（如'登录按钮'、'搜索框'）并返回位置信息\n"
      "视觉分析可以帮助你精确定位页面元素，避免盲目猜测 CSS 选择器。"
  )
  ```

- **修改 input_schema**：
  ```python
  # 新增参数
  "analyze": {"type": "boolean", "description": "是否进行视觉分析，默认 false"},
  "find_element": {"type": "string", "description": "要查找的元素描述（如'登录按钮'），启用此参数会自动开启 analyze"}
  ```

- **修改 max_result_chars**：
  ```python
  # 修改前：
  max_result_chars = 500
  
  # 修改后：
  max_result_chars = 3000  # 容纳视觉分析结果
  ```

- **修改 execute() 方法**：
  ```python
  async def execute(self, filename: str = "screenshot.png", full_page: bool = False,
                    analyze: bool = False, find_element: str = "",
                    _worktree_path: str = "", **kwargs) -> str:
      # ... 截图代码 ...
      
      # 如果指定了 find_element，自动启用 analyze
      if find_element:
          analyze = True
      
      # 执行视觉分析（如果启用）
      if analyze:
          vision_analyzer = get_vision_analyzer()
          if not vision_analyzer.is_available():
              return f"{base_result}\n\n[视觉分析] ⚠️ 未启用（需要配置 ANTHROPIC_AUTH_TOKEN 环境变量）"
          
          # 调用视觉分析
          if find_element:
              vision_result = vision_analyzer.analyze_for_click(
                  image_path=str(filepath),
                  target_description=find_element
              )
          else:
              vision_result = vision_analyzer.analyze_screenshot(
                  image_path=str(filepath)
              )
          
          # 格式化并返回分析结果
          if vision_result.get("success"):
              analysis_text = vision_result.get("analysis", "")
              location_hints = vision_result.get("location_hints", {})
              
              result = f"{base_result}\n\n"
              result += "=" * 60 + "\n"
              result += "[视觉分析结果]\n"
              result += "=" * 60 + "\n"
              
              if find_element:
                  result += f"🎯 查找目标: {find_element}\n\n"
                  if location_hints:
                      result += "【快速定位提示】\n"
                      for key, value in location_hints.items():
                          if value and value.strip():
                              result += f"  • {key}: {value}\n"
                      result += "\n"
              
              result += "【详细分析】\n"
              result += analysis_text + "\n"
              result += "=" * 60 + "\n"
              result += "\n💡 提示: 根据上述视觉分析结果，你可以更精确地构造 CSS 选择器或使用 run_js 定位元素。"
              
              return result
      
      return base_result
  ```

#### `browser_agent_system_v5/core/agent_definition.py` ✅ 修改
**改动位置**：Browser Agent system_prompt (约第 105-120 行)

**修改 1**：screenshot 工具描述
```python
# 修改前：
"- screenshot: 截图保存到数据目录供人类核对（🔥警告: 你自己没有视觉能力，无法"看"到截出的图片内容，请勿试图通过截图来分析页面，分析页面必须用 extract_text 或 run_js）\n"

# 修改后：
"- screenshot: 截图并可选视觉分析（🔥新功能: 支持 analyze=true 或 find_element='按钮描述' 来获取元素位置信息，帮助你精确定位目标元素）\n"
```

**修改 2**：新增"【视觉分析能力（重要！）】"章节
```python
# 在 wait_user 描述后新增：
"【视觉分析能力（重要！）】\n"
"- 当你难以定位页面元素时，使用 screenshot 工具的视觉分析功能：\n"
"  • screenshot(filename='page.png', analyze=true) — 分析整个页面布局和所有交互元素\n"
"  • screenshot(filename='page.png', find_element='登录按钮') — 查找特定元素并获取位置信息\n"
"- 视觉分析会返回元素的位置描述、视觉特征和推荐的 CSS 选择器\n"
"- 这比盲目猜测选择器更高效，尤其是在复杂的电商页面或 SPA 应用中\n\n"
```

---

## 文件归档整理

### 移动的文件

1. **视觉分析文档** → `docs/vision_analysis/`
   - `VISION_ANALYSIS_GUIDE.md`
   - `VISION_INTEGRATION_SUMMARY.md`
   - `QUICK_START_VISION.md`

2. **测试文件** → `tests/`
   - `test_skills.py`
   - `test_vision_integration.py`

### 目录结构（整理后）

```
browser_agent_system_v5/
├── core/
│   ├── agent_definition.py          ✅ 修改（删除网站特定策略 + 新增视觉分析说明）
│   ├── agent_spawner.py             ✅ 修改（传递 skill_registry）
│   ├── execution_loop.py            ✅ 修改（技能注入逻辑）
│   ├── hook_registry.py             ✅ 修改（新增 Hook 事件）
│   ├── prompt_builder.py            ✅ 修改（技能格式化和注入）
│   └── skill_registry.py            ✅ 新建（技能注册表）
├── toolkits/
│   ├── browser_tools.py             ✅ 修改（视觉分析集成）
│   └── vision_helper.py             ✅ 新建（视觉分析模块）
├── skills/
│   └── browser/
│       ├── _template.md             ✅ 新建（技能模板）
│       ├── _security_validator.md   ✅ 新建（安全验证）
│       ├── jd_ecommerce.md          ✅ 新建（京东技能）
│       └── taobao_ecommerce.md      ✅ 新建（淘宝技能）
├── tests/
│   ├── test_skills.py               ✅ 新建（技能测试）
│   └── test_vision_integration.py   ✅ 新建（视觉分析测试）
├── docs/
│   ├── vision_analysis/
│   │   ├── VISION_ANALYSIS_GUIDE.md         ✅ 新建
│   │   ├── VISION_INTEGRATION_SUMMARY.md    ✅ 新建
│   │   └── QUICK_START_VISION.md            ✅ 新建
│   └── CHANGELOG_2026-04-15.md              ✅ 新建（本文档）
└── main.py                          ✅ 修改（初始化 SkillRegistry）
```

---

## 核心改动总结

### 上一轮任务（Skill Injection）
1. **新建 7 个文件**：skill_registry.py + 4 个技能文件 + 1 个模板 + 1 个测试
2. **修改 6 个文件**：prompt_builder.py, hook_registry.py, main.py, execution_loop.py, agent_spawner.py, agent_definition.py
3. **核心逻辑**：在 Browser Agent 首次执行时，根据任务描述和 URL 自动选择并注入匹配的技能

### 本轮任务（Vision Analysis）
1. **新建 5 个文件**：vision_helper.py + 1 个测试 + 3 个文档
2. **修改 2 个文件**：browser_tools.py, agent_definition.py
3. **核心逻辑**：ScreenshotTool 截图后可选调用 MiniMax M2.7 API 进行视觉分析，返回元素位置和选择器建议

---

## 配置要求

### Skill Injection System
- 无需额外配置
- 技能文件放在 `skills/browser/` 目录
- 系统自动加载和匹配

### Vision Analysis
- 需要设置环境变量：
  ```bash
  export MINIMAX_API_KEY="your_minimax_api_key"
  export MINIMAX_API_HOST="https://api.minimaxi.com"  # 可选
  ```
- 使用 MiniMax Coding Plan API 的 `understand_image` 工具
- 支持通过代理访问（默认 http://127.0.0.1:7890）

---

## 重要更新 (2026-04-15 晚)

### Vision Helper API 修正

**问题**：初始实现错误地使用了 Anthropic Messages API 格式调用 MiniMax

**修正**：
- 改用 MiniMax Coding Plan API 的正确调用方式
- 使用 `understand_image` 工具而非 Messages API
- 配置代理支持（http://127.0.0.1:7890）用于访问 GitHub 等资源
- 环境变量从 `ANTHROPIC_AUTH_TOKEN` 改为 `MINIMAX_API_KEY`
- 环境变量从 `ANTHROPIC_BASE_URL` 改为 `MINIMAX_API_HOST`

**影响的文件**：
- `toolkits/vision_helper.py` - 完全重写 API 调用逻辑
- `toolkits/browser_tools.py` - 更新环境变量提示
- `docs/vision_analysis/*.md` - 更新所有文档中的环境变量名称
- `tests/test_vision_integration.py` - 更新测试脚本 + 修复导入路径
- `tests/test_skills.py` - 修复导入路径
- `tests/test_minimax_api.py` - 新增 API 测试工具
- `run_tests.py` - 新增测试运行器

---

## 测试方法

### 测试 Skill Injection
```bash
cd browser_agent_system_v5
python tests/test_skills.py
```

### 测试 Vision Analysis
```bash
# 测试 API 配置
python tests/test_minimax_api.py

# 测试完整集成
python tests/test_vision_integration.py

# 运行所有测试
python run_tests.py
```

---

## 后续建议

1. **Skill Injection**：
   - 添加更多网站的技能文件（Amazon、eBay、1688 等）
   - 实现技能的热重载（无需重启系统）
   - 添加技能的版本管理

2. **Vision Analysis**：
   - 实现结果缓存（避免重复分析同一页面）
   - 支持批量元素查找（一次分析多个元素）
   - 添加坐标定位支持（如果 API 支持）

3. **集成测试**：
   - 创建端到端测试（完整的京东/淘宝搜索流程）
   - 测试技能注入 + 视觉分析的组合使用
   - 性能基准测试

---

## 文件改动统计

- **新建文件**：12 个
- **修改文件**：8 个
- **删除文件**：0 个
- **移动文件**：5 个

**总代码行数变化**：约 +2000 行
