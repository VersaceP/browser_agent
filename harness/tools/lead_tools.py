"""
harness.tools.lead_tools - LeadAgent tool schemas and dispatch factory.
"""

from typing import Any, Awaitable, Callable, List, Tuple

from harness.local_fs import local_fs_jsonpath, local_fs_read, local_fs_search
from harness.tools.loop_guard import check_tool_call_loop
from harness.tools.parsers import parse_json_arg, parse_plan_steps_arg
from harness.utils import JsonDict, optional_int


LeadToolDispatcher = Callable[[JsonDict], Awaitable[Tuple[JsonDict, bool]]]


def build_lead_tool_dispatcher(agent: Any) -> LeadToolDispatcher:
    async def dispatch(tool_call: JsonDict) -> Tuple[JsonDict, bool]:
        return await execute_lead_tool(agent, tool_call)

    return dispatch


async def execute_lead_tool(agent: Any, tool_call: JsonDict) -> Tuple[JsonDict, bool]:
    name = str(tool_call.get("name") or "")
    tool_input = tool_call.get("input") or {}

    if name == "final_answer":
        return {
            "status": tool_input.get("status", "done"),
            "answer": str(tool_input.get("answer", "")).strip(),
        }, True

    if name == "emit_task_plan":
        result = agent.accept_task_plan(tool_input.get("plan"))
        return result, False

    # Loop guard: short-circuit if LeadAgent is hammering the same tool with
    # identical args (e.g. repeatedly listing browser agents or running the
    # same plan template). The current step isn't plumbed through the
    # dispatch signature, so we fall back to the value the run loop stashes
    # on the agent before each dispatch.
    short_circuit = check_tool_call_loop(
        agent,
        name=name,
        tool_input=tool_input,
        step=getattr(agent, "_current_step", 0),
    )
    if short_circuit is not None:
        return short_circuit

    if name == "spawn_browser_agent":
        if getattr(agent, "task_plan", None) is None:
            return {
                "status": "plan_required",
                "error": "LeadAgent must call emit_task_plan successfully before spawning BrowserAgents.",
                "next_instruction": "Emit a valid task_plan with phases, expected_artifact, and validators.",
            }, False
        phase_id = tool_input.get("phase_id")
        phase = agent.resolve_phase_for_spawn(
            str(phase_id) if isinstance(phase_id, str) and phase_id.strip() else None
        )
        if phase is None:
            return {
                "status": "failed",
                "error": f"phase not found or no pending phase: {phase_id}",
            }, False
        raw_contract = tool_input.get("worker_contract")
        worker_contract = agent.build_worker_contract(
            phase,
            raw_contract if isinstance(raw_contract, dict) else None,
        )
        strategy_guidance = ""
        if hasattr(agent, "strategy_guidance_for_phase"):
            strategy_guidance = agent.strategy_guidance_for_phase(phase)
        base_task = str(tool_input.get("task") or phase.get("worker_task") or "")
        base_context = str(tool_input.get("context") or phase.get("context") or "")
        if strategy_guidance:
            base_context = (
                f"{base_context}\n\n{strategy_guidance}".strip()
            )
        result = await agent.spawner.spawn_browser_agent(
            task=base_task,
            context=base_context,
            name=tool_input.get("name") or None,
            max_steps=tool_input.get("max_steps"),
            result_contract=str(tool_input.get("result_contract") or ""),
            phase_id=str(phase.get("id") or ""),
            worker_contract=worker_contract,
        )
        return result, False

    if name == "wait_browser_agents":
        result = await agent.spawner.wait_browser_agents(
            worker_ids=tool_input.get("worker_ids"),
            mode=tool_input.get("mode", "all"),
            timeout_seconds=tool_input.get("timeout_seconds"),
        )
        return result, False

    if name == "list_browser_agents":
        return agent.spawner.list_browser_agents(), False

    if name == "local_fs_search":
        return local_fs_search(
            agent.logger,
            glob_pattern=str(tool_input.get("glob") or "**/*"),
            pattern=(
                str(tool_input.get("pattern"))
                if tool_input.get("pattern") is not None else None
            ),
            event_type=(
                str(tool_input.get("event_type"))
                if tool_input.get("event_type") is not None else None
            ),
            max_results=optional_int(tool_input.get("max_results"), 20) or 20,
            max_bytes_per_hit=(
                optional_int(tool_input.get("max_bytes_per_hit"), 2000) or 2000
            ),
            max_total_bytes=(
                optional_int(tool_input.get("max_total_bytes"), 20000) or 20000
            ),
        ), False

    if name == "local_fs_read":
        return local_fs_read(
            agent.logger,
            path=str(tool_input.get("path") or ""),
            line_offset=optional_int(tool_input.get("line_offset"), 0) or 0,
            line_limit=optional_int(tool_input.get("line_limit"), 200) or 200,
            max_bytes=min(
                optional_int(
                    tool_input.get("max_bytes"),
                    agent.runtime.harness.local_fs_max_read_bytes,
                ) or agent.runtime.harness.local_fs_max_read_bytes,
                agent.runtime.harness.local_fs_max_read_bytes,
            ),
        ), False

    if name == "local_fs_jsonpath":
        return local_fs_jsonpath(
            agent.logger,
            path=str(tool_input.get("path") or ""),
            expr=str(tool_input.get("expr") or "$"),
            mode=str(tool_input.get("mode") or "auto"),
            max_nodes=optional_int(tool_input.get("max_nodes"), 50) or 50,
            max_bytes_per_node=(
                optional_int(tool_input.get("max_bytes_per_node"), 1000) or 1000
            ),
        ), False

    if name == "run_browser_batch":
        items, error = parse_json_arg(
            agent,
            name,
            tool_input,
            "items_json",
            list,
            "items_json must be a JSON array string",
        )
        if error:
            return error, False
        result = await agent.spawner.run_browser_batch(
            items=items,
            task_template=str(tool_input.get("task_template", "")),
            context_template=str(tool_input.get("context_template") or ""),
            concurrency=tool_input.get("concurrency"),
            max_steps=tool_input.get("max_steps"),
        )
        return result, False

    if name == "run_skill_agent":
        raw_evidence = tool_input.get("evidence_artifacts") or []
        if isinstance(raw_evidence, str):
            raw_evidence = [raw_evidence]
        evidence_artifacts = [
            str(p) for p in raw_evidence
            if isinstance(p, (str,)) and str(p).strip()
        ]
        result = await agent.spawner.run_skill_agent(
            task=str(tool_input.get("task", "")),
            input_context=str(tool_input.get("input_context") or ""),
            output_schema=str(tool_input.get("output_schema") or ""),
            evidence_artifacts=evidence_artifacts,
        )
        return result, False

    if name == "execute_abcp_plan":
        steps, error = parse_plan_steps_arg(agent, name, tool_input)
        if error:
            return error, False
        variables, error = parse_json_arg(
            agent,
            name,
            tool_input,
            "variables_json",
            dict,
            "variables_json must be a JSON object string; pass \"{}\" when there are no variables",
        )
        if error:
            return error, False
        result = await agent.spawner.execute_abcp_plan(
            steps=steps,
            variables=variables,
            agent_name=tool_input.get("agent_name") or None,
            context=str(tool_input.get("context") or ""),
        )
        return result, False

    if name == "run_abcp_plan_batch":
        items, error = parse_json_arg(
            agent,
            name,
            tool_input,
            "items_json",
            list,
            "items_json must be a JSON array string",
        )
        if error:
            return error, False
        steps, error = parse_plan_steps_arg(agent, name, tool_input)
        if error:
            return error, False
        variables, error = parse_json_arg(
            agent,
            name,
            tool_input,
            "variables_json",
            dict,
            "variables_json must be a JSON object string; pass \"{}\" when there are no variables",
        )
        if error:
            return error, False
        result = await agent.spawner.run_abcp_plan_batch(
            items=items,
            steps=steps,
            variables=variables,
            context_template=str(tool_input.get("context_template") or ""),
            concurrency=tool_input.get("concurrency"),
            validate_first_n=tool_input.get("validate_first_n"),
        )
        return result, False

    result = {
        "status": "failed",
        "error": f"Unknown LeadAgent tool: {name}",
    }
    agent.logger.write("lead.tool.error", result)
    return result, False


def build_lead_agent_tool_specs() -> List[JsonDict]:
    def nullable(type_name: str) -> JsonDict:
        return {"type": [type_name, "null"]}

    return [
        {
            "name": "emit_task_plan",
            "description": (
                "Submit the structured v1 task plan before spawning any worker."
                " The harness validates and persists task_plan.json and task_state.json."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "plan": {
                        "type": "object",
                        "additionalProperties": True,
                        "description": (
                            "Plan object with goal, task_type, and a linear phases array."
                            " Each phase needs id, type='browser_worker', objective,"
                            " worker_task, expected_artifact, validators, and max_attempts."
                        ),
                    },
                },
                "required": ["plan"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "name": "spawn_browser_agent",
            "description": "Asynchronously spawn a BrowserAgent with its own context and independent ABCP WebSocket.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {
                        **nullable("string"),
                        "description": "BrowserAgent name; pass null to auto-name.",
                    },
                    "phase_id": {
                        **nullable("string"),
                        "description": "The task_plan phase id this worker executes. Pass null to use the next pending phase.",
                    },
                    "task": {"type": "string"},
                    "context": {
                        "type": "string",
                        "description": "Subtask context; pass an empty string when none.",
                    },
                    "result_contract": {
                        "type": "string",
                        "description": "Structure / fields you expect the BrowserAgent to put in `answer`; pass an empty string when there are no extra requirements.",
                    },
                    "max_steps": {
                        **nullable("integer"),
                        "description": "Override the worker's max step count; pass null to use the default.",
                    },
                    "worker_contract": {
                        "type": "object",
                        "additionalProperties": True,
                        "description": (
                            "Contract override; pass {} when the phase contract is enough."
                            " The harness merges it with the"
                            " phase's expected_artifact, validators, allowed_methods,"
                            " forbidden_methods, max_surface_attempts, and stop_condition."
                        ),
                    },
                },
                "required": [
                    "name",
                    "phase_id",
                    "task",
                    "context",
                    "result_contract",
                    "max_steps",
                    "worker_contract",
                ],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "name": "wait_browser_agents",
            "description": "Wait for spawned BrowserAgents to complete; wait for all of them or the first one to finish.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "worker_ids": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                        "description": "List of workerIds to wait for; pass null to wait for every spawned agent.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["all", "first"],
                    },
                    "timeout_seconds": {
                        **nullable("number"),
                        "description": "Wait timeout in seconds; pass null for no limit.",
                    },
                },
                "required": ["worker_ids", "mode", "timeout_seconds"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "name": "list_browser_agents",
            "description": "Inspect the runtime status of currently spawned BrowserAgents.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "name": "local_fs_search",
            "description": "Read-only search across files inside the current task worktree; supports glob, JSONL event-type filtering, and per-hit / total output caps.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex grep; pass an empty string to list matches by glob / event_type only.",
                    },
                    "glob": {
                        "type": "string",
                        "description": "Glob relative to the current task worktree, e.g. traces/*.jsonl or observations/*.json.",
                    },
                    "event_type": {
                        "type": ["string", "null"],
                        "description": "JSONL-only: restrict the search to lines whose `event` matches this string; pass null when not needed.",
                    },
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
                    "max_bytes_per_hit": {"type": "integer", "minimum": 200, "maximum": 20000},
                    "max_total_bytes": {"type": "integer", "minimum": 1000, "maximum": 200000},
                },
                "required": [
                    "pattern",
                    "glob",
                    "event_type",
                    "max_results",
                    "max_bytes_per_hit",
                    "max_total_bytes",
                ],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "name": "local_fs_read",
            "description": "Read-only line-range read of a file inside the current task worktree; well suited to JSONL traces and AXTree lines.txt offload files.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "line_offset": {"type": "integer", "minimum": 0},
                    "line_limit": {"type": "integer", "minimum": 1, "maximum": 5000},
                    "max_bytes": {"type": "integer", "minimum": 1000, "maximum": 200000},
                },
                "required": ["path", "line_offset", "line_limit", "max_bytes"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "name": "local_fs_jsonpath",
            "description": "Read-only read of a JSON/JSONL file inside the current task worktree, with a JSONPath subset for node extraction.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "expr": {
                        "type": "string",
                        "description": "Supports $.a.b[0], [*], .*, ..field, ..*.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["auto", "json", "jsonl"],
                    },
                    "max_nodes": {"type": "integer", "minimum": 1, "maximum": 500},
                    "max_bytes_per_node": {"type": "integer", "minimum": 100, "maximum": 50000},
                },
                "required": [
                    "path",
                    "expr",
                    "mode",
                    "max_nodes",
                    "max_bytes_per_node",
                ],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "name": "run_browser_batch",
            "description": "Concurrently spawn multiple BrowserAgents from a template; use only when a deterministic ABCP plan cannot be reused or the page requires LLM judgement.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "items_json": {
                        "type": "string",
                        "description": "JSON array string; URL-string items expand to {item.value}.",
                    },
                    "task_template": {
                        "type": "string",
                        "description": "Supports template variables such as {item.url}, {item.title}, {index}.",
                    },
                    "context_template": {
                        "type": "string",
                        "description": "Supports the same template variables as task_template.",
                    },
                    "concurrency": {
                        **nullable("integer"),
                        "description": "Concurrency level; pass null to use the default.",
                    },
                    "max_steps": {
                        **nullable("integer"),
                        "description": "Max step count for each BrowserAgent; pass null to use the default.",
                    },
                },
                "required": [
                    "items_json",
                    "task_template",
                    "context_template",
                    "concurrency",
                    "max_steps",
                ],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "name": "run_skill_agent",
            "description": (
                "Spawn a browserless SkillAgent that summarises an execution trace and produces an"
                " extraction strategy or ABCP-step template."
                " **Evidence contract**: any structured data sourced from an upstream BrowserAgent"
                " (hrefs / ids / field values, etc.) MUST be passed via `evidence_artifacts` as the"
                " corresponding record_extraction artifact paths. Leaf values in the SkillAgent output"
                " that cannot be found in the evidence MUST be marked \"<unverified>\" or left empty;"
                " never infer values from naming conventions (e.g. do not synthesise URLs from slugs)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "input_context": {"type": "string"},
                    "output_schema": {"type": "string"},
                    "evidence_artifacts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Absolute paths of record_extraction artifacts produced by upstream"
                            " BrowserAgents. SkillAgent may only synthesise URLs / ids and other"
                            " critical fields from these artifacts. Pass [] when there is no evidence."
                        ),
                    },
                },
                "required": ["task", "input_context", "output_schema", "evidence_artifacts"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "name": "execute_abcp_plan",
            "description": "Sequentially execute a deterministic ABCP-method step list for a single item on its own ABCP WebSocket. Suitable for single-point debugging or fixing a failed_step. For batch reuse validation, prefer run_abcp_plan_batch(validate_first_n).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "agent_name": {
                        **nullable("string"),
                        "description": "Plan executor name; pass null to auto-name.",
                    },
                    "context": {"type": "string"},
                    "variables_json": {
                        "type": "string",
                        "description": "JSON object string; pass \"{}\" when there are no variables.",
                    },
                    "steps_json": {
                        "type": "string",
                        "description": "JSON array string; each step has `method`, `params`, and optionally `save_as`.",
                    },
                },
                "required": [
                    "agent_name",
                    "context",
                    "variables_json",
                    "steps_json",
                ],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "name": "run_abcp_plan_batch",
            "description": "Once the template has passed reuse validation, execute the same deterministic ABCP steps across `items` concurrently. Each item gets its own WebSocket/agentId; the LLM is not called per URL.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "items_json": {
                        "type": "string",
                        "description": "JSON array string; each item is injected as {item} into steps; URL strings expand to {item.value}.",
                    },
                    "variables_json": {
                        "type": "string",
                        "description": "JSON object string; shared variable template across the batch; may reference {item.url}, {item.value}, {index}; pass \"{}\" when there are no variables.",
                    },
                    "context_template": {
                        "type": "string",
                        "description": "Optional log-context template; supports the same template variables as `variables`.",
                    },
                    "concurrency": {
                        **nullable("integer"),
                        "description": "Concurrency level; pass null to use the default.",
                    },
                    "validate_first_n": {
                        **nullable("integer"),
                        "description": "Serially validate the first N items first; fan out only once they all pass. Recommended value for detail-page extraction is 2 or 3.",
                    },
                    "steps_json": {
                        "type": "string",
                        "description": "JSON array string; each step has `method`, `params`, and optionally `save_as`.",
                    },
                },
                "required": [
                    "items_json",
                    "variables_json",
                    "context_template",
                    "concurrency",
                    "validate_first_n",
                    "steps_json",
                ],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "name": "final_answer",
            "description": "Terminate LeadAgent orchestration and return the final result.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["done", "blocked", "failed"],
                    },
                    "answer": {"type": "string"},
                },
                "required": ["status", "answer"],
                "additionalProperties": False,
            },
            "strict": True,
        },
    ]
