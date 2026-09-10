"""
harness.constants - Shared constants for ABCP agent harness modules.
"""

CHALLENGE_KEYWORDS = (
    "captcha",
    "cloudflare",
    "verify you are human",
    "checking your browser",
    "unusual traffic",
    "are you a robot",
    "human verification",
    "turnstile",
    "hcaptcha",
    "recaptcha",
    "人机验证",
    "验证码",
    "请验证",
    "请完成安全验证",
    "完成人机验证",
    "访问验证",
    "我不是机器人",
)
NAVIGATION_CHALLENGE_TITLE_KEYWORDS = (
    "just a moment",
    "one more step",
    "checking your browser",
    "verify you are human",
    "cloudflare",
    "captcha",
    "人机验证",
    "验证码",
    "我不是机器人",
)

OFFLOAD_METHODS = {
    "DOM.getSemanticTree",
    "DOM.getAXTree",
    "DOM.getText",
    "DOM.getAttribute",
    "DOM.getImg",
}
OFFLOAD_FIELDS_AS_TEXT = {"lines"}
OFFLOAD_FIELDS_AS_JSON = {
    # getSemanticTree's payload moved from `tree` into `frames[].tree`, and
    # offload only reaches top-level fields. Without `frames` the heaviest read
    # on the surface travels into model context whole.
    "frames",
    "tree",
    "nodes",
    "ax",
    "text",
    "attributes",
    "items",
    "value",
    "layers",
}
OFFLOAD_FIELDS = OFFLOAD_FIELDS_AS_TEXT | OFFLOAD_FIELDS_AS_JSON
SCREENSHOT_METHODS = {"Page.screenshot"}

# Fleet-routing outcomes that can reach LeadAgent. Keep the guidance text and
# this catalog together so tests can mechanically reject undocumented additions.
LEAD_FLEET_ROUTING_DECISION_CODES = (
    "session_fleet_lost",
    "page_continuation_lost",
    "fleet_assignment_lost",
    "fleet_auth_gated",
    "fleet_auth_resolver_required",
    "fleet_reperception_required",
    "session_transport_unavailable",
    "session_manual_reset_required",
    "session_slot_busy",
    "fleet_owner_unavailable",
    "fleet_reference_invalid",
    "fleet_reference_not_found",
    "fleet_inventory_temporarily_unavailable",
    "ambiguous_fleet_reference",
    "reuse_fleet_lost",
    "reuse_session_conflict",
    "session_isolation_conflict",
    "fleet_routing_conflict",
    "session_binding_conflict",
    "fleet_session_conflict",
    "released_fleet_conflict",
    "task_fleet_limit_reached",
)

LEAD_FLEET_ROUTING_DECISION_GUIDANCE = """- Fleet/session routing outcomes reach the Lead on a worker result or a spawn rejection, and every one of them carries its own next_instruction naming the offending reference, whether the tool ran, and whether a retry is permitted. Follow that receipt; it knows which reference failed and this prompt cannot. When a rejection needs more than its instruction, read_harness_guide("lead.fleet-session-continuity") carries the background, and search_harness_guides resolves any of these codes to it - notably session_fleet_lost and page_continuation_lost, which are terminal for the affected session or page state, fleet_auth_gated whose fleet_auth_resolver_required variant requires you to ASSIGN a resolver rather than wait, and task_fleet_limit_reached, which waiting never clears because the harness never closes a fleet.
- Never answer a routing rejection by creating a replacement fleet, releasing a named session binding, or rebinding a session_key that the receipt did not release."""

GENERIC_TOOL_RESULT_KEEP_KEYS = (
    "method",
    "status",
    "statusCategory",
    "validatedStatus",
    "workerId",
    "agentId",
    "name",
    "phaseId",
    "tracePath",
    "resultLevels",
    "workerResultProtocol",
    "observation",
    "suggested_prompt",
    "error",
    "isError",
    "stage",
    "tool",
    "issues",
    "tool_was_executed",
    "preparedFields",
    "replayForbidden",
    "transportReceiptStatus",
    "rpcData",
    "errorClassification",
    # The visual-recovery note is the only place a failed page-facing call tells
    # the model that a visual locate exists. Offloading a large result must not
    # be what decides whether the agent knows it has that option.
    "visualRecoveryHint",
    "taskId",
    # Compact receipt emitted by the direct-worker controller. Keep it when a
    # large worker result is offloaded so Lead can distinguish an automatic
    # continuation from a normal orchestration tool response.
    "directExecution",
)
GENERIC_TOOL_RESULT_RESPONSE_KEEP_KEYS = (
    "observation",
    "suggested_prompt",
    "error",
    "isError",
    "stage",
    "tool",
    "issues",
    "tool_was_executed",
    "preparedFields",
    "replayForbidden",
    "transportReceiptStatus",
    "rpcData",
    "errorClassification",
    "taskId",
    "directExecution",
)
GENERIC_TOOL_RESULT_KEEP_FIELD_BYTES = 2000

# Used by diagnostics.classify_terminal_status for page_crashed detection.
# `status=crashed` is what Page.getState renders into its own observation
# (`Page state: status=..., title=..., url=...`); the rest are harness-side
# lifecycle event names. The raw Chromium strings that used to head this list
# ("No RenderWidgetHostView", "No WebContents") are gone: the public failure
# envelope is rebuilt from a stable code table, so no native diagnostic text
# reaches the harness any more. A dead renderer now arrives as the public code
# `renderer-lost` / `input-host-destroyed`, which error_classification maps.
PAGE_DEAD_OBSERVATION_MARKERS = (
    "status=crashed",
    "page_crashed",
    "page_load_failed",
    "Page crashed",
    "Renderer crashed",
)

# Recoverable routing classification: the worker's immutable artifact contract
# lacks the nested-array shape required by collect_items, so only Lead can fix
# it by replanning. This is deliberately not a worker/phase terminal status.
COLLECTION_CONTRACT_REPLAN_REQUIRED = "collection_contract_replan_required"

# --- Worker status taxonomy (see harness/diagnostics.py) ---
WORKER_STATUS_DONE = "done"
WORKER_STATUS_PARTIAL = "partial"
WORKER_STATUS_INCOMPLETE = "incomplete"
WORKER_STATUS_CONTEXT_LIMIT = "context_limit_exceeded"
WORKER_STATUS_HITL_REQUIRED = "hitl_required"
WORKER_STATUS_BLOCKED_BY_CHALLENGE = "blocked_by_challenge"
WORKER_STATUS_PAGE_SETTLED_AFTER_HITL = "page_settled_after_hitl"
WORKER_STATUS_STALE_PAUSE_DEADLOCK = "stale_pause_deadlock"
WORKER_STATUS_HITL_WAITING = "hitl_waiting"
WORKER_STATUS_HITL_TIMEOUT = "hitl_timeout"
WORKER_STATUS_API_CONTRACT_ERROR = "browser_api_contract_error"
WORKER_STATUS_PAGE_CRASHED = "page_crashed"
WORKER_STATUS_EXTRACTION_INCONCLUSIVE = "extraction_inconclusive"
WORKER_STATUS_STEP_BUDGET = "step_budget_exhausted"
WORKER_STATUS_UNKNOWN = "unknown"
WORKER_STATUS_FAILED = "failed"
WORKER_STATUS_CANCELLED = "cancelled"
WORKER_STATUS_RUNNING = "running"
WORKER_STATUS_SESSION_FLEET_LOST = "session_fleet_lost"
WORKER_STATUS_PAGE_CONTINUATION_LOST = "page_continuation_lost"
WORKER_STATUS_FLEET_ASSIGNMENT_LOST = "fleet_assignment_lost"

# Classifier priority (higher index = lower priority). The classifier walks this
# list and returns the first hard signal that matches. Soft (model-reported)
# status is only honored when no hard signal is present.
WORKER_STATUS_HARD_PRIORITY = (
    WORKER_STATUS_CONTEXT_LIMIT,
    WORKER_STATUS_SESSION_FLEET_LOST,
    WORKER_STATUS_PAGE_CONTINUATION_LOST,
    WORKER_STATUS_FLEET_ASSIGNMENT_LOST,
    WORKER_STATUS_STALE_PAUSE_DEADLOCK,
    WORKER_STATUS_PAGE_SETTLED_AFTER_HITL,
    WORKER_STATUS_HITL_WAITING,
    WORKER_STATUS_HITL_TIMEOUT,
    WORKER_STATUS_API_CONTRACT_ERROR,
    WORKER_STATUS_PAGE_CRASHED,
    WORKER_STATUS_EXTRACTION_INCONCLUSIVE,
    WORKER_STATUS_STEP_BUDGET,
)

# Categories that LeadAgent uses to pick a default reaction.
WORKER_STATUS_CATEGORY_DONE = "done"
WORKER_STATUS_CATEGORY_RECOVERABLE = "recoverable"
WORKER_STATUS_CATEGORY_NEEDS_HUMAN = "needs_human"
WORKER_STATUS_CATEGORY_FATAL = "fatal"
WORKER_STATUS_CATEGORY_UNKNOWN = "unknown"

WORKER_STATUS_CATEGORIES = {
    WORKER_STATUS_DONE: WORKER_STATUS_CATEGORY_DONE,
    WORKER_STATUS_PARTIAL: WORKER_STATUS_CATEGORY_DONE,
    WORKER_STATUS_INCOMPLETE: WORKER_STATUS_CATEGORY_RECOVERABLE,
    WORKER_STATUS_CONTEXT_LIMIT: WORKER_STATUS_CATEGORY_RECOVERABLE,
    WORKER_STATUS_BLOCKED_BY_CHALLENGE: WORKER_STATUS_CATEGORY_NEEDS_HUMAN,
    WORKER_STATUS_HITL_REQUIRED: WORKER_STATUS_CATEGORY_NEEDS_HUMAN,
    WORKER_STATUS_PAGE_SETTLED_AFTER_HITL: WORKER_STATUS_CATEGORY_NEEDS_HUMAN,
    WORKER_STATUS_STALE_PAUSE_DEADLOCK: WORKER_STATUS_CATEGORY_RECOVERABLE,
    WORKER_STATUS_STEP_BUDGET: WORKER_STATUS_CATEGORY_RECOVERABLE,
    WORKER_STATUS_PAGE_CRASHED: WORKER_STATUS_CATEGORY_RECOVERABLE,
    WORKER_STATUS_EXTRACTION_INCONCLUSIVE: WORKER_STATUS_CATEGORY_RECOVERABLE,
    WORKER_STATUS_HITL_WAITING: WORKER_STATUS_CATEGORY_NEEDS_HUMAN,
    WORKER_STATUS_HITL_TIMEOUT: WORKER_STATUS_CATEGORY_NEEDS_HUMAN,
    WORKER_STATUS_API_CONTRACT_ERROR: WORKER_STATUS_CATEGORY_FATAL,
    WORKER_STATUS_FAILED: WORKER_STATUS_CATEGORY_FATAL,
    WORKER_STATUS_CANCELLED: WORKER_STATUS_CATEGORY_FATAL,
    WORKER_STATUS_UNKNOWN: WORKER_STATUS_CATEGORY_UNKNOWN,
    WORKER_STATUS_RUNNING: WORKER_STATUS_CATEGORY_UNKNOWN,
    WORKER_STATUS_SESSION_FLEET_LOST: WORKER_STATUS_CATEGORY_NEEDS_HUMAN,
    WORKER_STATUS_PAGE_CONTINUATION_LOST: WORKER_STATUS_CATEGORY_NEEDS_HUMAN,
    WORKER_STATUS_FLEET_ASSIGNMENT_LOST: WORKER_STATUS_CATEGORY_RECOVERABLE,
}

# Soft statuses the model is allowed to self-report via final_answer.
# Anything outside this set is mapped to WORKER_STATUS_UNKNOWN at exit.
MODEL_ALLOWED_SOFT_STATUSES = frozenset({
    WORKER_STATUS_DONE,
    WORKER_STATUS_INCOMPLETE,
    WORKER_STATUS_PARTIAL,
    WORKER_STATUS_EXTRACTION_INCONCLUSIVE,
})

# --- Detection thresholds & markers ---
CONTEXT_LIMIT_ERROR_MARKERS = (
    "exceeded model token limit",
    "context length",
    "too many tokens",
    "input is too long",
)
API_CONTRACT_ERROR_MARKERS = (
    "method not found",
    "-32601",
    "requires a fleetid for routing",
    "proxied actions require",
)
API_CONTRACT_ERROR_THRESHOLD = 3

PAGE_CRASHED_LOOKBACK = 5
PAGE_CRASHED_FAIL_THRESHOLD = 3

EXTRACTION_METHODS = frozenset({
    "Runtime.evaluate",
    "DOM.getAXTree",
    "DOM.inspectSelect",
    "DOM.getText",
    "DOM.getSemanticTree",
})
EXTRACTION_FAILURE_OBS_MARKERS = (
    "timed out",
    "Result: null",
    "Result: undefined",
)
EXTRACTION_LOOKBACK = 10
EXTRACTION_FAIL_THRESHOLD = 5
