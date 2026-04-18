# Requirements Document: Comprehensive Unit Testing Suite

## 1. Functional Requirements

### 1.1 Test Infrastructure

**FR-1.1.1**: 系统应提供统一的测试配置文件 (conftest.py)，包含所有共享的 fixtures
- **Priority**: High
- **Acceptance Criteria**: 
  - conftest.py 文件存在于 unit_test/ 目录
  - 包含至少 5 个可复用的 fixtures
  - 所有测试文件可以访问这些 fixtures

**FR-1.1.2**: 系统应支持临时工作区创建和清理
- **Priority**: High
- **Acceptance Criteria**:
  - 提供 temp_worktree fixture
  - 每个测试获得独立的临时目录
  - 测试结束后自动清理临时文件

**FR-1.1.3**: 系统应提供 Mock LLM Provider
- **Priority**: High
- **Acceptance Criteria**:
  - 提供 mock_llm_provider fixture
  - 支持配置返回值和工具调用
  - 不依赖真实 API 调用

### 1.2 Core Module Tests

**FR-1.2.1**: 应测试 AgentDefinition 的创建和验证
- **Priority**: High
- **Acceptance Criteria**:
  - 测试所有 AgentDefinition 参数
  - 测试 TrustLevel 枚举
  - 测试 build_builtin_agents() 函数
  - 验证 4 个内建 Agent 的配置

**FR-1.2.2**: 应测试 LLM Provider 的初始化和配置
- **Priority**: High
- **Acceptance Criteria**:
  - 测试 ModelConfig 加载
  - 测试 Anthropic Provider 初始化
  - 测试 OpenAI Provider 初始化
  - 测试 LLMFactory.create_provider()

**FR-1.2.3**: 应测试 Skill Registry 的技能管理
- **Priority**: Medium
- **Acceptance Criteria**:
  - 测试技能文件解析
  - 测试技能加载和注册
  - 测试技能选择（URL 匹配）
  - 测试技能选择（关键词匹配）
  - 测试错误处理

**FR-1.2.4**: 应测试 Hook Registry 的事件管理
- **Priority**: High
- **Acceptance Criteria**:
  - 测试 Hook 注册
  - 测试 Hook 触发
  - 测试 HookAction (ALLOW/BLOCK/MODIFY)
  - 测试多个 Handler 的执行顺序

**FR-1.2.5**: 应测试 Context Compactor 的压缩逻辑
- **Priority**: Medium
- **Acceptance Criteria**:
  - 测试 Token 水位计算
  - 测试压缩触发条件
  - 测试消息保留策略
  - 测试规则压缩和 LLM 压缩

**FR-1.2.6**: 应测试 WorkTree Manager 的文件隔离
- **Priority**: High
- **Acceptance Criteria**:
  - 测试 worktree 创建
  - 测试路径解析
  - 测试路径穿越防御
  - 测试 worktree 清理

**FR-1.2.7**: 应测试 Session Persistence 的序列化
- **Priority**: Medium
- **Acceptance Criteria**:
  - 测试 session 保存
  - 测试 session 加载
  - 测试 session 列表
  - 测试版本兼容性
  - 测试错误处理

**FR-1.2.8**: 应测试 Execution Loop 的核心逻辑
- **Priority**: High
- **Acceptance Criteria**:
  - 测试 Turn 循环
  - 测试工具调用流程
  - 测试 Hook 触发时机
  - 测试压缩触发
  - 测试早停机制

### 1.3 Toolkit Module Tests

**FR-1.3.1**: 应测试 Tool Registry 的工具管理
- **Priority**: High
- **Acceptance Criteria**:
  - 测试工具注册
  - 测试工具过滤（trust level）
  - 测试工具过滤（白名单/黑名单）
  - 测试 schema 生成
  - 测试工具调度

**FR-1.3.2**: 应测试 BaseTool 的基础功能
- **Priority**: High
- **Acceptance Criteria**:
  - 测试 safe_execute() 方法
  - 测试输出截断
  - 测试溢出落盘
  - 测试 to_schema() 方法

**FR-1.3.3**: 应测试 Browser Tools 的浏览器操作
- **Priority**: Medium
- **Acceptance Criteria**:
  - 测试 navigate 工具
  - 测试 extract_text 工具
  - 测试 screenshot 工具
  - 测试 click_element 工具
  - 测试 fill_form 工具
  - 使用 Mock 浏览器对象

**FR-1.3.4**: 应测试 File Tools 的文件操作
- **Priority**: High
- **Acceptance Criteria**:
  - 测试 write_file 工具
  - 测试 read_file 工具
  - 测试 list_files 工具
  - 测试路径安全检查

**FR-1.3.5**: 应测试 Code Tools 的代码执行
- **Priority**: Medium
- **Acceptance Criteria**:
  - 测试 run_python 工具
  - 测试代码执行隔离
  - 测试超时处理
  - 测试错误捕获

### 1.4 Permission Module Tests

**FR-1.4.1**: 应测试 Input Sanitizer 的安全校验
- **Priority**: High
- **Acceptance Criteria**:
  - 测试路径穿越防御
  - 测试 URL 协议白名单
  - 测试 Shell 命令过滤
  - 测试支付行为拦截
  - 测试所有 SanitizationError 场景

**FR-1.4.2**: 应测试 Denial Tracker 的熔断机制
- **Priority**: High
- **Acceptance Criteria**:
  - 测试拒绝记录
  - 测试熔断触发
  - 测试批准重置
  - 测试冷却期
  - 测试会话隔离

## 2. Non-Functional Requirements

### 2.1 Performance

**NFR-2.1.1**: 测试执行速度
- **Requirement**: 所有单元测试应在 30 秒内完成
- **Measurement**: 使用 pytest --durations=10 测量
- **Priority**: Medium

**NFR-2.1.2**: 并行执行支持
- **Requirement**: 测试应支持并行执行 (pytest-xdist)
- **Measurement**: 使用 pytest -n auto 验证
- **Priority**: Low

### 2.2 Coverage

**NFR-2.2.1**: 代码覆盖率
- **Requirement**: 
  - 行覆盖率 > 80%
  - 分支覆盖率 > 70%
  - 函数覆盖率 > 90%
- **Measurement**: 使用 pytest-cov 生成报告
- **Priority**: High

**NFR-2.2.2**: 模块覆盖完整性
- **Requirement**: 每个源代码模块都有对应的测试文件
- **Measurement**: 手动检查 unit_test/ 目录结构
- **Priority**: High

### 2.3 Maintainability

**NFR-2.3.1**: 测试代码质量
- **Requirement**: 
  - 每个测试函数有清晰的 docstring
  - 测试名称描述测试场景
  - 使用 AAA 模式 (Arrange-Act-Assert)
- **Priority**: Medium

**NFR-2.3.2**: 测试隔离性
- **Requirement**: 
  - 测试之间无依赖关系
  - 测试顺序不影响结果
  - 每个测试使用独立的 fixtures
- **Priority**: High

### 2.4 Documentation

**NFR-2.4.1**: 测试文档
- **Requirement**: 
  - README.md 说明如何运行测试
  - 每个测试模块有模块级 docstring
  - 复杂测试有详细注释
- **Priority**: Medium

**NFR-2.4.2**: 测试报告
- **Requirement**: 
  - 生成 HTML 覆盖率报告
  - 生成 JUnit XML 报告（CI/CD 集成）
- **Priority**: Low

## 3. Constraints

### 3.1 Technical Constraints

**C-3.1.1**: 测试框架
- **Constraint**: 必须使用 pytest 作为测试框架
- **Rationale**: 项目已使用 pytest，保持一致性

**C-3.1.2**: Python 版本
- **Constraint**: 测试应兼容 Python 3.8+
- **Rationale**: 与项目主代码保持一致

**C-3.1.3**: 异步测试
- **Constraint**: 必须使用 pytest-asyncio 处理异步测试
- **Rationale**: 项目大量使用 async/await

### 3.2 Operational Constraints

**C-3.2.1**: 无外部依赖
- **Constraint**: 测试不应依赖外部服务（API、数据库、浏览器）
- **Rationale**: 确保测试可在任何环境运行

**C-3.2.2**: 只读原则
- **Constraint**: 测试不应修改源代码
- **Rationale**: 用户要求"只做测试，不修改代码"

**C-3.2.3**: 目录结构
- **Constraint**: 所有测试文件必须放在 unit_test/ 文件夹
- **Rationale**: 用户明确要求

## 4. Acceptance Criteria

### 4.1 Test Suite Completeness

**AC-4.1.1**: 核心模块测试
- [ ] test_agent_definition.py 存在且包含至少 5 个测试
- [ ] test_execution_loop.py 存在且包含至少 8 个测试
- [ ] test_llm_provider.py 存在且包含至少 6 个测试
- [ ] test_skill_registry.py 存在且包含至少 8 个测试
- [ ] test_hook_registry.py 存在且包含至少 6 个测试
- [ ] test_context_compactor.py 存在且包含至少 5 个测试
- [ ] test_worktree.py 存在且包含至少 6 个测试
- [ ] test_session_persistence.py 存在且包含至少 6 个测试

**AC-4.1.2**: 工具包模块测试
- [ ] test_tool_registry.py 存在且包含至少 6 个测试
- [ ] test_base_tool.py 存在且包含至少 5 个测试
- [ ] test_browser_tools.py 存在且包含至少 8 个测试
- [ ] test_file_tools.py 存在且包含至少 5 个测试
- [ ] test_code_tools.py 存在且包含至少 4 个测试

**AC-4.1.3**: 权限模块测试
- [ ] test_input_sanitizer.py 存在且包含至少 10 个测试
- [ ] test_denial_tracker.py 存在且包含至少 6 个测试

### 4.2 Test Execution

**AC-4.2.1**: 所有测试通过
- [ ] pytest 执行无错误
- [ ] 无跳过的测试（除非有明确原因）
- [ ] 无警告信息

**AC-4.2.2**: 覆盖率达标
- [ ] 行覆盖率 ≥ 80%
- [ ] 分支覆盖率 ≥ 70%
- [ ] 生成 HTML 覆盖率报告

### 4.3 Documentation

**AC-4.3.1**: 测试文档完整
- [ ] unit_test/README.md 存在
- [ ] README.md 包含运行说明
- [ ] README.md 包含覆盖率报告说明

**AC-4.3.2**: 测试报告生成
- [ ] 生成 coverage.html 报告
- [ ] 生成 junit.xml 报告（可选）
- [ ] 报告可在浏览器中查看

## 5. Test Scenarios

### 5.1 Core Module Scenarios

**Scenario 5.1.1**: Agent Definition Creation
- **Given**: Valid agent parameters
- **When**: Creating AgentDefinition
- **Then**: Agent is created with correct properties

**Scenario 5.1.2**: Tool Filtering by Trust Level
- **Given**: Tool with ADMIN trust level
- **When**: Filtering for WRITE agent
- **Then**: Tool is excluded from filtered list

**Scenario 5.1.3**: Path Traversal Attack
- **Given**: Malicious path "../../../etc/passwd"
- **When**: Sanitizing path
- **Then**: SanitizationError is raised

**Scenario 5.1.4**: Session Save and Load
- **Given**: Active TeammateContext
- **When**: Saving and loading session
- **Then**: Loaded context matches original

**Scenario 5.1.5**: Hook Execution Order
- **Given**: Multiple hooks registered for same event
- **When**: Emitting event
- **Then**: Hooks execute in registration order

### 5.2 Toolkit Module Scenarios

**Scenario 5.2.1**: Tool Output Truncation
- **Given**: Tool returns > max_result_chars
- **When**: Executing tool via safe_execute()
- **Then**: Output is truncated and spilled to file

**Scenario 5.2.2**: File Write in Worktree
- **Given**: Valid filename and content
- **When**: Executing write_file tool
- **Then**: File is created in worktree

**Scenario 5.2.3**: Browser Navigation (Mocked)
- **Given**: Valid URL
- **When**: Executing navigate tool
- **Then**: Mock browser navigates to URL

### 5.3 Permission Module Scenarios

**Scenario 5.3.1**: URL Protocol Validation
- **Given**: URL with file:// protocol
- **When**: Sanitizing URL
- **Then**: SanitizationError is raised

**Scenario 5.3.2**: Circuit Breaker Trigger
- **Given**: 5 consecutive denials
- **When**: Recording 6th denial
- **Then**: Circuit breaker is triggered

**Scenario 5.3.3**: Payment Action Block
- **Given**: Click on element with "pay" selector
- **When**: Sanitizing payment action
- **Then**: SanitizationError is raised

## 6. Dependencies

### 6.1 Testing Dependencies

- pytest >= 7.0.0
- pytest-asyncio >= 0.21.0
- pytest-mock >= 3.10.0
- pytest-cov >= 4.0.0

### 6.2 Optional Dependencies

- pytest-xdist (parallel execution)
- hypothesis (property-based testing)

### 6.3 Project Dependencies

- All dependencies from browser_agent_system_v5/requirements.txt

## 7. Risks and Mitigations

### 7.1 Risk: Async Test Complexity

**Description**: Async tests may be difficult to write and debug
**Impact**: Medium
**Mitigation**: 
- Use pytest-asyncio fixtures
- Provide clear examples in conftest.py
- Document async testing patterns

### 7.2 Risk: Mock Object Maintenance

**Description**: Mocks may become outdated as code evolves
**Impact**: Medium
**Mitigation**:
- Keep mocks simple and focused
- Review mocks when source code changes
- Use spec parameter to enforce interface

### 7.3 Risk: Test Execution Time

**Description**: Tests may take too long to run
**Impact**: Low
**Mitigation**:
- Use mocks to avoid slow operations
- Consider pytest-xdist for parallel execution
- Profile slow tests and optimize

### 7.4 Risk: Coverage Gaps

**Description**: Some code paths may be difficult to test
**Impact**: Medium
**Mitigation**:
- Identify untestable code early
- Refactor for testability if needed
- Document coverage gaps with reasons

## 8. Success Metrics

### 8.1 Quantitative Metrics

- **Test Count**: ≥ 100 unit tests
- **Coverage**: ≥ 80% line coverage
- **Execution Time**: ≤ 30 seconds
- **Pass Rate**: 100% (all tests pass)

### 8.2 Qualitative Metrics

- **Code Quality**: Tests follow best practices
- **Maintainability**: Tests are easy to understand and modify
- **Documentation**: Clear instructions for running tests
- **Reliability**: Tests produce consistent results
