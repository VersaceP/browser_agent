"""
Unit Test Suite for browser_agent_system_v5

This package contains comprehensive unit tests for all modules in the
browser_agent_system_v5 project.

Test Structure:
- core/: Tests for core modules (agent_definition, execution_loop, etc.)
- toolkits/: Tests for toolkit modules (tool_registry, base_tool, etc.)
- permissions/: Tests for permission modules (input_sanitizer, denial_tracker)

Running Tests:
    pytest unit_test/                    # Run all tests
    pytest unit_test/core/               # Run core module tests only
    pytest unit_test/ -v                 # Verbose output
    pytest unit_test/ --cov=browser_agent_system_v5  # With coverage

Coverage Report:
    pytest unit_test/ --cov=browser_agent_system_v5 --cov-report=html
    # Open htmlcov/index.html in browser
"""

__version__ = "1.0.0"
