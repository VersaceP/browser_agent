# Unit Test Suite for browser_agent_system_v5

This directory contains comprehensive unit tests for the browser_agent_system_v5 project.

## Overview

The test suite covers all major modules:
- **Core modules** (`core/`): Agent definition, execution loop, LLM provider, skill registry, hooks, etc.
- **Toolkit modules** (`toolkits/`): Tool registry, base tool, browser tools, file tools, code tools
- **Permission modules** (`permissions/`): Input sanitizer, denial tracker

## Requirements

Install test dependencies:

```bash
pip install pytest pytest-asyncio pytest-mock pytest-cov
```

Optional dependencies for advanced features:

```bash
pip install pytest-xdist  # For parallel test execution
pip install hypothesis    # For property-based testing
```

## Running Tests

### Run All Tests

```bash
# From project root
pytest unit_test/

# With verbose output
pytest unit_test/ -v

# With very verbose output (show each test)
pytest unit_test/ -vv
```

### Run Specific Test Modules

```bash
# Run only core module tests
pytest unit_test/core/

# Run only toolkit module tests
pytest unit_test/toolkits/

# Run only permission module tests
pytest unit_test/permissions/

# Run a specific test file
pytest unit_test/core/test_agent_definition.py

# Run a specific test function
pytest unit_test/core/test_agent_definition.py::test_agent_definition_creation
```

### Run Tests with Coverage

```bash
# Generate coverage report
pytest unit_test/ --cov=browser_agent_system_v5

# Generate HTML coverage report
pytest unit_test/ --cov=browser_agent_system_v5 --cov-report=html

# Open the HTML report (generated in htmlcov/index.html)
# On Windows:
start htmlcov/index.html
# On macOS:
open htmlcov/index.html
# On Linux:
xdg-open htmlcov/index.html

# Generate coverage report with missing lines
pytest unit_test/ --cov=browser_agent_system_v5 --cov-report=term-missing
```

### Run Tests in Parallel

```bash
# Install pytest-xdist first
pip install pytest-xdist

# Run tests in parallel (auto-detect CPU cores)
pytest unit_test/ -n auto

# Run tests on 4 cores
pytest unit_test/ -n 4
```

### Filter Tests by Markers

```bash
# Run only fast tests (skip slow tests)
pytest unit_test/ -m "not slow"

# Run only integration tests
pytest unit_test/ -m integration
```

### Other Useful Options

```bash
# Stop on first failure
pytest unit_test/ -x

# Show local variables in tracebacks
pytest unit_test/ -l

# Show print statements
pytest unit_test/ -s

# Run last failed tests only
pytest unit_test/ --lf

# Run failed tests first, then others
pytest unit_test/ --ff

# Show test durations (slowest 10 tests)
pytest unit_test/ --durations=10
```

## Test Structure

```
unit_test/
├── __init__.py
├── conftest.py              # Shared fixtures and configuration
├── README.md                # This file
├── core/                    # Core module tests
│   ├── __init__.py
│   ├── test_agent_definition.py
│   ├── test_execution_loop.py
│   ├── test_llm_provider.py
│   ├── test_skill_registry.py
│   ├── test_hook_registry.py
│   ├── test_context_compactor.py
│   ├── test_worktree.py
│   ├── test_session_persistence.py
│   ├── test_teammate_context.py
│   ├── test_prompt_builder.py
│   ├── test_resource_manager.py
│   └── test_agent_spawner.py
├── toolkits/                # Toolkit module tests
│   ├── __init__.py
│   ├── test_tool_registry.py
│   ├── test_base_tool.py
│   ├── test_browser_tools.py
│   ├── test_file_tools.py
│   ├── test_code_tools.py
│   ├── test_lead_tools.py
│   └── test_vision_helper.py
└── permissions/             # Permission module tests
    ├── __init__.py
    ├── test_input_sanitizer.py
    └── test_denial_tracker.py
```

## Shared Fixtures

The `conftest.py` file provides several reusable fixtures:

### `mock_llm_provider`
Mock LLM provider for testing without real API calls.

```python
def test_something(mock_llm_provider):
    mock_llm_provider.generate_response.return_value = (
        "Mock response",
        [],  # tool calls
        "end_turn"  # stop reason
    )
```

### `temp_worktree`
Temporary directory for file operations, automatically cleaned up.

```python
def test_file_ops(temp_worktree):
    file_path = temp_worktree / "test.txt"
    file_path.write_text("content")
```

### `sample_context`
Pre-configured TeammateContext for testing.

```python
def test_context(sample_context):
    sample_context.append_message("user", "Hello")
    assert len(sample_context.session_messages) == 1
```

### `mock_browser`
Mock browser object for testing browser tools.

```python
def test_navigate(mock_browser):
    mock_browser.page.goto.return_value = None
    # Test navigation logic
```

### `sample_agent_definition`
Sample AgentDefinition with WRITE trust level.

```python
def test_agent(sample_agent_definition):
    assert sample_agent_definition.trust_level == TrustLevel.WRITE
```

### `sample_skill_file`
Creates a temporary skill file for testing skill registry.

```python
def test_skill_parsing(sample_skill_file):
    skill = parse_skill_file(str(sample_skill_file))
    assert skill.name == "test_skill"
```

### `mock_hook_handler`
Mock async function for testing hook handlers.

```python
def test_hook(mock_hook_handler):
    mock_hook_handler.return_value = HookResult(action=HookAction.ALLOW)
```

## Helper Functions

### `assert_tool_result()`
Assert tool execution results match expectations.

```python
assert_tool_result(
    result,
    expected_prefix="Success",
    should_contain=["file created"],
    should_not_contain=["error"]
)
```

### `create_mock_tool_calls()`
Create mock tool calls for testing.

```python
tool_calls = create_mock_tool_calls("write_file", {"filename": "test.txt"})
```

## Writing New Tests

### Test File Naming
- Test files must start with `test_` or end with `_test.py`
- Example: `test_my_module.py`

### Test Function Naming
- Test functions must start with `test_`
- Use descriptive names: `test_agent_definition_creation_with_valid_parameters`

### Test Structure (AAA Pattern)
```python
def test_something():
    # Arrange: Set up test data and mocks
    agent = AgentDefinition(...)
    
    # Act: Execute the code being tested
    result = agent.some_method()
    
    # Assert: Verify the results
    assert result == expected_value
```

### Async Tests
Use `@pytest.mark.asyncio` for async tests:

```python
@pytest.mark.asyncio
async def test_async_function():
    result = await some_async_function()
    assert result == expected
```

### Parametrized Tests
Test multiple inputs with one test function:

```python
@pytest.mark.parametrize("input,expected", [
    ("http://example.com", "http://example.com"),
    ("https://test.com", "https://test.com"),
])
def test_url_validation(input, expected):
    result = sanitize_url(input)
    assert result == expected
```

### Exception Testing
Test that exceptions are raised:

```python
def test_invalid_input_raises_error():
    with pytest.raises(ValueError) as exc_info:
        some_function(invalid_input)
    assert "expected error message" in str(exc_info.value)
```

## Coverage Goals

- **Line coverage**: > 80%
- **Branch coverage**: > 70%
- **Function coverage**: > 90%

Check current coverage:

```bash
pytest unit_test/ --cov=browser_agent_system_v5 --cov-report=term-missing
```

## Continuous Integration

For CI/CD pipelines, use:

```bash
# Run tests with JUnit XML output
pytest unit_test/ --junitxml=junit.xml

# Run tests with coverage and XML report
pytest unit_test/ --cov=browser_agent_system_v5 --cov-report=xml --cov-report=term
```

## Troubleshooting

### Import Errors
If you get import errors, ensure the project root is in your Python path:

```bash
export PYTHONPATH="${PYTHONPATH}:$(pwd)"
```

Or run pytest from the project root directory.

### Async Test Warnings
If you see warnings about async tests, ensure `pytest-asyncio` is installed:

```bash
pip install pytest-asyncio
```

### Slow Tests
If tests are too slow, use parallel execution:

```bash
pip install pytest-xdist
pytest unit_test/ -n auto
```

Or skip slow tests:

```bash
pytest unit_test/ -m "not slow"
```

## Contributing

When adding new tests:
1. Follow the existing test structure
2. Use descriptive test names
3. Add docstrings to test functions
4. Use shared fixtures from `conftest.py`
5. Ensure tests are isolated (no dependencies between tests)
6. Run the full test suite before committing

## License

Same as the main project.
