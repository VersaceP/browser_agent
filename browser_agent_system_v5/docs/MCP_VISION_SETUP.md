# MCP 视觉分析配置指南

## 概述

使用 MiniMax MCP Server 的 `understand_image` 工具进行网页截图分析。

## 配置步骤

### 1. 安装 uv 和 uvx

```bash
# Windows (使用 pip)
pip install uv

# 或使用 Homebrew (macOS/Linux)
brew install uv
```

### 2. 设置环境变量

```bash
# Windows PowerShell
$env:MINIMAX_API_KEY="your_api_key_here"

# Linux/macOS
export MINIMAX_API_KEY="your_api_key_here"
```

### 3. 测试 MCP Server

```bash
# 启动 MCP server
uvx minimax-coding-plan-mcp

# Server 会通过 stdio 与客户端通信
```

### 4. 在 Agent 中使用

MCP Server 提供两个工具：

#### understand_image

分析图片内容

**参数：**
- `prompt` (string): 分析提示词
- `image_source` (string): 图片路径（本地路径或 URL）

**示例：**
```json
{
  "name": "understand_image",
  "arguments": {
    "prompt": "请描述这个网页的主要内容和布局",
    "image_source": "/path/to/screenshot.png"
  }
}
```

#### web_search

网页搜索（可选）

**参数：**
- `query` (string): 搜索关键词

## 集成方式

### 方式 1: 在 Kiro IDE 中配置 MCP

1. 将 `mcp_config.json` 复制到 `.kiro/settings/mcp.json`
2. 填写你的 `MINIMAX_API_KEY`
3. 重启 Kiro 或重新连接 MCP Server

### 方式 2: 在 Python Agent 中直接调用

如果你的 Agent 框架支持 MCP 协议，可以直接调用 `understand_image` 工具：

```python
# 伪代码示例
result = agent.call_mcp_tool(
    server="minimax",
    tool="understand_image",
    arguments={
        "prompt": "请找出页面中的搜索框位置",
        "image_source": "screenshots/page.png"
    }
)
```

### 方式 3: 使用 vision_helper.py (当前实现)

当前的 `vision_helper.py` 使用 Anthropic SDK 调用 MiniMax API。

**优点：**
- 不需要额外的 MCP Server 进程
- 直接 HTTP API 调用，简单直接

**缺点：**
- 图片大小受限（需要 base64 编码）
- 可能不如 MCP 方式稳定

## 推荐方案

### 短期方案（当前）
继续使用 `vision_helper.py`，但需要：
1. 压缩大图片（< 100KB）
2. 简化提示词
3. 使用较小的测试图片

### 长期方案（推荐）
集成 MCP 协议到 Agent 框架：
1. 使用 MCP 客户端库（如 `mcp` Python 包）
2. 启动 MiniMax MCP Server
3. 通过 MCP 协议调用 `understand_image`

## 故障排查

### 问题 1: uvx 命令不存在
```bash
pip install uv
```

### 问题 2: MCP Server 启动失败
检查环境变量：
```bash
echo $MINIMAX_API_KEY  # Linux/macOS
echo $env:MINIMAX_API_KEY  # Windows PowerShell
```

### 问题 3: 图片太大导致超时
- 压缩图片到 < 100KB
- 或使用 MCP 方式（支持更大的图片）

## 参考资料

- [MiniMax MCP 文档](https://platform.minimax.io/docs/coding-plan/mcp-guide)
- [MCP 协议规范](https://modelcontextprotocol.io/)
- [uv 安装指南](https://docs.astral.sh/uv/getting-started/installation/)
