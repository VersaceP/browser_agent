"""
Unit tests for toolkits/base_tool.py

Tests the BaseTool abstract class and its safe_execute mechanism.
"""

import pytest
from pathlib import Path

from browser_agent_system_v5.toolkits.base_tool import BaseTool
from browser_agent_system_v5.core.agent_definition import TrustLevel


class SimpleMockTool(BaseTool):
    """Simple mock tool for testing"""
    name = "simple_mock"
    description = "A simple mock tool"
    input_schema = {"type": "object", "properties": {}}
    
    async def execute(self, **kwargs):
        return "Simple result"


class LongOutputMockTool(BaseTool):
    """Mock tool that returns long output"""
    name = "long_output_mock"
    description = "Returns long output"
    input_schema = {"type": "object", "properties": {}}
    max_result_chars = 100  # Set low threshold for testing
    
    async def execute(self, **kwargs):
        # Return output longer than max_result_chars
        return "x" * 200


class ErrorMockTool(BaseTool):
    """Mock tool that raises an error"""
    name = "error_mock"
    description = "Raises an error"
    input_schema = {"type": "object", "properties": {}}
    
    async def execute(self, **kwargs):
        raise ValueError("Test error message")


class TestBaseTool:
    """Test BaseTool basic functionality"""
    
    def test_to_schema_generates_schema(self):
        """Test that to_schema() generates correct schema"""
        tool = SimpleMockTool()
        schema = tool.to_schema()
        
        assert schema["name"] == "simple_mock"
        assert schema["description"] == "A simple mock tool"
        assert "input_schema" in schema
        assert schema["input_schema"]["type"] == "object"
    
    @pytest.mark.asyncio
    async def test_safe_execute_calls_execute(self):
        """Test that safe_execute() calls execute()"""
        tool = SimpleMockTool()
        result = await tool.safe_execute()
        
        assert result == "Simple result"
    
    @pytest.mark.asyncio
    async def test_safe_execute_passes_parameters(self):
        """Test that safe_execute() passes parameters to execute()"""
        class ParamMockTool(BaseTool):
            name = "param_mock"
            description = "Test"
            input_schema = {"type": "object", "properties": {}}
            
            async def execute(self, **kwargs):
                return f"Received: {kwargs.get('test_param')}"
        
        tool = ParamMockTool()
        result = await tool.safe_execute(test_param="test_value")
        
        assert "test_value" in result
    
    @pytest.mark.asyncio
    async def test_safe_execute_truncates_long_output(self, temp_worktree):
        """Test that safe_execute() truncates output exceeding max_result_chars"""
        tool = LongOutputMockTool()
        result = await tool.safe_execute(
            _worktree_path=str(temp_worktree),
            _session_id="test_session"
        )
        
        # Result should be truncated
        assert len(result) > 100  # Includes truncation message
        assert "输出已截断" in result
        assert "完整内容已自动落盘" in result
    
    @pytest.mark.asyncio
    async def test_safe_execute_spills_to_file(self, temp_worktree):
        """Test that safe_execute() spills long output to file"""
        tool = LongOutputMockTool()
        result = await tool.safe_execute(
            _worktree_path=str(temp_worktree),
            _session_id="test_session"
        )
        
        # Check that spill file was created
        data_dir = temp_worktree / "data"
        spill_files = list(data_dir.glob("spill_*.txt"))
        
        assert len(spill_files) > 0
        
        # Check spill file content
        spill_file = spill_files[0]
        content = spill_file.read_text()
        assert len(content) == 200  # Original output length
        assert content == "x" * 200
    
    @pytest.mark.asyncio
    async def test_safe_execute_includes_file_list(self, temp_worktree):
        """Test that safe_execute() includes list of spilled files"""
        tool = LongOutputMockTool()
        result = await tool.safe_execute(
            _worktree_path=str(temp_worktree),
            _session_id="test_session"
        )
        
        assert "待读溢出文件清单" in result
        assert "spill_" in result
    
    @pytest.mark.asyncio
    async def test_safe_execute_handles_errors(self):
        """Test that safe_execute() handles errors gracefully"""
        tool = ErrorMockTool()
        result = await tool.safe_execute()
        
        assert "工具执行错误" in result
        assert "Test error message" in result
    
    @pytest.mark.asyncio
    async def test_safe_execute_without_worktree_no_spill(self):
        """Test that safe_execute() doesn't spill without worktree"""
        tool = LongOutputMockTool()
        result = await tool.safe_execute()  # No worktree_path
        
        # Should return full output without spilling
        assert len(result) == 200
        assert result == "x" * 200


class TestToolProperties:
    """Test tool property defaults and customization"""
    
    def test_default_properties(self):
        """Test that tools have correct default properties"""
        tool = SimpleMockTool()
        
        assert tool.is_destructive == False
        assert tool.max_result_chars == 1500
        assert tool.required_trust_level == TrustLevel.WRITE
    
    def test_custom_properties(self):
        """Test that tool properties can be customized"""
        class CustomTool(BaseTool):
            name = "custom"
            description = "Custom tool"
            input_schema = {"type": "object", "properties": {}}
            is_destructive = True
            max_result_chars = 5000
            required_trust_level = TrustLevel.ADMIN
            
            async def execute(self, **kwargs):
                return "result"
        
        tool = CustomTool()
        
        assert tool.is_destructive == True
        assert tool.max_result_chars == 5000
        assert tool.required_trust_level == TrustLevel.ADMIN


class TestSpillFileNaming:
    """Test spill file naming and organization"""
    
    @pytest.mark.asyncio
    async def test_spill_file_includes_tool_name(self, temp_worktree):
        """Test that spill file name includes tool name"""
        tool = LongOutputMockTool()
        await tool.safe_execute(
            _worktree_path=str(temp_worktree),
            _session_id="test_session"
        )
        
        data_dir = temp_worktree / "data"
        spill_files = list(data_dir.glob("spill_*.txt"))
        
        assert len(spill_files) > 0
        assert "long_output_mock" in spill_files[0].name
    
    @pytest.mark.asyncio
    async def test_multiple_spills_create_multiple_files(self, temp_worktree):
        """Test that multiple spills create separate files"""
        tool = LongOutputMockTool()
        
        # Execute twice
        await tool.safe_execute(
            _worktree_path=str(temp_worktree),
            _session_id="test_session"
        )
        await tool.safe_execute(
            _worktree_path=str(temp_worktree),
            _session_id="test_session"
        )
        
        data_dir = temp_worktree / "data"
        spill_files = list(data_dir.glob("spill_*.txt"))
        
        # Should have 2 separate files
        assert len(spill_files) == 2
