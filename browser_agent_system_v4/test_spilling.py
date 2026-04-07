import asyncio
import os
import pathlib
import sys
import shutil

# Add project root to sys.path
root_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, root_dir)

from core.agent_spawner import AgentSpawner
from toolkits.tool_registry import ToolRegistry
from core.hook_registry import HookRegistry
from core.llm_provider import BaseLLMProvider, ModelConfig
from core.worktree import WorkTreeManager
from core.context_compactor import ContextCompactor

class MockLLM(BaseLLMProvider):
    def __init__(self):
        super().__init__(ModelConfig(provider="mock"))

    async def generate_response(self, system_prompt, messages, tools):
        # Return a generator-like iterator to mock execute_turn's expectations if needed
        # But AgentSpawner calls execute_turn which calls generate_response.
        # Wait, execute_turn is an async generator.
        return "Mock Response", [], "end_turn"

# We need to mock execute_turn because it's an async generator that calls LLM.
# Or just let it run and mock the LLM call inside it.

async def test_payload_spilling():
    test_dir = os.path.join(root_dir, "test_worktrees")
    if os.path.exists(test_dir):
        shutil.rmtree(test_dir)
    os.makedirs(test_dir)

    tr = ToolRegistry()
    hr = HookRegistry()
    llm = MockLLM()
    wtm = WorkTreeManager(base_dir=test_dir)
    cc = ContextCompactor()

    spawner = AgentSpawner(tr, hr, llm, wtm, cc)
    spawner.register_builtin_agents()

    # Create a large task
    large_task = "X" * 15000
    
    print("Testing payload spilling with 15000 chars...")
    
    # We don't actually need to RUN the whole execution loop to test spilling.
    # Spilling happens in AgentSpawner.spawn BEFORE execute_turn is called.
    # But spawn() calls execute_turn. So we might get an error there, but we can check the file first.
    
    try:
        # This will likely fail during execution because we haven't fully mocked execute_turn
        # but the spilling logic should have already run.
        await spawner.spawn(
            agent_type="coding",
            task=large_task
        )
    except Exception as e:
        print(f"Caught expected execution error (mock LLM): {e}")

    # Find the newly created worktree
    worktrees = os.listdir(test_dir)
    if not worktrees:
        print("FAILURE: No worktree created.")
        return

    latest_wt = os.path.join(test_dir, worktrees[0])
    payload_file = pathlib.Path(latest_wt) / "input_payload.md"

    if payload_file.exists():
        print(f"SUCCESS: Payload file created at {payload_file}")
        content = payload_file.read_text(encoding="utf-8")
        if content == large_task:
            print("SUCCESS: Content matches exactly.")
        else:
            print(f"FAILURE: Content mismatch. Length: {len(content)}")
    else:
        print("FAILURE: Payload file NOT created.")

if __name__ == "__main__":
    asyncio.run(test_payload_spilling())
