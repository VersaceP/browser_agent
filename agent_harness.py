"""Legacy imports. Implementations live under harness.agents and harness.runtime.

Internal entry points import the concrete modules directly.
"""

from harness.agents.browser.prompt import RUNTIME_AUTH_INTERRUPT_SOP, MULTIMODAL_RUNTIME_AUTH_INTERRUPT_SOP
from harness.agents.lead.prompt import LEAD_AUTH_PLANNING_SOP
from harness.agents.lead.loop import summarize_lead_tool_result_for_log

from abcp_client import ABCPClient, ABCPTransportError

from harness.context.compaction import compact_messages_if_needed, validate_tool_pairing

from runtime_config import ABCPClientConfig, HarnessConfig, ModelConfig, RuntimeConfig, VLConfig

from harness.tools.local_fs import local_fs_read, local_fs_search

from harness.runtime.model_config import browser_agent_model_config, lead_agent_model_config

from harness.context.offload import offload_large_response_fields, offload_large_tool_result, strip_image_payload

from harness.observation.browser_call import build_browser_call_runner, call_browser_redacted

from harness.capabilities.schema_loader import build_capability_digest

from harness.capabilities.schema_cache import SchemaCacheStatus

from harness.spawner import BrowserAgentHandle, BrowserAgentSpawner

from harness.planning.validator import plan_candidate_hash

from harness.tools.browser_tools import build_browser_agent_tool_specs, build_browser_tool_dispatcher

from harness.tools.lead_tools import build_lead_agent_tool_specs, build_lead_tool_dispatcher

from harness.utils import JsonDict, RunLogger, exception_payload, make_browser_event_logger, trim_large_strings

from llm import BaseLLMProvider, LLMFactory

from harness.runtime.model_support import (
    _guide_manifest_for,
    TRUNCATION_STREAK_LIMIT,
    INFRA_STREAK_INCIDENTS,
    INFRA_STREAK_LIMIT,
    MODEL_TIMEOUT_ATTEMPT_LIMIT,
    llm_rate_limit_terminal_result,
    _effective_streak_limit,
    _STABLE_BROWSER_METHOD_PREFIXES,
    _STABLE_BROWSER_METHODS,
    _STATE_BOUNDARY_HARNESS_TOOLS,
    _EXTENSION_HANDOFF_HINT,
    _webcross_behavioral_guide,
    _tool_call_state_boundary,
    _deferred_tool_result,
    _expire_multimodal_image_blocks,
    _pending_multimodal_image_count,
    _first_pending_multimodal_image_pair,
    _tool_result_image_accounting,
    _compact_before_multimodal_request,
    _is_context_limit_exception,
    generate_response_surviving_moderation,
    offload_tool_result_for_model,
    _json_size_bytes,
    _is_offload_stub,
    _offload_stub_stats,
    _offload_file_category,
    log_model_visible_tool_result,
    CachePressureState,
    update_cache_pressure_state,
    compact_and_track_prefix_rebuild,
    _saved_paths_from_value,
    _tool_result_is_error,
    _tool_result_digest,
    _truncation_info,
    _store_received_model_output,
    _assistant_message_to_wire,
    _assistant_message_from_parts,
    _lifecycle_recorder_for,
)

from harness.runtime.resume_context import ResumeContext

from harness.agents.browser.agent import (
    BrowserAgent,
)

from harness.agents.lead.agent import (
    LeadAgent,
    PLAN_VALIDATOR_ERROR_CACHE_TTL_SECONDS,
    PLAN_VALIDATOR_PROTOCOL_ERROR_CACHE_TTL_SECONDS,
    _assignment_prefix_errors,
)

__all__ = [
    "ABCPClient",
    "ABCPClientConfig",
    "ABCPTransportError",
    "BaseLLMProvider",
    "BrowserAgent",
    "BrowserAgentHandle",
    "BrowserAgentSpawner",
    "HarnessConfig",
    "JsonDict",
    "LLMFactory",
    "LeadAgent",
    "ModelConfig",
    "ResumeContext",
    "RuntimeConfig",
    "RunLogger",
    "VLConfig",
    "browser_agent_model_config",
    "build_browser_agent_tool_specs",
    "build_browser_tool_dispatcher",
    "build_lead_agent_tool_specs",
    "build_lead_tool_dispatcher",
    "build_capability_digest",
    "build_browser_call_runner",
    "call_browser_redacted",
    "compact_messages_if_needed",
    "exception_payload",
    "lead_agent_model_config",
    "local_fs_read",
    "local_fs_search",
    "make_browser_event_logger",
    "offload_large_response_fields",
    "offload_large_tool_result",
    "offload_tool_result_for_model",
    "strip_image_payload",
    "trim_large_strings",
    "validate_tool_pairing",
]
