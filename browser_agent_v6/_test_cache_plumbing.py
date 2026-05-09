"""验证 prompt cache 协议 + usage 累计 — 用 stub provider 不真起 LLM。

覆盖:
  ① context.record_llm_usage 累加正确
  ② cache_summary() hit_rate 计算正确
  ③ Anthropic 端 cache_control 结构:tools[-1] / system / messages[-1] 都有 marker
  ④ Anthropic 端不会污染调用方 messages 列表
  ⑤ OpenAI 端 cache_control 结构:system + messages[-1] 末块有 marker;tools 不加 marker(百炼 tools 不能独立缓存)
  ⑥ execute_turn 在每轮 LLM 后调 record_llm_usage
"""
import sys, os, asyncio, json, copy
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.pop("SSL_CERT_FILE", None)
if sys.platform == "win32":
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except: pass

print("="*60)
print("  Prompt cache 协议 + usage 累计")
print("="*60)

from core.context import TeammateContext

# ──────────────────────────────────
# ① context.record_llm_usage 累加
# ──────────────────────────────────
print("\n[1] context.record_llm_usage 累加")
ctx = TeammateContext(agent_type="lead", session_id="t1", task="x")
ctx.record_llm_usage(cache_read=0, cache_creation=2000, uncached_input=300, output_tokens=120)
ctx.record_llm_usage(cache_read=2000, cache_creation=0, uncached_input=80, output_tokens=50)
ctx.record_llm_usage(cache_read=2000, cache_creation=0, uncached_input=120, output_tokens=70)
cs = ctx.cache_summary()
assert cs["llm_calls"] == 3
assert cs["cache_read"] == 4000
assert cs["cache_creation"] == 2000
assert cs["uncached_input"] == 500
assert cs["output"] == 240
assert cs["total_input"] == 6500
assert cs["cache_hit_rate"] == round(4000 / 6500, 3)
print(f"  ✓ {cs}")

# ──────────────────────────────────
# ② to_dict / from_dict 序列化
# ──────────────────────────────────
print("\n[2] 序列化往返保留 cache stats")
d = ctx.to_dict()
assert d["cache_read_total"] == 4000
ctx2 = TeammateContext.from_dict(d)
assert ctx2.cache_summary() == cs
print(f"  ✓ to_dict / from_dict 携带 cache stats")

# ──────────────────────────────────
# ③ from_dict 兼容老 session(无 cache 字段)
# ──────────────────────────────────
print("\n[3] from_dict 兼容老 session 文件(无 cache 字段)")
old = {"agent_type":"worker","session_id":"old","task":"x"}
ctx3 = TeammateContext.from_dict(old)
assert ctx3.cache_summary()["llm_calls"] == 0
print(f"  ✓ 老 session 加载不报错,cache_summary 默认 0")

# ──────────────────────────────────
# ④ AnthropicProvider 的 cache_control 结构 — 不真发请求,只检查构造的 kwargs
# ──────────────────────────────────
print("\n[4] AnthropicProvider 构造请求 cache_control 三处 marker")

# stub anthropic SDK
captured_kwargs = {}

class _StubResp:
    class usage:
        input_tokens = 100
        cache_read_input_tokens = 500
        cache_creation_input_tokens = 200
        output_tokens = 50
    content = []
    stop_reason = "end_turn"

class _StubMessages:
    async def create(self, **kwargs):
        captured_kwargs.update(kwargs)
        return _StubResp()

class _StubAnthropicClient:
    messages = _StubMessages()

import core.llm_provider as llm_module
from core.llm_provider import AnthropicProvider, ModelConfig

# 用 monkeypatch 把 AsyncAnthropic 替成 stub
original_async_anthropic = llm_module.AsyncAnthropic
llm_module.AsyncAnthropic = lambda **kw: _StubAnthropicClient()
try:
    cfg = ModelConfig(provider="anthropic", model_id="claude-sonnet-4-6", api_key="fake")
    provider = AnthropicProvider(cfg)

    original_messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "second"},
    ]
    snapshot = copy.deepcopy(original_messages)

    tools = [
        {"name":"a","description":"a","input_schema":{"type":"object"}},
        {"name":"b","description":"b","input_schema":{"type":"object"}},
    ]

    text, calls, stop, usage = asyncio.run(
        provider.generate_response("SYS", original_messages, tools)
    )
    # ④a: system block 有 cache_control
    sys_blocks = captured_kwargs["system"]
    assert sys_blocks[0]["cache_control"]["type"] == "ephemeral"
    print("  ✓ system block 有 cache_control")
    # ④b: tools 末位有 cache_control
    sent_tools = captured_kwargs["tools"]
    assert sent_tools[-1].get("cache_control", {}).get("type") == "ephemeral"
    assert "cache_control" not in sent_tools[0]
    print(f"  ✓ tools[-1] 有 cache_control,前面没有(共 {len(sent_tools)} tool)")
    # ④c: messages 末位的最后一块有 cache_control
    sent_msgs = captured_kwargs["messages"]
    last_content = sent_msgs[-1]["content"]
    assert isinstance(last_content, list)
    assert last_content[-1].get("cache_control", {}).get("type") == "ephemeral"
    print("  ✓ messages[-1] 末块有 cache_control")
    # ④d: 不污染调用方原始 messages
    assert original_messages == snapshot, "原始 messages 被改动!"
    print("  ✓ 调用方 messages 列表未被污染(rolling marker 安全)")
    # ④e: usage 4-tuple 内容
    assert usage == {"cache_read": 500, "cache_creation": 200, "uncached_input": 100, "output": 50}
    print(f"  ✓ usage tuple = {usage}")
finally:
    llm_module.AsyncAnthropic = original_async_anthropic

# ──────────────────────────────────
# ⑤ OpenAIProvider 的 cache_control 结构(百炼 / Qwen 兼容)
# ──────────────────────────────────
print("\n[5] OpenAIProvider 构造请求 cache_control(百炼/Qwen 格式)")

captured_oai = {}

class _StubOaiResp:
    class _Usage:
        prompt_tokens = 1500
        completion_tokens = 80
        class _Details:
            cached_tokens = 1200
        prompt_tokens_details = _Details()
    usage = _Usage()
    class _Choice:
        finish_reason = "stop"
        class _Message:
            content = "ok"
            tool_calls = None
        message = _Message()
    choices = [_Choice()]

class _StubOaiCompletions:
    async def create(self, **kwargs):
        captured_oai.update(kwargs)
        return _StubOaiResp()
class _StubOaiClient:
    class chat:
        completions = _StubOaiCompletions()

original_openai = llm_module.AsyncOpenAI
llm_module.AsyncOpenAI = lambda **kw: _StubOaiClient()
try:
    cfg = ModelConfig(provider="openai", model_id="qwen-max", api_key="fake")
    provider = llm_module.OpenAIProvider(cfg)

    msgs = [
        {"role":"user","content":"first"},
        {"role":"assistant","content":"ok"},
        {"role":"user","content":"second"},
    ]
    snap = copy.deepcopy(msgs)
    tools = [{"name":"a","description":"a","input_schema":{"type":"object"}}]

    text, calls, stop, usage = asyncio.run(provider.generate_response("SYS", msgs, tools))

    # ⑤a: system 在 messages[0] 且有 cache_control
    sent = captured_oai["messages"]
    assert sent[0]["role"] == "system"
    sys_content = sent[0]["content"]
    assert isinstance(sys_content, list)
    assert sys_content[0].get("cache_control", {}).get("type") == "ephemeral"
    print("  ✓ system 在 messages[0],带 cache_control")

    # ⑤b: 末位 message 末块带 cache_control
    last_msg = sent[-1]
    last_content = last_msg["content"]
    assert isinstance(last_content, list)
    assert last_content[-1].get("cache_control", {}).get("type") == "ephemeral"
    print("  ✓ messages[-1] 末块带 cache_control")

    # ⑤c: tools 不加 cache_control(百炼 tools 不能独立缓存)
    sent_tools = captured_oai["tools"]
    for t in sent_tools:
        assert "cache_control" not in t, f"OpenAI tool 不该有 cache_control, got {t}"
        assert "cache_control" not in t.get("function", {}), f"function 不该有 cache_control"
    print(f"  ✓ tools 上不加 cache_control(共 {len(sent_tools)} tool)")

    # ⑤d: usage 4-tuple
    assert usage == {"cache_read": 1200, "cache_creation": 0, "uncached_input": 300, "output": 80}
    print(f"  ✓ usage = {usage}")
finally:
    llm_module.AsyncOpenAI = original_openai

# ──────────────────────────────────
# ⑥ 模拟 execute_turn 在每轮调 record_llm_usage
# ──────────────────────────────────
print("\n[6] execute_turn 在每轮 LLM 调用后累加到 context")
from core.execution_loop import execute_turn
from core.agent_definition import AgentDefinition
from core.llm_provider import BaseLLMProvider
from tools.base import ToolRegistry, ToolContext

class _FakeProvider(BaseLLMProvider):
    def __init__(self):
        self.call_count = 0
    async def generate_response(self, system_prompt, messages, tools):
        self.call_count += 1
        # 第 1 轮 cache miss(写入 1000),第 2 轮 cache hit(读 1000)
        if self.call_count == 1:
            return "hello", [], "end_turn", {"cache_read": 0, "cache_creation": 1000, "uncached_input": 50, "output": 30}
        return "bye", [], "end_turn", {"cache_read": 1000, "cache_creation": 0, "uncached_input": 80, "output": 25}

reg = ToolRegistry()
adef = AgentDefinition(agent_type="t", system_prompt="s", allowed_tools=[], max_turns=2)
ctx = TeammateContext(agent_type="t", session_id="x", task="task")
provider = _FakeProvider()
tctx = ToolContext(worktree=".", shared_dir=".", session_id="x", agent_type="t")

async def _drive():
    async for ev in execute_turn(ctx, adef, provider, reg, tctx):
        pass

asyncio.run(_drive())
cs = ctx.cache_summary()
# turn 1 end_turn → break,只调 1 次
assert cs["llm_calls"] == 1, cs
assert cs["cache_creation"] == 1000
assert cs["cache_read"] == 0
print(f"  ✓ 单轮 end_turn:llm_calls={cs['llm_calls']} create={cs['cache_creation']}")

print()
print("✅ 6 组测试全部通过")
