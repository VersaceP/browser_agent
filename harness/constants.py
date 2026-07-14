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
}
OFFLOAD_FIELDS_AS_TEXT = {"lines"}
OFFLOAD_FIELDS_AS_JSON = {
    "tree",
    "nodes",
    "ax",
    "text",
    "attributes",
    "value",
    "layers",
}
OFFLOAD_FIELDS = OFFLOAD_FIELDS_AS_TEXT | OFFLOAD_FIELDS_AS_JSON
SCREENSHOT_METHODS = {"Page.screenshot", "DOM.getElementScreenshot"}
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
RENDER_RECOVERY_METHODS = {"Page.getState", "Page.switchTo", "Page.navigate"}
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
    "Input.type",
    "Input.press",
    "Input.scroll",
    "Input.drag",
}
ANCHOR_PARAM_KEYS = {"selector", "nodeId", "toSelector", "toNodeId"}

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

# Classifier priority (higher index = lower priority). The classifier walks this
# list and returns the first hard signal that matches. Soft (model-reported)
# status is only honored when no hard signal is present.
WORKER_STATUS_HARD_PRIORITY = (
    WORKER_STATUS_CONTEXT_LIMIT,
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
