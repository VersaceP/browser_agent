# 视觉分析功能使用指南

## 概述

Browser Agent 现在支持使用 MiniMax M2.7 多模态模型进行截图视觉分析，帮助 Agent 精确定位页面元素，避免盲目猜测 CSS 选择器。

## 配置

### 环境变量

在使用视觉分析功能前，需要配置以下环境变量：

```bash
# 必需：MiniMax API 密钥
export MINIMAX_API_KEY="your_minimax_api_key"

# 可选：API 基础 URL（默认为 https://api.minimaxi.com）
export MINIMAX_API_HOST="https://api.minimaxi.com"
```

### 获取 API 密钥

1. 访问 [MiniMax 开放平台](https://platform.minimaxi.com/)
2. 注册并登录账号
3. 在控制台创建 API 密钥（选择 Coding Plan）
4. 参考文档：https://platform.minimaxi.com/docs/token-plan/mcp-guide

## 使用方法

### 1. 基础截图（无视觉分析）

```python
# 仅截图，不进行视觉分析
screenshot(filename="page.png", full_page=False)
```

### 2. 截图 + 页面分析

```python
# 截图并分析整个页面的布局和交互元素
screenshot(filename="page.png", analyze=True)
```

**返回信息包括：**
- 页面整体布局（顶部、中部、底部区域）
- 所有可见的交互元素（按钮、输入框、链接等）
- 每个元素的位置描述
- 关键文本内容
- 视觉特征（颜色、大小、样式）

### 3. 截图 + 元素查找

```python
# 截图并查找特定元素
screenshot(filename="page.png", find_element="登录按钮")
screenshot(filename="search.png", find_element="搜索框")
screenshot(filename="cart.png", find_element="购物车图标")
```

**返回信息包括：**
- 是否找到目标元素
- 元素的精确位置（水平/垂直位置百分比）
- 相对于其他元素的位置关系
- 视觉特征（颜色、大小、形状、文本）
- 周围环境描述
- 推荐的 CSS 选择器
- 其他定位方法建议

## 使用场景

### 场景 1：复杂电商页面元素定位

```python
# 问题：京东商品页面的"加入购物车"按钮难以定位
# 解决方案：使用视觉分析
screenshot(filename="jd_product.png", find_element="加入购物车按钮")

# 视觉分析会返回：
# - 按钮的精确位置（如"页面右侧中部，价格信息下方"）
# - 推荐的选择器（如 "button.btn-addcart" 或 "#add-to-cart"）
# - 视觉特征（如"红色背景，白色文字，矩形按钮"）
```

### 场景 2：动态加载内容识别

```python
# 问题：淘宝评论区是异步加载的，不确定何时加载完成
# 解决方案：截图分析确认内容已加载
screenshot(filename="taobao_comments.png", find_element="评论列表")

# 根据视觉分析结果判断：
# - 如果找到评论列表 → 继续提取评论
# - 如果未找到 → 需要滚动或等待更长时间
```

### 场景 3：验证码和登录页面识别

```python
# 问题：不确定是否进入了登录页面或验证码页面
# 解决方案：截图分析页面内容
screenshot(filename="current_page.png", analyze=True)

# 根据视觉分析结果判断：
# - 如果发现"登录"、"验证码"等关键词 → 调用 wait_user
# - 如果是正常页面 → 继续自动化流程
```

## 工作流程建议

### 推荐流程

1. **首次访问页面**：使用 `analyze=True` 获取页面整体布局
2. **定位特定元素**：使用 `find_element` 查找目标元素
3. **根据分析结果**：构造精确的 CSS 选择器或使用 `run_js` 定位
4. **执行操作**：使用 `click_element` 或 `fill_form` 等工具

### 示例：京东商品搜索

```python
# 步骤 1：打开京东首页
navigate(url="https://www.jd.com", wait=2)

# 步骤 2：截图并查找搜索框
screenshot(filename="jd_home.png", find_element="搜索框")
# 返回：推荐选择器 "#key" 或 "input.search-input"

# 步骤 3：根据视觉分析结果填写搜索框
fill_form(selector="#key", value="iPhone 15", enter_key=True)

# 步骤 4：等待搜索结果加载
wait(3)

# 步骤 5：截图分析搜索结果页面
screenshot(filename="jd_search_results.png", analyze=True)
# 返回：商品列表位置、筛选条件位置等信息
```

## 性能考虑

### API 调用成本

- 每次视觉分析会调用 MiniMax M2.7 API
- 建议仅在必要时使用（元素难以定位时）
- 对于简单页面，优先使用 `extract_text` 或 `run_js`

### 最佳实践

1. **先尝试简单方法**：`extract_text` → `run_js` → 视觉分析
2. **缓存分析结果**：同一页面不需要重复分析
3. **精确描述元素**：`find_element` 的描述越精确，结果越准确
4. **结合 DOM 诊断**：视觉分析 + `run_js` 诊断 = 最佳定位策略

## 故障排除

### 问题 1：视觉分析不可用

**症状**：截图成功，但提示"视觉分析 ⚠️ 未启用"

**解决方案**：
```bash
# 检查环境变量
echo $MINIMAX_API_KEY

# 如果为空，设置环境变量
export MINIMAX_API_KEY="your_api_key"
```

### 问题 2：视觉分析失败

**症状**：截图成功，但提示"视觉分析 ❌ 失败"

**可能原因**：
1. API 密钥无效或过期
2. 网络连接问题
3. API 配额不足
4. 图片格式不支持

**解决方案**：
1. 验证 API 密钥是否有效
2. 检查网络连接（是否需要代理）
3. 查看 API 控制台的配额使用情况
4. 确保截图格式为 PNG/JPG/JPEG

### 问题 3：元素未找到

**症状**：视觉分析返回"未找到"目标元素

**可能原因**：
1. 元素描述不够精确
2. 元素在截图范围外（需要滚动）
3. 元素被遮挡或隐藏
4. 页面尚未完全加载

**解决方案**：
1. 使用更精确的描述（如"红色的加入购物车按钮"）
2. 先滚动页面，再截图分析
3. 使用 `full_page=True` 截取整个页面
4. 增加 `navigate` 的 `wait` 参数

## 测试

运行测试脚本验证视觉分析功能：

```bash
cd browser_agent_system_v5

# 方法 1：使用测试运行器（推荐）
python run_tests.py

# 方法 2：单独运行测试
python tests/test_minimax_api.py
python tests/test_skills.py
python tests/test_vision_integration.py
```

测试内容包括：
1. 视觉分析器状态检查
2. 浏览器启动
3. 页面导航
4. 基础截图
5. 视觉分析截图
6. 元素查找

## 参考资料

- [MiniMax 开放平台文档](https://platform.minimaxi.com/docs)
- [MiniMax MCP 指南](https://platform.minimaxi.com/docs/token-plan/mcp-guide)
- [Anthropic Messages API](https://docs.anthropic.com/en/api/messages)

## 更新日志

### 2026-04-15
- ✅ 集成 MiniMax M2.7 视觉分析功能
- ✅ 扩展 ScreenshotTool 支持 `analyze` 和 `find_element` 参数
- ✅ 更新 Browser Agent 系统提示词
- ✅ 创建 VisionAnalyzer 类和测试脚本
