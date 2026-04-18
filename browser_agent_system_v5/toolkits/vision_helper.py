"""
vision_helper.py — 基于 MCP 的视觉理解辅助模块

使用 MiniMax MCP Server 的 understand_image 工具分析截图

环境变量：
- ANTHROPIC_AUTH_TOKEN: API 密钥（必需）
- ANTHROPIC_BASE_URL: API 地址（可选，默认 https://api.minimaxi.com/anthropic）
"""

import os
import asyncio
from pathlib import Path
from typing import Optional, Dict, Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class VisionAnalyzer:
    """
    基于 MCP 的视觉分析器
    
    使用 MiniMax MCP Server 提供的 understand_image 工具
    """
    
    def __init__(self):
        """初始化 MCP 视觉分析器"""
        self.api_key = os.getenv("ANTHROPIC_AUTH_TOKEN", "")
        self.api_host = os.getenv("ANTHROPIC_BASE_URL", "https://api.minimaxi.com/anthropic")
        self.mcp_base_path = Path("./mcp_output")
        self.mcp_base_path.mkdir(exist_ok=True)
        
        if self.api_key:
            print("[VisionAnalyzer] API 密钥已加载")
            print(f"[VisionAnalyzer] API 地址: {self.api_host}")
        else:
            print("[VisionAnalyzer] 未配置 ANTHROPIC_AUTH_TOKEN 环境变量")
    
    def is_available(self) -> bool:
        """检查 MCP 视觉分析功能是否可用"""
        return bool(self.api_key)
    
    def _get_server_params(self) -> StdioServerParameters:
        """获取 MCP Server 参数"""
        return StdioServerParameters(
            command="uvx",
            args=["minimax-coding-plan-mcp"],
            env={
                "MINIMAX_API_KEY": self.api_key,
                "MINIMAX_MCP_BASE_PATH": str(self.mcp_base_path.absolute()),
                "MINIMAX_API_HOST": "https://api.minimaxi.com",
                "MINIMAX_API_RESOURCE_MODE": "local"
            }
        )
    
    async def analyze_screenshot_async(
        self,
        image_path: str,
        query: str = "",
        find_element: str = ""
    ) -> Dict[str, Any]:
        """
        异步分析截图内容
        
        :param image_path: 截图文件路径
        :param query: 自定义查询（可选）
        :param find_element: 要查找的元素描述
        :return: 分析结果字典
        """
        if not self.is_available():
            return {"success": False, "error": "MCP 功能不可用：未配置 API 密钥"}
        
        if not Path(image_path).exists():
            return {"success": False, "error": f"截图文件不存在: {image_path}"}
        
        try:
            # 构建提示词
            if find_element:
                prompt = self._build_element_location_prompt(find_element)
            elif query:
                prompt = query
            else:
                prompt = self._build_default_analysis_prompt()
            
            # 使用 async with 确保资源正确管理
            async with stdio_client(self._get_server_params()) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    
                    # 调用 understand_image 工具
                    result = await session.call_tool(
                        "understand_image",
                        arguments={
                            "prompt": prompt,
                            "image_source": str(Path(image_path).absolute())
                        }
                    )
                    
                    # 提取结果
                    if result.content:
                        analysis_text = ""
                        for content in result.content:
                            if hasattr(content, 'text'):
                                analysis_text += content.text
                        
                        return {
                            "success": True,
                            "analysis": analysis_text,
                            "model": "MiniMax-MCP",
                            "image_path": image_path
                        }
                    else:
                        return {
                            "success": False,
                            "error": "MCP 返回空结果"
                        }
            
        except Exception as e:
            import traceback
            error_detail = traceback.format_exc()
            print(f"[VisionAnalyzer] 错误详情:\n{error_detail}")
            return {"success": False, "error": f"MCP 调用失败: {str(e)}"}
    
    def analyze_screenshot(
        self,
        image_path: str,
        query: str = "",
        find_element: str = ""
    ) -> Dict[str, Any]:
        """
        同步分析截图内容（包装异步方法）
        
        :param image_path: 截图文件路径
        :param query: 自定义查询（可选）
        :param find_element: 要查找的元素描述
        :return: 分析结果字典
        """
        try:
            # 获取或创建事件循环
            try:
                loop = asyncio.get_running_loop()
                # 如果已经在事件循环中，创建任务
                return asyncio.create_task(
                    self.analyze_screenshot_async(image_path, query, find_element)
                )
            except RuntimeError:
                # 没有运行中的事件循环，创建新的
                return asyncio.run(
                    self.analyze_screenshot_async(image_path, query, find_element)
                )
        except Exception as e:
            return {"success": False, "error": f"同步调用失败: {str(e)}"}
    
    def _build_default_analysis_prompt(self) -> str:
        """构建默认分析提示词"""
        return """请详细分析这张网页截图，包括：

1. 页面布局：描述页面的整体结构
2. 交互元素：列出所有可见的按钮、输入框、链接
3. 元素位置：描述重要元素的位置
4. 文本内容：提取页面上的关键文本
5. 视觉特征：描述元素的颜色、大小、样式

请用结构化的方式组织回答。"""
    
    def _build_element_location_prompt(self, element_description: str) -> str:
        """构建元素定位提示词"""
        return f"""请在这张网页截图中找到"{element_description}"，并提供以下信息：

1. 是否找到该元素
2. 元素的位置（左/中/右，上/中/下）
3. 视觉特征（颜色、大小、形状、文本）
4. 周围有哪些其他元素
5. 推荐的定位方法（CSS选择器、ID、class等）

请用清晰、结构化的方式回答。"""
    
    def analyze_for_click(
        self,
        image_path: str,
        target_description: str
    ) -> Dict[str, Any]:
        """
        专门用于点击操作的视觉分析
        
        :param image_path: 截图文件路径
        :param target_description: 目标元素描述
        :return: 包含定位信息的分析结果
        """
        result = self.analyze_screenshot(
            image_path=image_path,
            find_element=target_description
        )
        
        if result.get("success"):
            analysis = result.get("analysis", "")
            result["location_hints"] = self._extract_location_hints(analysis)
        
        return result
    
    def _extract_location_hints(self, analysis_text: str) -> Dict[str, str]:
        """从分析文本中提取定位提示"""
        hints = {
            "found": "未知",
            "position": "",
            "features": "",
            "selector_suggestions": ""
        }
        
        text_lower = analysis_text.lower()
        
        if "找到" in analysis_text or "found" in text_lower:
            hints["found"] = "是"
        elif "未找到" in analysis_text or "not found" in text_lower:
            hints["found"] = "否"
        
        for line in analysis_text.split('\n'):
            if "位置" in line or "position" in line.lower():
                hints["position"] += line.strip() + " "
            elif "特征" in line or "feature" in line.lower():
                hints["features"] += line.strip() + " "
            elif "选择器" in line or "selector" in line.lower():
                hints["selector_suggestions"] += line.strip() + " "
        
        return hints


# 全局单例
_vision_analyzer = None


def get_vision_analyzer() -> VisionAnalyzer:
    """获取全局视觉分析器实例"""
    global _vision_analyzer
    if _vision_analyzer is None:
        _vision_analyzer = VisionAnalyzer()
    return _vision_analyzer
