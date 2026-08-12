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
SCREENSHOT_METHODS = {"Page.screenshot", "DOM.getElementScreenshot"}

# Fleet-routing outcomes that can reach LeadAgent. Keep the guidance text and
# this catalog together so tests can mechanically reject undocumented additions.
LEAD_FLEET_ROUTING_DECISION_CODES = (
    "session_fleet_lost",
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

LEAD_FLEET_ROUTING_DECISION_GUIDANCE = """- session_fleet_lost: the named fleet disappeared from the owner inventory and is effectively terminal until explicit reset/re-authentication. Mark the auth session stale and follow the auth-interrupt/login recovery flow; never retry or silently rebind the same session_key.
- fleet_assignment_lost: stop the worker and request a fresh coordinator assignment; do not retry calls against the lost fleetId.
- fleet_auth_gated: another worker is resolving login/CAPTCHA for the shared fleet. Wait; do not create another fleet or continue account actions.
  - reasonKind=fleet_auth_resolver_required: the gate is closed but currently has no resolver. Spawn or continue exactly one worker on the same fleet/session to refresh Page.getState and DOM.getAXTree, then explicitly call Hitl.requestPause to claim resolution. Do not create another fleet and do not wait without assigning a resolver.
- fleet_reperception_required: shared auth state changed. The worker must call Page.getState and DOM.getAXTree before any further action.

Fleet routing rejection table for spawn_browser_agent:
- session_transport_unavailable: the original owner socket could not be restored; retry later without changing the session/fleet binding.
- session_manual_reset_required: repeated owner-socket recovery failed. Stop retrying. A host/operator must restore the transport or explicitly reset the exact fleet/generation; the Lead must never release or silently rebind it.
- session_slot_busy: when multi-worker fleet reuse is disabled, wait for the worker using that named session; never route the session to another fleet.
- fleet_owner_unavailable: wait for the owner slot to reconnect; do not replace the task/session fleet.
- fleet_reference_invalid: copy an existing Fleet UUID or a hexadecimal UUID prefix of at least eight characters into fleet_id; never put it in session_key.
- fleet_reference_not_found: refresh the authoritative Fleet inventory or ask the user for the current Fleet; never create a replacement.
- ambiguous_fleet_reference: use a longer Fleet UUID prefix that uniquely identifies one existing Fleet.
- reuse_fleet_lost: drop reuse_from_worker_id and request a fresh coordinator assignment.
- reuse_session_conflict: keep the source worker's session_key, or start a fresh named session without inheriting that worker.
- session_isolation_conflict: start a fresh fleet whose needs_isolated_session contract matches the request.
- fleet_routing_conflict: session_key, preferred_slot_id, and reuse_from_worker_id disagree; remove the conflicting selectors instead of retrying them unchanged.
- session_binding_conflict / fleet_session_conflict / released_fleet_conflict: fail closed and follow next_instruction; these protect an existing or released cookie jar from reassignment.
- task_fleet_limit_reached: the task already occupies runtime_limits.max_task_fleets fleets and this spawn cannot be served from them — it demanded a separate identity (needs_isolated_session or a new session_key), or every task fleet is bound to a named session and none may be lent to a generic worker. An ordinary fleetless spawn is normally NOT rejected here; it silently reuses a task fleet. Waiting does not clear this rejection: the harness never closes a fleet, so a finished worker still holds its own, and a named session stays bound after its worker ends. Continue on a fleet the task already has (drop needs_isolated_session, or pass the exact session_key already bound to it), release a session binding through the auth-recovery flow, or ask the operator to raise harness.max_task_fleets. Never retry it as a fresh fleet.
"""

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
    "errorClassification",
    "taskId",
)
GENERIC_TOOL_RESULT_RESPONSE_KEEP_KEYS = (
    "observation",
    "suggested_prompt",
    "error",
    "errorClassification",
    "taskId",
)
GENERIC_TOOL_RESULT_KEEP_FIELD_BYTES = 2000

RENDER_LOST_MARKERS = (
    "No RenderWidgetHostView",
    "No WebContents",
)

# Broader set used by diagnostics.classify_terminal_status for page_crashed
# detection. Superset of RENDER_LOST_MARKERS — render_recovery.py keeps using
# the narrower set for its active-recovery decision (transient WebContents
# detach), while the classifier here accepts any signal that the page is
# functionally dead. Once the notification hub lands (PR #3), the
# System.notification page_crashed / page_load_failed events should also feed
# into the same diagnostic, but for now we read what's already in transport
# observations.
PAGE_DEAD_OBSERVATION_MARKERS = RENDER_LOST_MARKERS + (
    "status=crashed",
    "page_crashed",
    "page_load_failed",
    "Page crashed",
    "Renderer crashed",
)
RENDER_RECOVERY_WINDOW_SECONDS = 30.0
RENDER_RECOVERY_METHODS = {
    "Page.getState",
    "Page.switchTo",
    "Page.navigate",
    "Page.reload",
    "Page.go",
}
READ_METHODS_RETRY_AFTER_NAVIGATE = {
    "Page.screenshot",
    "DOM.getAXTree",
    "DOM.getSemanticTree",
    "DOM.getText",
    "DOM.getAttribute",
    "DOM.getElementScreenshot",
}
ACTION_METHODS = {
    "Input.click",
    "Input.select",
    "Input.type",
    "Input.press",
    "Input.scroll",
    "Input.drag",
}
ANCHOR_PARAM_KEYS = {"selector", "id", "nodeId", "toSelector", "toNodeId"}

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
WORKER_STATUS_FLEET_ASSIGNMENT_LOST = "fleet_assignment_lost"

# Classifier priority (higher index = lower priority). The classifier walks this
# list and returns the first hard signal that matches. Soft (model-reported)
# status is only honored when no hard signal is present.
WORKER_STATUS_HARD_PRIORITY = (
    WORKER_STATUS_CONTEXT_LIMIT,
    WORKER_STATUS_SESSION_FLEET_LOST,
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
    WORKER_STATUS_FLEET_ASSIGNMENT_LOST: WORKER_STATUS_CATEGORY_RECOVERABLE,
}

# Soft statuses the model is allowed to self-report via final_answer.
# Anything outside this set is mapped to WORKER_STATUS_UNKNOWN at exit.
MODEL_ALLOWED_SOFT_STATUSES = frozenset({
    WORKER_STATUS_DONE,
    WORKER_STATUS_INCOMPLETE,
    WORKER_STATUS_PARTIAL,
    WORKER_STATUS_EXTRACTION_INCONCLUSIVE,
    WORKER_STATUS_BLOCKED_BY_CHALLENGE,
    WORKER_STATUS_HITL_REQUIRED,
    WORKER_STATUS_PAGE_SETTLED_AFTER_HITL,
    WORKER_STATUS_STALE_PAUSE_DEADLOCK,
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
