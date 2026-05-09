"""execution_loop — 通用 LLM 驱动循环。

vs v5 简化点:
- 无 hook bus(PRE/POST_TOOL_EXECUTE 等 9 事件)→ 直接调 dispatch
- 无 large_input_compactor / repetition_compactor → tool 自身限制返回值大小
- 无 80% LLM 摘要压缩 → 简化为"近上限时给 LLM 一个软提示"
- 无 spill 落盘自动机 → tool 返回值由 format_result_for_llm 截断,大数据 LLM 主动用 save_artifact

事件 generator 流程(每 turn):
1. 检查 token 水位 → 软提示
2. 调 LLM → 文本 + tool_uses + stop_reason
3. yield 'llm_text' / 'tool_use' 事件
4. 串行 dispatch 每个 tool → yield 'tool_result'
5. 把 tool_result 写回 messages
6. 若 stop_reason != 'tool_use',结束;否则继续
"""
import asyncio
from typing import Any, AsyncGenerator, Dict, List, Optional, TYPE_CHECKING

from core.context import TeammateContext
from core.agent_definition import AgentDefinition
from core.llm_provider import BaseLLMProvider
from tools.base import ToolRegistry, ToolContext, dispatch, format_result_for_llm

if TYPE_CHECKING:
    pass


# 软提示阈值 — 超过这个比例,系统在 user 消息里追加压缩提醒
SOFT_TOKEN_HINT_RATIO = 0.85


async def execute_turn(
    context: TeammateContext,
    agent_def: AgentDefinition,
    llm: BaseLLMProvider,
    registry: ToolRegistry,
    tool_ctx: ToolContext,
    browser_lock: Optional[asyncio.Lock] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """驱动一个 agent 跑到结束(或 max_turns 上限)。

    Args:
        context: 状态容器(被 mutate:每轮追加 messages、累加 token)
        agent_def: agent 人格
        llm: LLM provider 实例
        registry: 全部工具注册表(本函数内按 agent_def.allowed_tools 过滤)
        tool_ctx: 给每个 tool dispatch 时传入(含 worktree / pool / spawner)
        browser_lock: 当 agent_def.uses_browser 时,worker 调浏览器 tool 前 acquire

    Yields:
        事件 dict,字段含 type / turn / ...

    最终(generator 结束前)yield 一次:
        {"type": "agent_complete", "stop_reason": ..., "final_text": ..., "turns_used": N}
    """
    # 第一次 turn 把 task 作为初始 user message(若 messages 为空)
    if not context.messages:
        context.append_message("user", _build_initial_user_msg(context))

    # 过滤工具白名单 — 统一用 Anthropic 格式,OpenAI provider 内部自动转
    available_tools = registry.filter_by_names(agent_def.allowed_tools)
    tool_schemas = [t.to_anthropic_schema() for t in available_tools]

    final_text = ""
    stop_reason = ""
    fatal_error = False

    for turn in range(1, agent_def.max_turns + 1):
        yield {"type": "turn_start", "turn": turn, "agent": agent_def.agent_type}

        # 1. 软提示压缩
        if context.get_token_ratio() > SOFT_TOKEN_HINT_RATIO:
            context.append_message(
                "user",
                f"[system] Context is at {context.get_token_ratio():.0%} of limit "
                f"({context.token_usage}/{context.max_tokens} tokens). "
                f"Wrap up: aggregate / save_artifact / publish_artifact and end the task. "
                f"Avoid further large tool outputs."
            )

        # 2. LLM 调用
        try:
            text, tool_uses, stop_reason, usage = await _call_llm(
                llm, agent_def.system_prompt, context.messages, tool_schemas
            )
            # 记录缓存命中累计(给 /stats 看效果)
            context.record_llm_usage(
                cache_read=usage.get("cache_read", 0),
                cache_creation=usage.get("cache_creation", 0),
                uncached_input=usage.get("uncached_input", 0),
                output_tokens=usage.get("output", 0),
            )
        except Exception as e:
            yield {"type": "llm_error", "error": str(e), "turn": turn}
            fatal_error = True
            stop_reason = "fatal_error"
            final_text = f"LLM call failed at turn {turn}: {e}"
            break

        # 3. 写回 assistant message + yield 文本/tool_use 事件
        # Anthropic 风格 content blocks:[{"type":"text","text":...}, {"type":"tool_use",...}]
        assistant_blocks: List[Dict[str, Any]] = []
        if text:
            assistant_blocks.append({"type": "text", "text": text})
            yield {"type": "llm_text", "content": text, "turn": turn}
        for tu in tool_uses:
            assistant_blocks.append({
                "type": "tool_use",
                "id": tu["id"],
                "name": tu["name"],
                "input": tu["input"],
            })
            yield {"type": "tool_use", "name": tu["name"], "input": tu["input"], "id": tu["id"], "turn": turn}

        if not assistant_blocks:
            # 罕见:LLM 啥也没回(可能是流截断)→ 强行结束
            yield {"type": "warn", "msg": "empty assistant response", "turn": turn}
            stop_reason = "empty"
            break

        context.append_message("assistant", assistant_blocks)

        # 4. 终止判断
        if stop_reason != "tool_use":
            final_text = text or ""
            break

        # 5. 串行 dispatch 工具(dispatch 自身是 async,sync handler 内部走 to_thread)
        tool_result_blocks: List[Dict[str, Any]] = []
        for tu in tool_uses:
            # 浏览器 tool 在多 worker 并发场景下需要 browser_lock 串行
            # 当前 single-agent 路径下,LLM 自己一轮串行调,基本不撞;但加锁以防 spawn_agents_parallel 触发
            needs_lock = (
                browser_lock is not None
                and agent_def.uses_browser
                and _is_browser_tool(tu["name"])
            )
            # 注入当前 tool_use_id,供 handler(如 read_file)做 supersede 标记
            tool_ctx.tool_use_id = tu["id"]
            if needs_lock:
                async with browser_lock:
                    result = await dispatch(registry, tu["name"], tu["input"], tool_ctx)
            else:
                result = await dispatch(registry, tu["name"], tu["input"], tool_ctx)

            # 处理 supersede:某些工具(read_file)在新一次成功调用后,会标记之前同 target
            # 的 tool_use_id 应被覆盖,以避免渐进读取在上下文里堆累积副本
            supersede_ids = tool_ctx.extra.pop("supersede_tool_use_ids", None)
            if supersede_ids:
                _supersede_tool_results(context.messages, supersede_ids)

            formatted = format_result_for_llm(result)
            tool_result_blocks.append({
                "type": "tool_result",
                "tool_use_id": tu["id"],
                "content": formatted,
                "is_error": result["status"] == "error",
            })
            yield {
                "type": "tool_result",
                "name": tu["name"],
                "id": tu["id"],
                "status": result["status"],
                "preview": formatted[:200],
                "turn": turn,
            }

        # 6. 写回 user message 含全部 tool_result blocks
        context.append_message("user", tool_result_blocks)

        yield {"type": "turn_end", "turn": turn, "stop_reason": stop_reason}

    # 收尾
    yield {
        "type": "agent_complete",
        "agent": agent_def.agent_type,
        "stop_reason": stop_reason,
        "final_text": final_text,
        "turns_used": turn,
        "token_usage": context.token_usage,
        "fatal_error": fatal_error,
    }


# ──────────────────────────────────
# 内部
# ──────────────────────────────────

def _build_initial_user_msg(context: TeammateContext) -> str:
    """组装第一条 user 消息 — 含任务 + 工作区路径 + 共享区路径"""
    parts = [f"Task:\n{context.task}"]
    parts.append("")
    parts.append(f"Worktree (your private sandbox): {context.worktree_path}")
    if context.shared_dir:
        parts.append(f"Shared dir (cross-agent delivery): {context.shared_dir}")
    if context.env_vars:
        parts.append(f"Env vars: {list(context.env_vars.keys())}")
    return "\n".join(parts)


async def _call_llm(
    llm: BaseLLMProvider,
    system_prompt: str,
    messages: List[Dict[str, Any]],
    tool_schemas: List[Dict[str, Any]],
):
    """统一 LLM 调用包装 — tool_schemas 用 Anthropic 格式,OpenAIProvider 内部自动转。

    Returns:
        (text, tool_uses, stop_reason, usage)
        tool_uses: List[{id, name, input}]
        stop_reason: 'end_turn' | 'tool_use' | 'max_tokens' | ...
        usage: {cache_read, cache_creation, uncached_input, output}
    """
    return await llm.generate_response(
        system_prompt=system_prompt,
        messages=messages,
        tools=tool_schemas,
    )


def _is_browser_tool(name: str) -> bool:
    """判断一个 tool 是否触碰浏览器(需要 browser_lock)"""
    return name in {
        "navigate", "click", "fill", "extract_text", "screenshot",
        "scroll", "wait_user", "accept_dialog", "dismiss_dialog",
    }


_SUPERSEDE_STUB = (
    "[superseded] 这条 tool_result 已被同 session 后续同 target 的调用覆盖。"
    "原内容已不再相关 — 请看后面对应工具的最新 tool_result。"
)


def _supersede_tool_results(messages: List[Dict[str, Any]], ids_to_supersede: List[str]) -> None:
    """把 messages 历史里指定 tool_use_id 对应的 tool_result block 内容改写成短 stub。

    用途:read_file 渐进读取(10K → 20K → 50K → 摘要)同 path 多次调,
    后一次内容覆盖前一次,前面的 tool_result 留个 stub 占位即可,主上下文省下大量 token。

    注意:
    - 只改 content / is_error 两个字段;tool_use_id / type 不动(保 LLM API 协议合法)
    - 已经被 superseded 过的(content == stub)再 supersede 一次也无副作用
    - 改 messages 会让 Anthropic prompt cache 失效一次(本轮 cache miss),
      但下一轮新 marker 会重建 — 净收益是上下文短得多,长期更省
    """
    if not ids_to_supersede:
        return
    id_set = set(ids_to_supersede)
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for i, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_result":
                continue
            if block.get("tool_use_id") in id_set:
                # 用 dict 覆盖,保留 type / tool_use_id,只改 content / is_error
                content[i] = {
                    "type": "tool_result",
                    "tool_use_id": block["tool_use_id"],
                    "content": _SUPERSEDE_STUB,
                    "is_error": False,
                }
