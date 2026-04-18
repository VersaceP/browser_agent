"""
Unit tests for toolkits/tool_registry.py

Tests the tool registration, filtering, and dispatch system.
"""

import pytest

from browser_agent_system_v5.toolkits.tool_registry import ToolRegistry
from browser_agent_system_v5.toolkits.base_tool import BaseTool
from browser_agent_system_v5.core.agent_definition import AgentDefinition, TrustLevel


class MockTool(BaseTool):
    """Mock tool for testing"""
    name = "mock_tool"
    description = "A mock tool for testing"
    input_schema = {
        "type": "object",
        "properties": {
            "param1": {"type": "string"}
        }
    }
    required_trust_level = TrustLevel.WRITE
    is_destructive = False
    
    async def execute(self, **kwargs):
        return f"Mock tool executed with {kwargs}"


class AdminMockTool(BaseTool):
    """Mock tool requiring ADMIN trust level"""
    name = "admin_tool"
    description = "A tool requiring admin privileges"
    input_schema = {"type": "object", "properties": {}}
    required_trust_level = TrustLevel.ADMIN
    is_destructive = True
    
    async def execute(self, **kwargs):
        return "Admin tool executed"


class ReadOnlyMockTool(BaseTool):
    """Mock tool for read-only operations"""
    name = "readonly_tool"
    description = "A read-only tool"
    input_schema = {"type": "object", "properties": {}}
    required_trust_level = TrustLevel.READONLY
    is_destructive = False
    
    async def execute(self, **kwargs):
        return "Read-only tool executed"


class TestToolRegistry:
    """Test ToolRegistry initialization and basic operations"""
    
    def test_tool_registry_initialization(self):
        """Test creating a new ToolRegistry"""
        registry = ToolRegistry()
        assert len(registry) == 0
        assert registry.list_tools() == []
    
    @pytest.mark.asyncio
    async def test_register_adds_tool(self):
        """Test that register() adds a tool to the registry"""
        registry = ToolRegistry()
        tool = MockTool()
        
        registry.register(tool)
        
        assert len(registry) == 1
        assert "mock_tool" in registry.list_tools()
        assert registry.get_tool("mock_tool") == tool
    
    @pytest.mark.asyncio
    async def test_register_many_adds_multiple_tools(self):
        """Test that register_many() adds multiple tools"""
        registry = ToolRegistry()
        tools = [MockTool(), AdminMockTool(), ReadOnlyMockTool()]
        
        registry.register_many(tools)
        
        assert len(registry) == 3
        assert "mock_tool" in registry.list_tools()
        assert "admin_tool" in registry.list_tools()
        assert "readonly_tool" in registry.list_tools()
    
    def test_get_tool_retrieves_tool(self):
        """Test that get_tool() retrieves registered tool"""
        registry = ToolRegistry()
        tool = MockTool()
        registry.register(tool)
        
        retrieved = registry.get_tool("mock_tool")
        
        assert retrieved == tool
        assert retrieved.name == "mock_tool"
    
    def test_get_tool_returns_none_for_missing_tool(self):
        """Test that get_tool() returns None for non-existent tool"""
        registry = ToolRegistry()
        
        result = registry.get_tool("nonexistent_tool")
        
        assert result is None


class TestToolFiltering:
    """Test tool filtering by agent definition"""
    
    def test_filter_tools_by_trust_level(self):
        """Test filtering tools by trust level"""
        registry = ToolRegistry()
        registry.register_many([MockTool(), AdminMockTool(), ReadOnlyMockTool()])
        
        # Agent with WRITE trust level
        agent_write = AgentDefinition(
            agent_type="test",
            system_prompt="test",
            trust_level=TrustLevel.WRITE
        )
        filtered = registry.filter_tools(agent_write)
        tool_names = [t.name for t in filtered]
        
        # Should include READONLY and WRITE tools, but not ADMIN
        assert "readonly_tool" in tool_names
        assert "mock_tool" in tool_names
        assert "admin_tool" not in tool_names
    
    def test_filter_tools_by_whitelist(self):
        """Test filtering tools by whitelist"""
        registry = ToolRegistry()
        registry.register_many([MockTool(), AdminMockTool(), ReadOnlyMockTool()])
        
        # Agent with whitelist
        agent = AgentDefinition(
            agent_type="test",
            system_prompt="test",
            allowed_tools=["mock_tool", "readonly_tool"],
            trust_level=TrustLevel.ADMIN  # High trust but limited by whitelist
        )
        filtered = registry.filter_tools(agent)
        tool_names = [t.name for t in filtered]
        
        # Should only include whitelisted tools
        assert "mock_tool" in tool_names
        assert "readonly_tool" in tool_names
        assert "admin_tool" not in tool_names
    
    def test_filter_tools_by_blacklist(self):
        """Test filtering tools by blacklist"""
        registry = ToolRegistry()
        registry.register_many([MockTool(), AdminMockTool(), ReadOnlyMockTool()])
        
        # Agent with blacklist
        agent = AgentDefinition(
            agent_type="test",
            system_prompt="test",
            disallowed_tools=["admin_tool"],
            trust_level=TrustLevel.ADMIN
        )
        filtered = registry.filter_tools(agent)
        tool_names = [t.name for t in filtered]
        
        # Should exclude blacklisted tool
        assert "mock_tool" in tool_names
        assert "readonly_tool" in tool_names
        assert "admin_tool" not in tool_names
    
    def test_filter_tools_readonly_agent_excludes_destructive(self):
        """Test that read-only agents cannot use destructive tools"""
        registry = ToolRegistry()
        registry.register_many([MockTool(), AdminMockTool(), ReadOnlyMockTool()])
        
        # Read-only agent
        agent = AgentDefinition(
            agent_type="test",
            system_prompt="test",
            trust_level=TrustLevel.ADMIN,  # High trust
            is_read_only=True  # But read-only flag
        )
        filtered = registry.filter_tools(agent)
        tool_names = [t.name for t in filtered]
        
        # Should exclude destructive tools
        assert "readonly_tool" in tool_names
        assert "mock_tool" in tool_names  # Not destructive
        assert "admin_tool" not in tool_names  # Destructive
    
    def test_filter_tools_empty_whitelist_allows_all(self):
        """Test that empty whitelist allows all tools (subject to trust level)"""
        registry = ToolRegistry()
        registry.register_many([MockTool(), ReadOnlyMockTool()])
        
        # Agent with empty whitelist
        agent = AgentDefinition(
            agent_type="test",
            system_prompt="test",
            allowed_tools=[],  # Empty whitelist
            trust_level=TrustLevel.WRITE
        )
        filtered = registry.filter_tools(agent)
        
        # Should include all tools within trust level
        assert len(filtered) == 2


class TestSchemaGeneration:
    """Test schema generation for LLM"""
    
    def test_get_schemas_generates_schemas(self):
        """Test that get_schemas() generates tool schemas"""
        registry = ToolRegistry()
        registry.register(MockTool())
        
        agent = AgentDefinition(
            agent_type="test",
            system_prompt="test",
            trust_level=TrustLevel.WRITE
        )
        schemas = registry.get_schemas(agent)
        
        assert len(schemas) == 1
        assert schemas[0]["name"] == "mock_tool"
        assert schemas[0]["description"] == "A mock tool for testing"
        assert "input_schema" in schemas[0]
    
    def test_get_schemas_respects_filtering(self):
        """Test that get_schemas() respects agent filtering"""
        registry = ToolRegistry()
        registry.register_many([MockTool(), AdminMockTool()])
        
        # Agent with WRITE trust level (cannot use ADMIN tools)
        agent = AgentDefinition(
            agent_type="test",
            system_prompt="test",
            trust_level=TrustLevel.WRITE
        )
        schemas = registry.get_schemas(agent)
        
        # Should only include mock_tool schema
        assert len(schemas) == 1
        assert schemas[0]["name"] == "mock_tool"


class TestToolDispatch:
    """Test tool execution dispatch"""
    
    @pytest.mark.asyncio
    async def test_dispatch_executes_tool(self, temp_worktree):
        """Test that dispatch() executes the tool"""
        registry = ToolRegistry()
        registry.register(MockTool())
        
        agent = AgentDefinition(
            agent_type="test",
            system_prompt="test",
            trust_level=TrustLevel.WRITE
        )
        
        result = await registry.dispatch(
            tool_name="mock_tool",
            params={"param1": "value1"},
            agent_def=agent,
            worktree_path=str(temp_worktree),
            session_id="test_session"
        )
        
        assert "Mock tool executed" in result
        assert "param1" in result
    
    @pytest.mark.asyncio
    async def test_dispatch_blocks_insufficient_trust_level(self, temp_worktree):
        """Test that dispatch() blocks tools requiring higher trust level"""
        registry = ToolRegistry()
        registry.register(AdminMockTool())
        
        # Agent with WRITE trust level trying to use ADMIN tool
        agent = AgentDefinition(
            agent_type="test",
            system_prompt="test",
            trust_level=TrustLevel.WRITE
        )
        
        result = await registry.dispatch(
            tool_name="admin_tool",
            params={},
            agent_def=agent,
            worktree_path=str(temp_worktree),
            session_id="test_session"
        )
        
        assert "权限拒绝" in result
        assert "ADMIN" in result
    
    @pytest.mark.asyncio
    async def test_dispatch_blocks_readonly_agent_destructive_tool(self, temp_worktree):
        """Test that dispatch() blocks destructive tools for read-only agents"""
        registry = ToolRegistry()
        registry.register(AdminMockTool())
        
        # Read-only agent with ADMIN trust level
        agent = AgentDefinition(
            agent_type="test",
            system_prompt="test",
            trust_level=TrustLevel.ADMIN,
            is_read_only=True
        )
        
        result = await registry.dispatch(
            tool_name="admin_tool",
            params={},
            agent_def=agent,
            worktree_path=str(temp_worktree),
            session_id="test_session"
        )
        
        assert "只读拦截" in result
    
    @pytest.mark.asyncio
    async def test_dispatch_returns_error_for_nonexistent_tool(self, temp_worktree):
        """Test that dispatch() returns error for non-existent tool"""
        registry = ToolRegistry()
        
        agent = AgentDefinition(
            agent_type="test",
            system_prompt="test",
            trust_level=TrustLevel.WRITE
        )
        
        result = await registry.dispatch(
            tool_name="nonexistent_tool",
            params={},
            agent_def=agent,
            worktree_path=str(temp_worktree),
            session_id="test_session"
        )
        
        assert "系统错误" in result
        assert "不存在" in result
