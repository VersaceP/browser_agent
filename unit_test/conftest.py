"""
Pytest configuration and shared fixtures for unit tests.

This module provides reusable fixtures for all test modules.
"""

import sys
import os
from pathlib import Path
from typing import Dict, Any, List
from unittest.mock import Mock, AsyncMock, MagicMock
import pytest

# Add project root and browser_agent_system_v5 to Python path
project_root = Path(__file__).parent.parent
browser_agent_root = project_root / "browser_agent_system_v5"
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(browser_agent_root))

from browser_agent_system_v5.core.agent_definition import AgentDefinition, TrustLevel
from browser_agent_system_v5.core.teammate_context import TeammateContext
from browser_agent_system_v5.core.llm_provider import BaseLLMProvider, ModelConfig


@pytest.fixture
def mock_llm_provider():
    """
    Create a mock LLM provider for testing.
    
    Returns a mock that simulates LLM responses without making real API calls.
    The mock can be configured to return specific responses and tool calls.
    
    Usage:
        def test_something(mock_llm_provider):
            mock_llm_provider.generate_response.return_value = (
                "Mock response",
                [],
                "end_turn"
            )
    """
    mock = AsyncMock(spec=BaseLLMProvider)
    mock.config = ModelConfig(provider="mock", model_id="mock-model")
    
    # Default response
    mock.generate_response.return_value = (
        "Mock LLM response",
        [],  # No tool calls
        "end_turn"
    )
    
    return mock


@pytest.fixture
def temp_worktree(tmp_path):
    """
    Create a temporary worktree directory for file operations.
    
    Each test gets an isolated temporary directory that is automatically
    cleaned up after the test completes.
    
    Args:
        tmp_path: pytest's built-in temporary directory fixture
    
    Returns:
        Path: Path to the temporary worktree directory
    
    Usage:
        def test_file_operations(temp_worktree):
            file_path = temp_worktree / "test.txt"
            file_path.write_text("test content")
    """
    worktree = tmp_path / "test_worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    
    # Create standard subdirectories
    (worktree / "data").mkdir(exist_ok=True)
    (worktree / "scripts").mkdir(exist_ok=True)
    
    return worktree


@pytest.fixture
def sample_context(temp_worktree):
    """
    Create a sample TeammateContext for testing.
    
    Provides a pre-configured context object with reasonable defaults.
    
    Args:
        temp_worktree: Temporary worktree fixture
    
    Returns:
        TeammateContext: A sample context object
    
    Usage:
        def test_context_operations(sample_context):
            sample_context.append_message("user", "Hello")
            assert len(sample_context.session_messages) == 1
    """
    context = TeammateContext(
        agent_type="test_agent",
        session_id="test_session_123",
        task="Test task description",
        worktree_path=str(temp_worktree),
        max_tokens=100000,
    )
    return context


@pytest.fixture
def mock_browser():
    """
    Create a mock browser object for testing browser tools.
    
    Returns a mock Playwright browser/page object with common methods.
    
    Usage:
        def test_navigate(mock_browser):
            mock_browser.goto.return_value = None
            # Test navigation logic
    """
    mock = MagicMock()
    
    # Mock page object
    mock_page = MagicMock()
    mock_page.goto = AsyncMock()
    mock_page.content = AsyncMock(return_value="<html><body>Test</body></html>")
    mock_page.screenshot = AsyncMock(return_value=b"fake_screenshot_data")
    mock_page.evaluate = AsyncMock(return_value="eval result")
    mock_page.query_selector = AsyncMock()
    mock_page.fill = AsyncMock()
    mock_page.click = AsyncMock()
    
    # Mock element
    mock_element = MagicMock()
    mock_element.click = AsyncMock()
    mock_element.fill = AsyncMock()
    mock_element.text_content = AsyncMock(return_value="Element text")
    mock_page.query_selector.return_value = mock_element
    
    mock.page = mock_page
    mock.new_page = AsyncMock(return_value=mock_page)
    mock.close = AsyncMock()
    
    return mock


@pytest.fixture
def sample_agent_definition():
    """
    Create a sample AgentDefinition for testing.
    
    Returns:
        AgentDefinition: A sample agent definition with WRITE trust level
    
    Usage:
        def test_agent_behavior(sample_agent_definition):
            assert sample_agent_definition.trust_level == TrustLevel.WRITE
    """
    return AgentDefinition(
        agent_type="test_agent",
        system_prompt="Test system prompt",
        allowed_tools=["tool1", "tool2"],
        trust_level=TrustLevel.WRITE,
        max_turns=10,
    )


@pytest.fixture
def mock_tool_result():
    """
    Create a mock tool execution result.
    
    Returns:
        str: A sample tool execution result string
    """
    return "Tool executed successfully. Result: test_output"


@pytest.fixture
def sample_skill_file(tmp_path):
    """
    Create a sample skill file for testing skill registry.
    
    Args:
        tmp_path: pytest's built-in temporary directory fixture
    
    Returns:
        Path: Path to the created skill file
    
    Usage:
        def test_skill_parsing(sample_skill_file):
            skill = parse_skill_file(str(sample_skill_file))
            assert skill.name == "test_skill"
    """
    skill_content = """---
name: test_skill
version: 1.0.0
target_websites:
  - example.com
  - test.com
keywords:
  - test
  - example
description: A test skill for unit testing
author: Test Author
---

# Test Skill

This is a test skill for unit testing purposes.

## Usage

Use this skill when testing the skill registry.
"""
    
    skill_file = tmp_path / "test_skill.md"
    skill_file.write_text(skill_content, encoding="utf-8")
    return skill_file


@pytest.fixture
def mock_hook_handler():
    """
    Create a mock hook handler function.
    
    Returns:
        AsyncMock: A mock async function that can be used as a hook handler
    
    Usage:
        def test_hook_execution(mock_hook_handler):
            mock_hook_handler.return_value = HookResult(action=HookAction.ALLOW)
            # Test hook logic
    """
    from browser_agent_system_v5.core.hook_registry import HookResult, HookAction
    
    handler = AsyncMock()
    handler.return_value = HookResult(action=HookAction.ALLOW)
    return handler


# Configure pytest
def pytest_configure(config):
    """Configure pytest with custom markers."""
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')"
    )
    config.addinivalue_line(
        "markers", "integration: marks tests as integration tests"
    )


# Helper functions for assertions

def assert_tool_result(
    result: str,
    expected_prefix: str = None,
    should_contain: List[str] = None,
    should_not_contain: List[str] = None
) -> None:
    """
    Assert tool execution result matches expectations.
    
    Args:
        result: Tool execution result string
        expected_prefix: Expected prefix of the result (optional)
        should_contain: List of strings that should be in result (optional)
        should_not_contain: List of strings that should not be in result (optional)
    
    Raises:
        AssertionError: If any expectation fails
    
    Usage:
        assert_tool_result(
            result,
            expected_prefix="Success",
            should_contain=["file created", "path/to/file"],
            should_not_contain=["error", "failed"]
        )
    """
    if expected_prefix:
        assert result.startswith(expected_prefix), \
            f"Expected result to start with '{expected_prefix}', got: {result[:100]}"
    
    if should_contain:
        for text in should_contain:
            assert text in result, \
                f"Expected '{text}' in result, got: {result[:200]}"
    
    if should_not_contain:
        for text in should_not_contain:
            assert text not in result, \
                f"Did not expect '{text}' in result, got: {result[:200]}"


def create_mock_tool_calls(tool_name: str, tool_input: Dict[str, Any]) -> List[Dict]:
    """
    Create a list of mock tool calls for testing.
    
    Args:
        tool_name: Name of the tool
        tool_input: Input parameters for the tool
    
    Returns:
        List of tool call dictionaries
    
    Usage:
        tool_calls = create_mock_tool_calls("write_file", {"filename": "test.txt"})
    """
    return [{
        "id": "call_123",
        "name": tool_name,
        "input": tool_input
    }]
