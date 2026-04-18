# MiniMax Coding Plan API 调用说明

## API 信息

- **服务名称**: MiniMax Coding Plan
- **工具名称**: `understand_image`
- **API 文档**: https://platform.minimaxi.com/docs/token-plan/mcp-guide

## 环境变量配置

```bash
# 必需
export MINIMAX_API_KEY="your_api_key"

# 可选（默认值）
export MINIMAX_API_HOST="https://api.minimaxi.com"
```

## API 调用方式

### 当前实现

```python
import requests

url = f"{api_host}/v1/tools/understand_image"

headers = {
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json"
}

payload = {
    "prompt": "分析这张图片...",
    "image_source": "/absolute/path/to/image.png"
}

# 使用代理（用于访问 GitHub 等资源）
proxies = {
    "http": "http://127.0.0.1:7890",
    "https": "http://127.0.0.1:7890"
}

response = requests.post(
    url,
    headers=headers,
    json=payload,
    proxies=proxies,
    timeout=30
)

result = response.json()
analysis_text = result.get("content", result.get("result", ""))
```

## 注意事项

### 1. 图片路径
- 必须使用**绝对路径**
- 支持的格式：JPEG, PNG, WebP
- 最大文件大小：20 MB

### 2. 代理配置
- 默认使用 `http://127.0.0.1:7890` 代理
- 用于访问需要代理的资源（如 GitHub）
- 如果不需要代理，可以移除 `proxies` 参数

### 3. API 响应格式
响应可能包含以下字段：
- `content`: 分析结果文本
- `result`: 分析结果文本（备用字段）
- `error`: 错误信息（如果失败）

### 4. 错误处理
常见错误：
- HTTP 401: API 密钥无效
- HTTP 403: API 配额不足
- HTTP 404: API 端点不存在
- HTTP 500: 服务器错误
- 网络超时: 检查代理配置和网络连接

## 测试方法

### 1. 测试 API 配置

```bash
cd browser_agent_system_v5
python tests/test_minimax_api.py
```

### 2. 测试完整集成

```bash
python tests/test_vision_integration.py
```

## 与 Anthropic API 的区别

| 特性 | Anthropic Messages API | MiniMax Coding Plan API |
|------|------------------------|-------------------------|
| 调用方式 | `client.messages.create()` | `requests.post()` |
| 图片格式 | base64 编码 | 文件路径 |
| 环境变量 | `ANTHROPIC_AUTH_TOKEN` | `MINIMAX_API_KEY` |
| API 端点 | `/v1/messages` | `/v1/tools/understand_image` |
| 模型参数 | `model="MiniMax-M2.7"` | 无需指定（自动使用） |

## 常见问题

### Q: 为什么需要代理？
A: 用于访问 GitHub 等需要代理的资源。如果你的网络环境不需要代理，可以移除 `proxies` 参数。

### Q: 图片路径必须是绝对路径吗？
A: 是的，MiniMax API 要求使用绝对路径。代码中已自动转换：
```python
abs_image_path = str(Path(image_path).absolute())
```

### Q: 如何获取 API 密钥？
A: 
1. 访问 https://platform.minimaxi.com/
2. 注册并登录
3. 在控制台创建 API 密钥
4. 选择 "Coding Plan" 类型

### Q: API 调用失败怎么办？
A: 
1. 检查环境变量是否正确设置
2. 检查 API 密钥是否有效
3. 检查网络连接和代理配置
4. 查看 API 配额是否充足
5. 运行 `python tests/test_minimax_api.py` 进行诊断

## 参考资料

- [MiniMax 开放平台](https://platform.minimaxi.com/)
- [MiniMax MCP 指南](https://platform.minimaxi.com/docs/token-plan/mcp-guide)
- [MiniMax API 文档](https://platform.minimaxi.com/docs/api-reference/api-overview)

## 更新历史

- **2026-04-15**: 修正 API 调用方式，从 Anthropic Messages API 改为 MiniMax Coding Plan API
- **2026-04-15**: 添加代理支持（http://127.0.0.1:7890）
- **2026-04-15**: 创建 API 测试工具
