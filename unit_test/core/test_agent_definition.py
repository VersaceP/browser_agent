"""
Unit tests for core/agent_definition.py

Tests the Agent definition data structures and builtin agent configurations.
"""

import pytest

from browser_agent_system_v5.core.agent_definition import (
    AgentDefinition,
    TrustLevel,
    build_builtin_agents
)


class TestTrustLevel:
    """Test TrustLevel enum"""
    
    def test_trust_level_values(self):
        """Test that TrustLevel enum has correct values"""
        assert TrustLevel.READONLY == 1
        assert TrustLevel.WRITE == 2
        assert TrustLevel.ADMIN == 3
    
    def test_trust_level_ordering(self):
        """Test that TrustLevel values are properly ordered"""
        assert TrustLevel.READONLY < TrustLevel.WRITE
        assert TrustLevel.WRITE < TrustLevel.ADMIN
        assert TrustLevel.READONLY < TrustLevel.ADMIN


class TestAgentDefinition:
    """Test AgentDefinition data class"""
    
    def test_agent_definition_creation_with_valid_parameters(self):
        """Test creating an AgentDefinition with valid parameters"""
        agent = AgentDefinition(
            agent_type="test_agent",
            system_prompt="Test system prompt",
            allowed_tools=["tool1", "tool2"],
            disallowed_tools=["tool3"],
            trust_level=TrustLevel.WRITE,
            max_turns=20,
            is_read_only=False,
            can_spawn=True
        )
        
        assert agent.agent_type == "test_agent"
        assert agent.system_prompt == "Test system prompt"
        assert agent.allowed_tools == ["tool1", "tool2"]
        assert agent.disallowed_tools == ["tool3"]
        assert agent.trust_level == TrustLevel.WRITE
        assert agent.max_turns == 20
        assert agent.is_read_only == False
        assert agent.can_spawn == True
    
    def test_agent_definition_with_defaults(self):
        """Test AgentDefinition with default values"""
        agent = AgentDefinition(
            agent_type="minimal_agent",
            system_prompt="Minimal prompt"
        )
        
        assert agent.agent_type == "minimal_agent"
        assert agent.system_prompt == "Minimal prompt"
        assert agent.allowed_tools == []
        assert agent.disallowed_tools == []
        assert agent.trust_level == TrustLevel.WRITE
        assert agent.max_turns == 50
        assert agent.is_read_only == False
        assert agent.can_spawn == False
    
    def test_agent_definition_readonly_agent(self):
        """Test creating a read-only agent"""
        agent = AgentDefinition(
            agent_type="readonly_agent",
            system_prompt="Read-only prompt",
            trust_level=TrustLevel.READONLY,
            is_read_only=True
        )
        
        assert agent.trust_level == TrustLevel.READONLY
        assert agent.is_read_only == True
    
    def test_agent_definition_admin_agent(self):
        """Test creating an admin-level agent"""
        agent = AgentDefinition(
            agent_type="admin_agent",
            system_prompt="Admin prompt",
            trust_level=TrustLevel.ADMIN
        )
        
        assert agent.trust_level == TrustLevel.ADMIN


class TestBuiltinAgents:
    """Test builtin agent configurations"""
    
    def test_build_builtin_agents_returns_4_agents(self):
        """Test that build_builtin_agents() returns 4 agents"""
        agents = build_builtin_agents()
        
        assert len(agents) == 4
        assert "lead" in agents
        assert "browser" in agents
        assert "coding" in agents
        assert "verification" in agents
    
    def test_lead_agent_configuration(self):
        """Test lead agent configuration"""
        agents = build_builtin_agents()
        lead = agents["lead"]
        
        assert lead.agent_type == "lead"
        assert lead.trust_level == TrustLevel.WRITE
        assert lead.can_spawn == True
        assert lead.is_read_only == False
        assert lead.max_turns == 30
        
        # Check allowed tools
        assert "submit_plan" in lead.allowed_tools
        assert "spawn_agent" in lead.allowed_tools
        assert "spawn_agents_parallel" in lead.allowed_tools
        assert "init_progress" in lead.allowed_tools
        assert "update_progress" in lead.allowed_tools
        
        # Lead agent should have system prompt
        assert "Lead Agent" in lead.system_prompt
        assert "submit_plan" in lead.system_prompt
    
    def test_browser_agent_configuration(self):
        """Test browser agent configuration"""
        agents = build_builtin_agents()
        browser = agents["browser"]
        
        assert browser.agent_type == "browser"
        assert browser.trust_level == TrustLevel.WRITE
        assert browser.can_spawn == False
        assert browser.is_read_only == False
        assert browser.max_turns == 100
        
        # Check allowed tools
        assert "navigate" in browser.allowed_tools
        assert "click_element" in browser.allowed_tools
        assert "extract_text" in browser.allowed_tools
        assert "screenshot" in browser.allowed_tools
        assert "fill_form" in browser.allowed_tools
        assert "run_js" in browser.allowed_tools
        assert "write_file" in browser.allowed_tools
        assert "read_file" in browser.allowed_tools
        
        # Check disallowed tools
        assert "run_python" in browser.disallowed_tools
        assert "spawn_agent" in browser.disallowed_tools
        
        # Browser agent should have system prompt
        assert "Browser Agent" in browser.system_prompt
    
    def test_coding_agent_configuration(self):
        """Test coding agent configuration"""
        agents = build_builtin_agents()
        coding = agents["coding"]
        
        assert coding.agent_type == "coding"
        assert coding.trust_level == TrustLevel.ADMIN
        assert coding.can_spawn == False
        assert coding.is_read_only == False
        assert coding.max_turns == 50
        
        # Check allowed tools
        assert "run_python" in coding.allowed_tools
        assert "write_file" in coding.allowed_tools
        assert "read_file" in coding.allowed_tools
        assert "update_progress" in coding.allowed_tools
        
        # Check disallowed tools
        assert "navigate" in coding.disallowed_tools
        assert "click_element" in coding.disallowed_tools
        assert "extract_text" in coding.disallowed_tools
        assert "spawn_agent" in coding.disallowed_tools
        
        # Coding agent should have system prompt
        assert "Coding Agent" in coding.system_prompt
    
    def test_verification_agent_configuration(self):
        """Test verification agent configuration"""
        agents = build_builtin_agents()
        verification = agents["verification"]
        
        assert verification.agent_type == "verification"
        assert verification.trust_level == TrustLevel.READONLY
        assert verification.can_spawn == False
        assert verification.is_read_only == True
        assert verification.max_turns == 30
        
        # Check allowed tools (read-only tools)
        assert "read_file" in verification.allowed_tools
        assert "list_files" in verification.allowed_tools
        assert "navigate" in verification.allowed_tools
        assert "screenshot" in verification.allowed_tools
        assert "extract_text" in verification.allowed_tools
        
        # Check disallowed tools (write/execute tools)
        assert "write_file" in verification.disallowed_tools
        assert "run_python" in verification.disallowed_tools
        assert "click_element" in verification.disallowed_tools
        assert "fill_form" in verification.disallowed_tools
        assert "run_js" in verification.disallowed_tools
        assert "spawn_agent" in verification.disallowed_tools
        
        # Verification agent should have system prompt
        assert "Verification Agent" in verification.system_prompt
    
    def test_all_agents_have_system_prompts(self):
        """Test that all builtin agents have non-empty system prompts"""
        agents = build_builtin_agents()
        
        for agent_type, agent in agents.items():
            assert agent.system_prompt, f"{agent_type} agent has empty system prompt"
            assert len(agent.system_prompt) > 100, f"{agent_type} agent has too short system prompt"
    
    def test_agent_trust_levels_are_appropriate(self):
        """Test that agent trust levels match their capabilities"""
        agents = build_builtin_agents()
        
        # Verification should be READONLY
        assert agents["verification"].trust_level == TrustLevel.READONLY
        assert agents["verification"].is_read_only == True
        
        # Lead and Browser should be WRITE
        assert agents["lead"].trust_level == TrustLevel.WRITE
        assert agents["browser"].trust_level == TrustLevel.WRITE
        
        # Coding should be ADMIN (can execute code)
        assert agents["coding"].trust_level == TrustLevel.ADMIN
    
    def test_only_lead_agent_can_spawn(self):
        """Test that only lead agent can spawn other agents"""
        agents = build_builtin_agents()
        
        assert agents["lead"].can_spawn == True
        assert agents["browser"].can_spawn == False
        assert agents["coding"].can_spawn == False
        assert agents["verification"].can_spawn == False
