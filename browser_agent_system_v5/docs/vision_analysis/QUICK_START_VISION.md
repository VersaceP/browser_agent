# 视觉分析快速开始

## 1分钟快速配置

### 步骤 1：设置 API 密钥

```bash
# Windows (PowerShell)
$env:MINIMAX_API_KEY="your_minimax_api_key"
$env:MINIMAX_API_HOST="https://api.minimaxi.com"  # 可选，默认值

# Linux/Mac
export MINIMAX_API_KEY="your_minimax_api_key"
export MINIMAX_API_HOST="https://api.minimaxi.com"  # 可选，默认值
```

### 步骤 2：测试功能

```bash
cd browser_agent_system_v5

# 使用测试运行器
python run_tests.py

# 或单独测试 API
python tests/test_minimax_api.py
```

## 常用命令

### 分析整个页面
```python
screenshot(filename="page.png", analyze=True)
```

### 查找特定元素
```python
screenshot(filename="page.png", find_element="登录按钮")
screenshot(filename="page.png", find_element="搜索框")
screenshot(filename="page.png", find_element="购物车图标")
```

### 普通截图（不分析）
```python
screenshot(filename="page.png")
```

## 典型工作流

```python
# 1. 打开页面
navigate(url="https://www.jd.com", wait=2)

# 2. 截图并查找元素
screenshot(filename="jd.png", find_element="搜索框")
# 返回：推荐选择器 "#key"

# 3. 使用推荐的选择器操作元素
fill_form(selector="#key", value="iPhone 15", enter_key=True)
```

## 何时使用视觉分析？

✅ **应该使用**：
- 复杂的电商页面（京东、淘宝）
- 动态加载的 SPA 应用
- 元素选择器难以确定
- 连续 2 次定位失败后

❌ **不需要使用**：
- 简单的静态页面
- 已知选择器的元素
- 纯文本提取任务

## 故障排除

### 问题：视觉分析不可用
```bash
# 检查环境变量
echo $MINIMAX_API_KEY  # Linux/Mac
echo $env:MINIMAX_API_KEY  # Windows PowerShell

# 如果为空，重新设置
export MINIMAX_API_KEY="your_api_key"
```

### 问题：元素未找到
- 使用更精确的描述（如"红色的加入购物车按钮"）
- 先滚动页面，再截图
- 使用 `full_page=True` 截取整个页面

## 获取 API 密钥

1. 访问：https://platform.minimaxi.com/
2. 注册并登录
3. 创建 API 密钥
4. 复制密钥并设置环境变量

## 更多信息

- 完整指南：[VISION_ANALYSIS_GUIDE.md](./VISION_ANALYSIS_GUIDE.md)
- 实现总结：[VISION_INTEGRATION_SUMMARY.md](./VISION_INTEGRATION_SUMMARY.md)
- MiniMax 文档：https://platform.minimaxi.com/docs
