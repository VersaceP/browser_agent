# Tasks: Comprehensive Unit Testing Suite

## 1. Setup Test Infrastructure

### 1.1 Create Test Directory Structure
- [x] 1.1.1 Create unit_test/ directory in project root
- [x] 1.1.2 Create __init__.py in unit_test/
- [x] 1.1.3 Create subdirectories: core/, toolkits/, permissions/

### 1.2 Setup Test Configuration
- [x] 1.2.1 Create conftest.py with shared fixtures
- [x] 1.2.2 Add mock_llm_provider fixture
- [x] 1.2.3 Add temp_worktree fixture
- [x] 1.2.4 Add sample_context fixture
- [x] 1.2.5 Add mock_browser fixture

### 1.3 Create Test Documentation
- [x] 1.3.1 Create unit_test/README.md with running instructions
- [x] 1.3.2 Document fixture usage
- [x] 1.3.3 Document coverage report generation

## 2. Core Module Tests

### 2.1 Test Agent Definition (test_agent_definition.py)
- [ ] 2.1.1 Test AgentDefinition creation with valid parameters
- [ ] 2.1.2 Test TrustLevel enum values
- [ ] 2.1.3 Test build_builtin_agents() returns 4 agents
- [ ] 2.1.4 Test lead agent configuration
- [ ] 2.1.5 Test browser agent configuration
- [ ] 2.1.6 Test coding agent configuration
- [ ] 2.1.7 Test verification agent configuration

### 2.2 Test LLM Provider (test_llm_provider.py)
- [ ] 2.2.1 Test ModelConfig creation
- [ ] 2.2.2 Test ModelConfig.load_from_file()
- [ ] 2.2.3 Test AnthropicProvider initialization
- [ ] 2.2.4 Test OpenAIProvider initialization
- [ ] 2.2.5 Test LLMFactory.create_provider() for Anthropic
- [ ] 2.2.6 Test LLMFactory.create_provider() for OpenAI
- [ ] 2.2.7 Test generate_response() with mocked API

### 2.3 Test Skill Registry (test_skill_registry.py)
- [ ] 2.3.1 Test SkillRegistry initialization
- [ ] 2.3.2 Test parse_skill_file() with valid file
- [ ] 2.3.3 Test parse_skill_file() with invalid YAML
- [ ] 2.3.4 Test parse_skill_file() with missing fields
- [ ] 2.3.5 Test load_all() with valid skills
- [ ] 2.3.6 Test select_skills() by URL matching
- [ ] 2.3.7 Test select_skills() by keyword matching
- [ ] 2.3.8 Test select_skills() with explicit skills

### 2.4 Test Hook Registry (test_hook_registry.py)
- [ ] 2.4.1 Test HookRegistry initialization
- [ ] 2.4.2 Test register() adds handler
- [ ] 2.4.3 Test emit() calls handlers in order
- [ ] 2.4.4 Test emit() with ALLOW action
- [ ] 2.4.5 Test emit() with BLOCK action
- [ ] 2.4.6 Test emit() with MODIFY action
- [ ] 2.4.7 Test multiple handlers for same event

### 2.5 Test Context Compactor (test_context_compactor.py)
- [ ] 2.5.1 Test ContextCompactor initialization
- [ ] 2.5.2 Test should_compact() returns True when threshold exceeded
- [ ] 2.5.3 Test should_compact() returns False when below threshold
- [ ] 2.5.4 Test compact_if_needed() with rule-based compression
- [ ] 2.5.5 Test compact_if_needed() preserves recent messages
- [ ] 2.5.6 Test _rule_based_summary() generates summary

### 2.6 Test WorkTree Manager (test_worktree.py)
- [ ] 2.6.1 Test WorkTreeManager initialization
- [ ] 2.6.2 Test get_or_create_worktree() creates directory
- [ ] 2.6.3 Test resolve_path() with valid relative path
- [ ] 2.6.4 Test resolve_path() blocks path traversal
- [ ] 2.6.5 Test save_spilled_data() writes file
- [ ] 2.6.6 Test cleanup_worktree() removes directory
- [ ] 2.6.7 Test list_worktrees() returns all worktrees

### 2.7 Test Session Persistence (test_session_persistence.py)
- [ ] 2.7.1 Test save_session() creates JSON file
- [ ] 2.7.2 Test save_session() with directory path
- [ ] 2.7.3 Test load_session() restores context
- [ ] 2.7.4 Test load_session() with directory path
- [ ] 2.7.5 Test load_session() validates schema version
- [ ] 2.7.6 Test list_sessions() finds all sessions
- [ ] 2.7.7 Test SessionPersistenceError handling

### 2.8 Test Execution Loop (test_execution_loop.py)
- [ ] 2.8.1 Test execute_turn() initialization
- [ ] 2.8.2 Test execute_turn() yields turn_started event
- [ ] 2.8.3 Test execute_turn() calls LLM provider
- [ ] 2.8.4 Test execute_turn() dispatches tool calls
- [ ] 2.8.5 Test execute_turn() triggers hooks
- [ ] 2.8.6 Test execute_turn() handles compression
- [ ] 2.8.7 Test execute_turn() handles early stop
- [ ] 2.8.8 Test execute_turn() handles LLM errors

### 2.9 Test Teammate Context (test_teammate_context.py)
- [ ] 2.9.1 Test TeammateContext initialization
- [ ] 2.9.2 Test append_message() adds message
- [ ] 2.9.3 Test get_messages() returns messages
- [ ] 2.9.4 Test estimate_tokens() calculates tokens
- [ ] 2.9.5 Test get_token_ratio() returns ratio

### 2.10 Test Prompt Builder (test_prompt_builder.py)
- [ ] 2.10.1 Test build_system_prompt() generates prompt
- [ ] 2.10.2 Test build_dynamic_context() includes task
- [ ] 2.10.3 Test format_skills() formats skill content

### 2.11 Test Resource Manager (test_resource_manager.py)
- [ ] 2.11.1 Test ResourceManager initialization
- [ ] 2.11.2 Test acquire_browser() with mocked browser
- [ ] 2.11.3 Test release_browser() closes browser
- [ ] 2.11.4 Test release_all() closes all browsers

### 2.12 Test Agent Spawner (test_agent_spawner.py)
- [ ] 2.12.1 Test AgentSpawner initialization
- [ ] 2.12.2 Test register_builtin_agents() registers 4 agents
- [ ] 2.12.3 Test spawn() creates context
- [ ] 2.12.4 Test spawn() executes agent
- [ ] 2.12.5 Test chat() continues conversation

## 3. Toolkit Module Tests

### 3.1 Test Tool Registry (test_tool_registry.py)
- [ ] 3.1.1 Test ToolRegistry initialization
- [ ] 3.1.2 Test register() adds tool
- [ ] 3.1.3 Test register_many() adds multiple tools
- [ ] 3.1.4 Test get_tool() retrieves tool
- [ ] 3.1.5 Test filter_tools() by trust level
- [ ] 3.1.6 Test filter_tools() by whitelist
- [ ] 3.1.7 Test filter_tools() by blacklist
- [ ] 3.1.8 Test get_schemas() generates schemas
- [ ] 3.1.9 Test dispatch() executes tool

### 3.2 Test Base Tool (test_base_tool.py)
- [ ] 3.2.1 Create MockTool class for testing
- [ ] 3.2.2 Test BaseTool.to_schema() generates schema
- [ ] 3.2.3 Test BaseTool.safe_execute() calls execute()
- [ ] 3.2.4 Test BaseTool.safe_execute() truncates long output
- [ ] 3.2.5 Test BaseTool.safe_execute() spills to file
- [ ] 3.2.6 Test BaseTool.safe_execute() handles errors

### 3.3 Test Browser Tools (test_browser_tools.py)
- [ ] 3.3.1 Test NavigateTool with mocked browser
- [ ] 3.3.2 Test ExtractTextTool with mocked page
- [ ] 3.3.3 Test ScreenshotTool with mocked page
- [ ] 3.3.4 Test ClickElementTool with mocked element
- [ ] 3.3.5 Test FillFormTool with mocked input
- [ ] 3.3.6 Test ScrollPageTool with mocked page
- [ ] 3.3.7 Test RunJSTool with mocked page
- [ ] 3.3.8 Test WaitUserTool

### 3.4 Test File Tools (test_file_tools.py)
- [ ] 3.4.1 Test WriteFileTool creates file
- [ ] 3.4.2 Test WriteFileTool validates path
- [ ] 3.4.3 Test ReadFileTool reads file
- [ ] 3.4.4 Test ReadFileTool handles missing file
- [ ] 3.4.5 Test ListFilesTool lists directory

### 3.5 Test Code Tools (test_code_tools.py)
- [ ] 3.5.1 Test RunPythonTool executes code
- [ ] 3.5.2 Test RunPythonTool captures output
- [ ] 3.5.3 Test RunPythonTool handles errors
- [ ] 3.5.4 Test RunPythonTool enforces timeout

### 3.6 Test Lead Tools (test_lead_tools.py)
- [ ] 3.6.1 Test SubmitPlanTool validates plan
- [ ] 3.6.2 Test SpawnAgentToolImpl spawns agent
- [ ] 3.6.3 Test SpawnAgentsParallelTool spawns multiple agents
- [ ] 3.6.4 Test InitProgressTool initializes progress
- [ ] 3.6.5 Test UpdateProgressTool updates progress

### 3.7 Test Vision Helper (test_vision_helper.py)
- [ ] 3.7.1 Test analyze_screenshot() with mocked LLM
- [ ] 3.7.2 Test find_element_in_screenshot() with mocked LLM
- [ ] 3.7.3 Test image encoding

## 4. Permission Module Tests

### 4.1 Test Input Sanitizer (test_input_sanitizer.py)
- [x] 4.1.1 Test sanitize_path() with valid path
- [ ] 4.1.2 Test sanitize_path() blocks traversal attack
- [ ] 4.1.3 Test sanitize_path() with missing worktree
- [ ] 4.1.4 Test sanitize_url() with valid HTTPS URL
- [ ] 4.1.5 Test sanitize_url() with valid HTTP URL
- [ ] 4.1.6 Test sanitize_url() blocks file:// protocol
- [ ] 4.1.7 Test sanitize_url() blocks javascript: protocol
- [ ] 4.1.8 Test sanitize_shell_input() with safe input
- [ ] 4.1.9 Test sanitize_shell_input() blocks pipe character
- [ ] 4.1.10 Test sanitize_shell_input() blocks redirect
- [ ] 4.1.11 Test sanitize_payment_action() blocks payment click
- [ ] 4.1.12 Test sanitize_payment_action() blocks card input

### 4.2 Test Denial Tracker (test_denial_tracker.py)
- [ ] 4.2.1 Test DenialTracker initialization
- [ ] 4.2.2 Test record_denial() increments count
- [ ] 4.2.3 Test record_denial() triggers circuit breaker
- [ ] 4.2.4 Test record_approval() resets count
- [ ] 4.2.5 Test is_circuit_broken() returns status
- [ ] 4.2.6 Test clear_session() clears agent state

## 5. Test Execution and Reporting

### 5.1 Run Tests
- [ ] 5.1.1 Run all tests with pytest
- [ ] 5.1.2 Verify all tests pass
- [ ] 5.1.3 Fix any failing tests

### 5.2 Generate Coverage Report
- [ ] 5.2.1 Run pytest with coverage
- [ ] 5.2.2 Generate HTML coverage report
- [ ] 5.2.3 Verify coverage meets requirements (>80%)
- [ ] 5.2.4 Identify uncovered code paths

### 5.3 Create Test Report
- [ ] 5.3.1 Document test execution results
- [ ] 5.3.2 Document coverage statistics
- [ ] 5.3.3 Document any known issues or limitations
- [ ] 5.3.4 Create summary report for user

## 6. Documentation and Cleanup

### 6.1 Update Documentation
- [ ] 6.1.1 Ensure README.md is complete
- [ ] 6.1.2 Add examples of running specific tests
- [ ] 6.1.3 Document any test dependencies

### 6.2 Code Quality
- [ ] 6.2.1 Ensure all tests have docstrings
- [ ] 6.2.2 Ensure test names are descriptive
- [ ] 6.2.3 Remove any debug code or comments

### 6.3 Final Verification
- [ ] 6.3.1 Run full test suite one final time
- [ ] 6.3.2 Verify no source code was modified
- [ ] 6.3.3 Verify all test files are in unit_test/ folder
- [ ] 6.3.4 Generate final coverage report
