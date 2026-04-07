"""
agent_definition.py — Agent 人格数据定义体

V4 核心设计：Agent 不是类，而是数据。所有 Agent（Lead, Browser, Coding, Verification）
的差异化完全由 AgentDefinition 配置驱动（工具白名单、Trust Level、System Prompt）。
执行引擎（execution_loop.py）是通用的纯函数生成器，读取此处的定义来差异化行为。
"""

from dataclasses import dataclass, field
from enum import IntEnum
from typing import List, Optional


class TrustLevel(IntEnum):
    """
    三级信任等级，决定 Agent 可以访问的工具层级。
    数值越高，权限越大。ToolRegistry 在分发工具时会根据此等级进行过滤。
    """
    READONLY = 1   # 只读审查，禁止任何写入/执行操作（Verification Agent 专用）
    WRITE = 2      # 允许文件读写和浏览器操作（Lead, Browser Agent）
    ADMIN = 3      # 允许执行任意代码（Coding Agent，需显式授权）


@dataclass
class AgentDefinition:
    """
    Agent 人格档案 — 纯数据容器，零行为。
    
    通过以下维度完整定义一个 Agent 的"人格"：
    - agent_type: 唯一标识符（lead / browser / coding / verification）
    - system_prompt: 注入到 LLM 的系统提示词，决定 Agent 的行为模式
    - allowed_tools: 白名单工具列表（为空表示允许所有符合 trust_level 的工具）
    - disallowed_tools: 黑名单工具列表（优先级高于白名单）
    - trust_level: 信任等级，与 BaseTool.required_trust_level 进行权限匹配
    - max_turns: 最大思考-行动循环轮次，防止无限循环
    - is_read_only: 硬性只读标记，系统级强制拦截所有写入操作
    - can_spawn: 是否允许派生子 Agent（防递归炸弹）
    """
    agent_type: str
    system_prompt: str
    allowed_tools: List[str] = field(default_factory=list)
    disallowed_tools: List[str] = field(default_factory=list)
    trust_level: TrustLevel = TrustLevel.WRITE
    max_turns: int = 15
    is_read_only: bool = False
    can_spawn: bool = False


def build_builtin_agents() -> dict[str, AgentDefinition]:
    """
    注册 4 个内建 Agent 定义，返回 {agent_type: AgentDefinition} 字典。
    
    四大 Agent 拓扑：
    ┌──────────────────┐
    │   Lead Agent     │ → 拆解任务、调度子 Agent、汇总结果
    │   Browser Agent  │ → 网页导航、DOM 提取、截图
    │   Coding Agent   │ → 数据清洗、Python 代码执行
    │   Verification   │ → 只读对抗性审查（找出最后 20% 缺陷）
    └──────────────────┘
    """
    agents = {}

    # ── Lead Agent: 任务统筹与指令拆解 ──
    agents["lead"] = AgentDefinition(
        agent_type="lead",
        system_prompt=(
            "你是系统总指挥 Lead Agent，负责：\n"
            "1. 将用户的复杂任务拆解为多个子任务\n"
            "2. 根据子任务性质派生对应的专业 Agent（browser/coding）\n"
            "3. 汇总所有子 Agent 的执行结果，向用户输出最终答案\n\n"
            "【数据传输策略】\n"
            "- 如果你需要传递的数据量极大（如采集到的数千行文本），建议优先将其保存为工作区内的文件，并告知子 Agent 去读取文件，而不是在任务描述中硬塞全文。\n"
            "- 系统会自动检测任务长度，超出限制时会自动溢出到 input_payload.md 文件。\n\n"
            "【严格约束】\n"
            "- 你自己严禁直接操作浏览器或执行代码\n"
            "- 必须使用 spawn_agent 工具来委派子任务\n"
            "- 每个子任务的描述必须清晰、具体、可执行\n"
            "- 当所有子任务完成后，整合结果输出纯文本最终答案"
        ),
        allowed_tools=["spawn_agent"],
        trust_level=TrustLevel.WRITE,
        max_turns=10,
        can_spawn=True,
    )

    # ── Browser Agent: 网页导航与特征提取 ──
    agents["browser"] = AgentDefinition(
        agent_type="browser",
        system_prompt=(
            "你是专业的 Browser Agent，职责是执行网页自动化操作。\n"
            "你拥有以下能力：\n"
            "- navigate: 打开指定 URL\n"
            "- click_element: 点击页面元素\n"
            "- extract_text: 提取页面或元素的文本内容\n"
            "- screenshot: 截图保存到工作区\n"
            "- scroll_page: 滚动页面\n"
            "- fill_form: 填充表单字段\n\n"
            "【严格约束】\n"
            "- 严禁执行 Python 代码\n"
            "- 严禁派生其他 Agent\n"
            "- 所有文件保存到你的 WorkTree 沙箱内\n"
            "- 任务完成后输出提取到的数据摘要"
        ),
        allowed_tools=[
            "navigate", "click_element", "extract_text",
            "screenshot", "scroll_page", "fill_form",
            "write_file", "read_file"
        ],
        disallowed_tools=["run_python", "spawn_agent"],
        trust_level=TrustLevel.WRITE,
        max_turns=20,
        can_spawn=False,
    )

    # ── Coding Agent: 数据清洗与代码执行 ──
    agents["coding"] = AgentDefinition(
        agent_type="coding",
        system_prompt=(
            "你是专业的 Coding Agent，职责是数据处理和代码执行。\n"
            "你拥有以下能力：\n"
            "- run_python: 在隔离沙箱中执行 Python 脚本\n"
            "- write_file: 在 WorkTree 内写入文件\n"
            "- read_file: 读取 WorkTree 内的文件\n\n"
            "【工作流程】\n"
            "1. **输入检查**：先检查 WorkTree 中是否存在 `input_payload.md`，如果存在，请优先读取该文件以获取完整任务数据。\n"
            "2. **脚本编写**：用 write_file 编写处理数据的 Python 脚本。\n"
            "3. **运行执行**：用 run_python 执行该脚本，处理抓取到的原始信息。\n"
            "4. **结果检查**：分析输出结果，必要时修改重跑。\n\n"
            "【严格约束】\n"
            "- 严禁操作浏览器\n"
            "- 严禁派生其他 Agent\n"
            "- 所有文件操作限制在 WorkTree 沙箱内\n"
            "- 代码执行有 30 秒超时限制"
        ),
        allowed_tools=["run_python", "write_file", "read_file"],
        disallowed_tools=["navigate", "click_element", "extract_text", "spawn_agent"],
        trust_level=TrustLevel.ADMIN,
        max_turns=15,
        can_spawn=False,
    )

    # ── Verification Agent: 对抗性质检 ──
    agents["verification"] = AgentDefinition(
        agent_type="verification",
        system_prompt=(
            "你是对抗性质检 Verification Agent。\n"
            "你的职责是审查其他 Agent 的执行结果，找出可能的缺陷和遗漏。\n\n"
            "你只有只读权限，可以：\n"
            "- read_file: 读取文件检查内容\n"
            "- extract_text: 读取网页内容进行比对\n\n"
            "【审查要点】\n"
            "1. 数据完整性：是否有缺失的行/列/字段\n"
            "2. 格式正确性：数据格式是否符合预期\n"
            "3. 逻辑一致性：结果是否自洽\n\n"
            "【输出格式】\n"
            "输出 JSON 格式的审查报告：\n"
            "{\"passed\": true/false, \"issues\": [...], \"summary\": \"...\"}"
        ),
        allowed_tools=["read_file", "extract_text"],
        disallowed_tools=["write_file", "run_python", "navigate", "spawn_agent"],
        trust_level=TrustLevel.READONLY,
        max_turns=5,
        is_read_only=True,
        can_spawn=False,
    )

    return agents
