"""
harness.diagnostics.error_classification - Structured browser/tool error hints.
"""

from typing import Any, Optional

from harness.results.call_outcome import (
    domain_state_read_succeeded,
    public_action_failure,
)
from harness.constants import (
    API_CONTRACT_ERROR_MARKERS,
    PAGE_DEAD_OBSERVATION_MARKERS,
)
from harness.utils import JsonDict


# Keyboard-driven select contract (2026-09 platform generation).
#
# ONE declaration per public code, and every select-facing surface is a
# PROJECTION of it: the Input.select action map, the DOM.inspectSelect action
# map, the retry budget, the failure-receipt guidance, and whether a visual
# locate is honest advice for this code. Those five used to be five
# hand-maintained tables in three modules, pinned to each other by invariants
# of the form `set(A) == set(B)`. Such an invariant cannot fail when A and B
# are both wrong, which is exactly how a code set that was missing
# select-option-label-ambiguous stayed green. A projection cannot disagree
# with its source, so the only thing left to review is this table.
#
# `raised_by` is DECLARED, not inferred. Nothing in the receipt or the schema
# says which Action can produce which code, and a static scan of the platform
# bundle cannot answer it either (codes travel as parameters, native reasons
# are remapped by method, adapters dispatch dynamically). So it is written
# here from a read of the 1.1.91 call graph, dated and reviewable, rather than
# guessed by a generator whose output would be one more artifact to trust.
#
# `family` is the recovery LADDER shape. `visual_locate` is a separate
# question - "is the answer on screen?" - and deliberately not derived from
# the family: select-target-not-select is a terminal verdict for the select
# contract and simultaneously the case where looking at the page helps most.
#
# The platform deleted the whole select-agent-takeover-* family plus
# select-option-id-unavailable / select-option-id-proof-unavailable /
# select-popup-relation-changed / select-multiple-unsupported /
# select-control-kind-mismatch when it took over popup exploration itself.
# Entries for codes the connected build cannot emit are not harmless: they
# read as coverage the harness does not have, which is how Hitl.getTaskSummary
# / Hitl.resumeEvent survived in tool_policy long after their deletion.

SELECT_METHODS = frozenset({"DOM.inspectSelect", "Input.select"})

_BOTH = SELECT_METHODS
_SELECT_ONLY = frozenset({"Input.select"})

# Why no automatic replay, for EVERY select code. The public envelope states
# error/observation/suggested_prompt and explicitly does not say whether a side
# effect started, so "the failed attempt already sent keys" is an assertion the
# receipt cannot support - select-popup-ambiguous, for one, is raised while
# resolving the target, before any key is dispatched. The instruction is the
# same either way; the reason has to be the true one.
_NO_REPLAY = (
    "Do not replay the Action that just failed: it may already have opened or"
    " moved the control, and this receipt does not prove that it did not."
)

# The ladder for "the menu exists somewhere but nothing can name it". Ordered
# cheapest-and-most-durable first. VL enters only at step 3 and only LOCATES;
# the action stays an ordinary native call, chosen to fit the control.
_POPUP_DISCOVERY_LADDER = (
    " Recover in this order: (1) refresh DOM.getAXTree; (2) read a page-wide"
    " DOM.getSemanticTree and relate aria-controls / aria-owns /"
    " aria-activedescendant to a listbox/menu/option surface, which for a"
    " custom control is often rendered in a portal OUTSIDE the control's own"
    " subtree; (3) only when the target is visibly on screen and no structured"
    " surface can name it, call visual_verify mode=visual_locate - it locates,"
    " it never acts; (4) act on what step 2 or 3 returned using whichever"
    " ordinary Input.*/DOM.* method the control actually needs (a click, a"
    " keypress, typing to filter, a scroll to reveal - not necessarily"
    " Input.click), preferring a returned resolvedId over any coordinate;"
    " (5) re-observe and verify the outcome before the next step."
)


class SelectFailurePolicy:
    """One public select code and everything the harness does with it."""

    __slots__ = (
        "family", "raised_by", "action", "inspect_action",
        "retries", "visual_locate", "guidance",
    )

    def __init__(
        self,
        family,
        raised_by,
        action,
        retries,
        visual_locate,
        guidance,
        inspect_action="",
    ):
        self.family = family
        self.raised_by = frozenset(raised_by)
        self.action = action
        # "" means the inspect path routes exactly like the selection path.
        self.inspect_action = inspect_action or action
        self.retries = retries
        self.visual_locate = visual_locate
        self.guidance = guidance


SELECT_FAILURE_POLICY = {
    # --- popup_discovery: the control resolved, the menu did not ------------
    # A retry budget of 0 throughout: the menu binding is what failed, and a
    # second identical dispatch re-runs the same binding against a control the
    # first one may have moved.
    "select-popup-not-found": SelectFailurePolicy(
        family="popup_discovery",
        raised_by=_BOTH,
        action="reobserve_page_then_locate_menu_by_semantic_tree_then_visual_locate",
        retries=0,
        visual_locate=True,
        guidance=(
            "The select menu could not be bound through standard accessibility"
            " relationships. " + _NO_REPLAY + _POPUP_DISCOVERY_LADDER
        ),
    ),
    "select-popup-ambiguous": SelectFailurePolicy(
        family="popup_discovery",
        raised_by=_BOTH,
        action="disambiguate_control_and_menu_before_any_further_select",
        retries=0,
        visual_locate=True,
        guidance=(
            "More than one selectable control or menu matched this target."
            " The platform raises this while RESOLVING the target, so the page"
            " may well be untouched - but the receipt does not say so, and the"
            " recovery does not depend on the answer: name one control"
            " unambiguously before selecting again. " + _NO_REPLAY
            + _POPUP_DISCOVERY_LADDER
        ),
    ),
    "select-popup-not-ready": SelectFailurePolicy(
        family="popup_discovery",
        raised_by=_BOTH,
        action="observe_popup_with_ax_and_semantic_tree_then_one_bounded_generic_action",
        retries=0,
        visual_locate=True,
        guidance=(
            "The menu could not be observed as visible and stable, and was"
            " left in whatever state it reached. " + _NO_REPLAY
            + _POPUP_DISCOVERY_LADDER
        ),
    ),
    # --- option_evidence: enumeration WORKED, the request did not match -----
    #
    # Read this family off the raise sites, not off the receipt. The public
    # failure envelope carries error + observation + suggested_prompt and
    # nothing else: no select code declares `detailFields`, so a failure never
    # ships the option list. The earlier version of this comment claimed it
    # did, and built the whole family's visual verdict on that - a false
    # premise about the platform, pinned by tests that only compared harness
    # artifacts to each other.
    #
    # What is actually true of the three codes below is narrower and enough:
    # the platform SUCCEEDED in enumerating the menu and then found no match
    # (orchestrator resolveOneSelection: matches.length === 0 ->
    # not-in-current-window, > 1 -> label-ambiguous) or found the option
    # disabled. The option list lives in the last successful inspection the
    # model already holds, so the recovery is to re-inspect and use a returned
    # field. A visual locate cannot beat an enumeration that worked, which is
    # why these carry visual_locate=False - and why select-options-incomplete,
    # where enumeration FAILED, does not.
    "select-option-not-in-current-window": SelectFailurePolicy(
        family="option_evidence",
        raised_by=_BOTH,
        action="reinspect_current_window_then_retry_once_with_returned_fields",
        retries=1,
        visual_locate=False,
        guidance=(
            "The requested option is not among the ones the latest observation"
            " returned. Use only an id, exact label, or explicit value from"
            " that response, and when the walk was incomplete continue it with"
            " the startOption the platform named rather than starting over."
        ),
    ),
    "select-option-label-ambiguous": SelectFailurePolicy(
        family="option_evidence",
        raised_by=_BOTH,
        action="read_popup_semantic_tree_then_retry_once_with_a_unique_option_id",
        retries=1,
        visual_locate=False,
        guidance=(
            "The label matched more than one option. Re-inspect and continue"
            " from a unique option id returned by that inspection instead of"
            " the label."
        ),
    ),
    "select-option-disabled": SelectFailurePolicy(
        family="option_evidence",
        raised_by=_BOTH,
        action="stop_and_report_requested_option_unavailable",
        retries=0,
        visual_locate=False,
        guidance=(
            "The requested option is disabled. Choose an enabled option, or"
            " satisfy the page condition that enables it, before continuing."
            " Clicking it by any other route does not make it selectable."
        ),
    ),
    # The one option-family code where enumeration itself failed, so it is the
    # one that keeps the visual fallback. Its eleven raise sites cover three
    # different situations, and only the first is "your request was wrong":
    #   * the option list is empty or absent (orchestrator, element adapter);
    #   * the menu changed under the walk (relationId moved mid-walk);
    #   * an option node could not be PARSED at all - antd parseOption throws
    #     when a rendered option carries no id, no label, or no aria-selected.
    # That last one is the textbook structured blind spot: the menu is on
    # screen and the platform cannot name what is in it. Denying the hint here
    # would close the exact door this ladder exists to open. It is still last,
    # after AXTree and SemanticTree, and it still only LOCATES.
    "select-options-incomplete": SelectFailurePolicy(
        family="option_evidence",
        raised_by=_BOTH,
        action="reinspect_current_select_then_locate_options_by_semantic_tree_or_visually",
        retries=0,
        visual_locate=True,
        guidance=(
            "The platform could not establish the requested option and its"
            " state - the option list came back empty, changed under the walk,"
            " or an option node could not be parsed at all. " + _NO_REPLAY
            + " Inspect the select again first and continue as normal once the"
            " option and its state are present. If a fresh inspection still"
            " cannot enumerate them while the menu is plainly on screen, this"
            " is a structured blind spot rather than a wrong request:"
            + _POPUP_DISCOVERY_LADDER
        ),
    ),
    # --- control_unsupported: not a select at all ---------------------------
    # Terminal for the select contract, wide open for everything else. Only
    # native <select>, Ant Design and Element have adapters, so every other
    # custom widget arrives here, and reaching it is exactly what a visual
    # locate is for.
    "select-target-not-select": SelectFailurePolicy(
        family="control_unsupported",
        raised_by=_BOTH,
        action="stop_select_and_use_generic_input_actions",
        retries=0,
        visual_locate=True,
        guidance=(
            "This element is not an ABCP-supported select control (only native"
            " <select>, Ant Design and Element have adapters). Stop calling"
            " the select Actions for it and drive it as ordinary UI: enumerate"
            " fresh AXTree targets and act one verified step per visible"
            " level. A visible multi-column category/list browser is ordinary"
            " UI, not a broken select. If a level is visible but no structured"
            " surface names it, visual_verify mode=visual_locate can locate it;"
            " act on the id it returns with whichever ordinary Input.*/DOM.*"
            " method that level needs."
        ),
    ),
    # --- surface_unavailable: nothing to talk to right now ------------------
    # NOT "this control has no adapter" - that is select-target-not-select.
    # The platform also maps the native surface_unavailable / frame_unavailable
    # diagnostics onto this code for DOM.inspectSelect, so it can be a page
    # surface or frame that is momentarily gone. Routing it to "treat as
    # ordinary UI" would turn a transient infrastructure fault into a permanent
    # verdict about the control, and would contradict the platform's own
    # suggested_prompt, which the agent sees in the same envelope.
    # visual_locate=False: when the surface itself is unavailable there is
    # nothing trustworthy to photograph, and a screenshot that succeeds anyway
    # would be of a different surface than the one that failed.
    "select-capability-unavailable": SelectFailurePolicy(
        family="surface_unavailable",
        raised_by=_BOTH,
        action="reobserve_page_and_control_state_then_continue_only_when_available",
        retries=0,
        visual_locate=False,
        guidance=(
            "A required capability, page surface, or frame was unavailable, so"
            " the control could not be inspected or operated. This is not a"
            " verdict about the control: read Page.getState and a fresh"
            " DOM.getAXTree, and continue only once the control is observable"
            " again. Do not reclassify it as ordinary UI on this code alone -"
            " select-target-not-select is what says that."
        ),
    ),
    # --- contract_unproven: stop and establish reality ----------------------
    # These say the harness cannot know what happened. The recovery is
    # verification, never a corrective dispatch, and never a visual guess:
    # "no automatic solution" is not a reason to go clicking.
    "select-selection-mode-unknown": SelectFailurePolicy(
        family="contract_unproven",
        raised_by=_BOTH,
        action="stop_and_report_platform_select_contract_failure",
        retries=0,
        visual_locate=False,
        guidance=(
            "The platform could not determine the selection mode. This is an"
            " ABCP select contract failure: report it with this receipt rather"
            " than working around it."
        ),
    ),
    # Only restoreSelectionMenu passes this code, and only Input.select calls
    # it: an inspect restores with a fire-and-forget Escape that raises
    # nothing. So it is Input.select-only, and no inspect projection is built.
    "select-state-restore-failed": SelectFailurePolicy(
        family="contract_unproven",
        raised_by=_SELECT_ONLY,
        action="reinspect_before_continuing",
        retries=0,
        visual_locate=False,
        guidance=(
            "The selection was made but the menu could not be restored to a"
            " known state. Inspect the control again before continuing; do not"
            " assume the menu is closed."
        ),
    ),
    "select-final-state-unproven": SelectFailurePolicy(
        family="contract_unproven",
        raised_by=_SELECT_ONLY,
        action="inspect_control_before_any_correction",
        retries=0,
        visual_locate=False,
        guidance=(
            "The selection was recorded during the keyboard operation but the"
            " final control state could not be proven. Read the control's own"
            " value (inspect it, or DOM.getAttribute) BEFORE issuing any"
            " correction - a corrective selection against an unknown state can"
            " undo a selection that in fact succeeded."
        ),
    ),
}


# --- Projections. Never edit these; edit SELECT_FAILURE_POLICY. -------------

SELECT_FAILURE_ACTIONS = {
    code: policy.action for code, policy in SELECT_FAILURE_POLICY.items()
}

# An inspect failure describes the menu binding or the control itself, so a few
# codes route differently from a selection - but the KEY SET is now derived
# from `raised_by` instead of maintained by hand, which is what let phantom
# entries for select-popup-relation-changed and select-option-id-unavailable
# outlive their deletion from the platform.
INSPECT_SELECT_FAILURE_ACTIONS = {
    code: policy.inspect_action
    for code, policy in SELECT_FAILURE_POLICY.items()
    if "DOM.inspectSelect" in policy.raised_by
}

SELECT_FAILURE_RETRY_LIMITS = {
    code: policy.retries for code, policy in SELECT_FAILURE_POLICY.items()
}

SELECT_FAILURE_GUIDANCE = {
    code: policy.guidance for code, policy in SELECT_FAILURE_POLICY.items()
}


def select_failure_family(code):
    """The recovery-ladder family for one public select code, or ""."""
    policy = SELECT_FAILURE_POLICY.get(str(code or ""))
    return policy.family if policy is not None else ""


def select_code_is_declared_for_method(code, method):
    """Whether `raised_by` claims this Action can produce this code.

    False is a claim about the HARNESS, not about the page: either the 1.1.91
    call graph was read wrong here, or the platform changed. Both are worth
    seeing, and both are invisible if the classifier just routes the code the
    usual way - the one signal that could falsify this table gets consumed.

    An unknown code answers True: `raised_by` only speaks about codes it
    declares, and treating silence as a mismatch would flag every new platform
    code as harness drift.
    """
    policy = SELECT_FAILURE_POLICY.get(str(code or ""))
    if policy is None:
        return True
    name = str(method or "")
    if name not in SELECT_METHODS:
        return True
    return name in policy.raised_by


def select_failure_visual_locate_useful(code):
    """Whether a visual locate is honest advice for this select failure.

    Unknown codes answer True: a code the harness has never seen is not
    evidence that looking at the page is pointless, and the denylist in
    harness.vl.arbiter is built on the same default.
    """
    policy = SELECT_FAILURE_POLICY.get(str(code or ""))
    return True if policy is None else policy.visual_locate


# --- Structured public failure classification -------------------------------
#
# Every ABCP failure now arrives as a stable public ``error.code``. The private
# ``runtime`` block (code/phase/sideEffectStarted/actionKind) is stripped at the
# transport boundary, so there is nothing else to read; prose matching survives
# only for the few harness-generated errors that never had a code.
#
# The code enum is 263 entries and grows with the platform, so it is NOT
# transcribed here. Only codes that change what the harness DOES get an entry;
# everything else is classified by its family (prefix/suffix), which is a
# property of the naming contract rather than of any one code. An unrecognized
# code still produces a structured classification carrying the code verbatim,
# which is strictly more actionable than the "unknown" prose matching returns.

_RUNTIME_CODE_TYPES = {
    # ABCP publishes two occlusion codes and the harness has to map both:
    # `occlusion_blocked` is what arms the automatic dismiss_overlay recovery in
    # tools/browser_tools/auto_intercept.py, and a code that misses this table
    # falls through to the generic family rule, so the recovery silently never
    # fires. `target-occluded` is the one Input.click actually reports.
    "occluded": ("occlusion_blocked", "refresh_dom_dismiss_overlay_then_retry_once"),
    "target-occluded": ("occlusion_blocked", "refresh_dom_dismiss_overlay_then_retry_once"),
    "renderer-lost": ("render_lost", "rebuild_page_in_same_fleet_then_refresh_targets"),
    "input-host-destroyed": ("render_lost", "rebuild_page_in_same_fleet_then_refresh_targets"),
    "stale-target": ("stale_target", "refresh_ax_tree_then_retarget_once"),
    "target-not-found": ("target_not_found", "refresh_ax_tree_then_retarget_once"),
    "scroll-target-not-found": ("target_not_found", "refresh_ax_tree_then_retarget_once"),
    "scroll-container-not-found": ("target_not_found", "refresh_ax_tree_then_retarget_once"),
    "target-frame-not-found": ("target_frame_not_found", "refresh_ax_tree_then_retarget_once"),
    "invalid-input": ("contract_error", "switch_method_or_report_platform_contract_bug"),
    "invalid-selector": ("contract_error", "switch_method_or_report_platform_contract_bug"),
    "selector-matched-multiple-elements": (
        "target_ambiguous", "narrow_the_selector_or_use_a_canonical_id",
    ),
    "coordinate-conversion-failed": (
        "coordinate_unavailable", "stop_using_coordinates_and_target_by_id_or_selector",
    ),
    # Not every drag-* code is a same-document mistake. These two describe an
    # endpoint that went away or a destination that cannot be pinned down, and
    # "keep both endpoints in one document" would be useless advice for them.
    "drag-source-frame-unavailable": (
        "drag_endpoint_lost", "refresh_ax_tree_then_retarget_once",
    ),
    "drag-destination-frame-ambiguous": (
        "drag_endpoint_ambiguous",
        "name_the_destination_with_a_canonical_id_from_the_source_frame",
    ),
}

# Family rules, applied in order when no exact entry matched. Each is a
# statement about the naming contract: drag endpoint errors are unsupported
# geometry rather than transient, a scroll code means the scroll request itself
# was wrong, and anything ending in -timeout timed out.
_RUNTIME_CODE_FAMILIES = (
    ("cross-frame-drag", "drag_unsupported", "stop_and_keep_both_endpoints_in_one_document"),
    ("cross-document-drag", "drag_unsupported", "stop_and_keep_both_endpoints_in_one_document"),
    ("drag-", "drag_unsupported", "stop_and_keep_both_endpoints_in_one_document"),
    # Unknown select codes default to guidance, not an automatic Input.select
    # replay: on this contract generation a failed select may already have
    # moved the popup, and the platform's own prompts never say "just retry".
    ("select-", "select_failure", "reinspect_select_then_follow_returned_guidance"),
    ("scroll-", "scroll_failed", "inspect_viewport_then_correct_the_scroll_request"),
    ("input-", "input_surface_unavailable", "inspect_page_state_before_retrying_input"),
    ("semantic-tree-", "semantic_tree_unavailable", "reinspect_page_state_then_retry_semantic_tree"),
)

_TIMEOUT_SUFFIX = "-timeout"

# The verdict the classifier reaches when NOTHING matched: it restates "read the
# state and do what the platform said" and carries no information the platform's
# own `suggested_prompt` does not already give the model. Every public code ships
# a prompt, so on the model-facing projection this is duplication, not guidance.
# It stays in the internal classification (spawner status, compaction and
# auto-intercept all read `errorClassification`) and is hidden only from the
# model, and only when a platform prompt is present.
GENERIC_PUBLIC_FALLBACK_ACTION = "inspect_page_state_then_follow_platform_guidance"
# These are client-generated transport codes, not ABCP action-runtime codes.
# Keep them separate from the platform's ``runtime.code`` taxonomy: a socket
# that has already lost its reader cannot be recovered by another browser tool
# call, while an RPC action failure can often be handled by the normal action
# recovery policies above.
_TRANSPORT_ERROR_TYPES = {
    "ABCP_TRANSPORT_CONNECT_FAILED": (
        "transport_connection_lost",
        "reconnect_browser_transport_before_rescheduling",
    ),
    "ABCP_TRANSPORT_NOT_CONNECTED": (
        "transport_connection_lost",
        "reconnect_browser_transport_before_rescheduling",
    ),
    "ABCP_TRANSPORT_CLOSED": (
        "transport_connection_lost",
        "reconnect_browser_transport_before_rescheduling",
    ),
    "ABCP_TRANSPORT_READER_FAILED": (
        "transport_connection_lost",
        "reconnect_browser_transport_before_rescheduling",
    ),
    "ABCP_TRANSPORT_SEND_FAILED": (
        "transport_connection_lost",
        "reconnect_browser_transport_before_rescheduling",
    ),
    "ABCP_TRANSPORT_CALL_TIMEOUT": (
        "transport_timeout",
        "treat_action_outcome_as_unknown_and_do_not_replay_automatically",
    ),
    "ABCP_RPC_ERROR": (
        "rpc_error",
        "follow_method_specific_error_guidance",
    ),
}



def select_failure_action(code: str, method: str = "") -> Optional[str]:
    """The select recovery action for one public code, honouring the method.

    DOM.inspectSelect keeps its own routing - reading the popup is the right
    next move for an inspect and not for a selection - but it is a VIEW over
    the same catalog, never a second catalog. Routing lived only in the prose
    fallback for a while, so the live structured path silently served the
    Input.select action for every inspect failure.
    """
    if method == "DOM.inspectSelect" and code in INSPECT_SELECT_FAILURE_ACTIONS:
        return INSPECT_SELECT_FAILURE_ACTIONS[code]
    # Deliberate fallback, not an oversight. A code arriving on a method that
    # `raised_by` says cannot raise it still gets real advice, because the
    # model is mid-task and the recovery for that code is very likely still the
    # right one. What must NOT happen is the combination passing unremarked -
    # that is reported as contractDrift by the callers below, so the mismatch
    # is visible on the receipt and in the log instead of being swallowed here.
    return SELECT_FAILURE_ACTIONS.get(code)


def _mark_select_contract_drift(classification, code, method):
    """Flag a select code arriving on an Action that should not raise it.

    Advisory by design: the recovery action is left alone (see
    select_failure_action) so a live task is not stranded on a harness
    bookkeeping error. What this adds is visibility - the receipt says the
    declaration and the platform disagree, and the reader is told not to trust
    `raised_by` for this code until one of them is corrected.
    """
    if select_code_is_declared_for_method(code, method):
        return
    classification["contractDrift"] = "select_method_code_mismatch"
    classification["contractDriftDetail"] = (
        f"{method} returned {code}, which this harness declares as raised only"
        f" by {'/'.join(sorted(SELECT_FAILURE_POLICY[code].raised_by))}."
        " Either the declared call graph is stale or the platform changed."
        " The recovery below is the catalog's and is still worth following;"
        " report the mismatch."
    )


def classify_public_action_failure(
    failure: Any,
    *,
    method: str = "",
) -> Optional[JsonDict]:
    """Classify the stable public failure envelope without private metadata."""
    if not isinstance(failure, dict):
        return None
    code = str(failure.get("code") or "").strip()
    if not code:
        return None
    method_name = str(method or failure.get("method") or "")
    mapped = _RUNTIME_CODE_TYPES.get(code)
    if mapped is None:
        select_action = select_failure_action(code, method_name)
        if select_action is not None:
            mapped = ("select_failure", select_action)
    if mapped is None:
        for prefix, error_type, action in _RUNTIME_CODE_FAMILIES:
            if code.startswith(prefix):
                mapped = (error_type, action)
                break
    if mapped is None and code.endswith(_TIMEOUT_SUFFIX):
        mapped = ("timeout", "retry_with_backoff_or_reduce_surface")
    if mapped is None:
        mapped = ("action_failure", GENERIC_PUBLIC_FALLBACK_ACTION)
    error_type, suggested_action = mapped
    classification: JsonDict = {
        "type": error_type,
        "errorCode": code,
        "suggested_action": suggested_action,
        "method": method_name,
        "source": "public_action_failure",
        # With private sideEffectStarted removed, replay safety is unknown.
        "replayForbidden": True,
    }
    _mark_select_contract_drift(classification, code, method_name)
    platform_prompt = str(failure.get("suggested_prompt") or "").strip()
    if platform_prompt:
        classification["platformSuggestedPrompt"] = platform_prompt[:1000]
    return classification


def classify_browser_error(
    error_text: Any,
    *,
    method: str = "",
) -> JsonDict:
    """Classify an error string without replacing the original `error` field.

    HITL/paused signals intentionally win over render/page-dead markers when a
    message contains both. Treating human-intervention state as the primary
    blocker prevents the agent from issuing more browser actions while the page
    may still be gated by a user-visible challenge.
    """
    text = str(error_text or "")
    lower = text.lower()
    method_name = str(method or "")

    if (
        method_name == "DOM.getAXTree"
        and "nodecount=" in lower
        and "no parseable axtree nodes" in lower
    ):
        return {
            "type": "axtree_data_inconsistent",
            "errorCode": "axtree_node_count_parse_mismatch",
            "suggested_action": (
                "report_platform_axtree_data_error_and_do_not_reuse_prior_ids"
            ),
            "method": method_name,
        }

    if method_name == "DOM.inspectSelect":
        # Scan the full catalog, not just the inspect view: a code the inspect
        # view does not override still routes through the shared table rather
        # than falling out to the generic fallback. select_failure_action is
        # the single routing rule the structured path uses too.
        for error_code in SELECT_FAILURE_ACTIONS:
            if error_code in lower:
                classified = {
                    "type": error_code.replace("-", "_"),
                    "errorCode": error_code,
                    "suggested_action": select_failure_action(
                        error_code, method_name
                    ),
                    "method": method_name,
                }
                # This is the branch where drift is most likely to appear -
                # `raised_by` excludes two codes from inspect, and this is the
                # path an inspect failure takes - and it was the one branch of
                # four that did not report it.
                _mark_select_contract_drift(classified, error_code, method_name)
                return classified
        # Some platform builds collapse an inspect implementation failure to
        # the bare JSON-RPC envelope, with no public select-* reason code.
        # This is not an unknown application error: repeating the same inspect
        # cannot add information and a successful generic input receipt alone
        # does not prove that a popup ever opened.
        if (
            "-32005" in lower
            and "action dom.inspectselect failed" in lower
        ):
            return {
                "type": "inspect_select_platform_failure",
                "errorCode": "inspect-select-platform-action-failed",
                "suggested_action": (
                    "reobserve_control_then_use_one_generic_input_action"
                    "_with_visible_popup_proof"
                ),
                "method": method_name,
            }

    if method_name == "Input.select":
        for error_code in SELECT_FAILURE_ACTIONS:
            if error_code in lower:
                classified = {
                    "type": error_code.replace("-", "_"),
                    "errorCode": error_code,
                    "suggested_action": select_failure_action(
                        error_code, method_name
                    ),
                    "method": method_name,
                }
                _mark_select_contract_drift(classified, error_code, method_name)
                return classified
        # Family fallback mirrors the runtime classifier: an unrecognized
        # select code still yields a structured classification carrying the
        # code verbatim, and never recommends an automatic Input.select
        # replay (a failed select may already have moved the popup).
        import re as _re

        match = _re.search(r"(select-[a-z]+(?:-[a-z]+)*)", lower)
        if match:
            return {
                "type": "select_failure",
                "errorCode": match.group(1),
                "suggested_action": (
                    "reinspect_select_then_follow_returned_guidance"
                ),
                "method": method_name,
            }

    if (
        method_name == "Page.create"
        and "-32005" in lower
        and "page.create" in lower
    ):
        return {
            "type": "page_create_failed",
            "suggested_action": "probe_existing_pages_then_reuse_or_abort_worker",
            "method": method_name,
        }
    if _contains(lower, "err_page_paused", "paused for human intervention"):
        return {
            "type": "hitl_paused_state",
            "suggested_action": "wait_for_explicit_hitl_resume_or_quarantine_stale_page",
            "method": method_name,
        }
    if _contains(
        lower,
        "mouse action blocked",
        "target element is occluded",
        "element is occluded",
        "is occluded",
    ):
        return {
            "type": "occlusion_blocked",
            "suggested_action": "refresh_dom_dismiss_overlay_then_retry_once",
            "method": method_name,
        }
    if _contains(lower, "err_render_lost"):
        return {
            "type": "render_lost",
            "suggested_action": "rebuild_page_in_same_fleet_then_refresh_targets",
            "method": method_name,
        }
    if _contains_any(text, PAGE_DEAD_OBSERVATION_MARKERS):
        return {
            "type": "page_crashed",
            "suggested_action": "rebuild_fleet_or_open_fresh_page",
            "method": method_name,
        }
    if _contains(lower, "err_timeout", "timeout", "timed out"):
        return {
            "type": "timeout",
            "suggested_action": "retry_with_backoff_or_reduce_surface",
            "method": method_name,
        }
    if _contains_any(lower, API_CONTRACT_ERROR_MARKERS):
        return {
            "type": "contract_error",
            "suggested_action": "switch_method_or_report_platform_contract_bug",
            "method": method_name,
        }
    return {
        "type": "unknown",
        "suggested_action": "report_to_lead_with_context",
        "method": method_name,
    }


def attach_error_classification(result: JsonDict, *, method: str = "") -> JsonDict:
    """Mutate and return result with `errorClassification` when an error exists.

    Structured first: when the platform stated a public error code, that is the
    verdict. HITL/pause is the one exception that still wins over it — a paused
    page blocks every further action regardless of which code the interrupted
    one reported, and treating it as an ordinary action failure would send the
    worker back to clicking a gated page.
    """
    if isinstance(result.get("errorClassification"), dict):
        return result
    transport_code = str(result.get("transportCode") or "").strip()
    # A server-side JSON-RPC failure still travels through
    # ABCPTransportError, but its action/runtime payload is more specific
    # than this transport wrapper. Keep the wrapper code for audit only and
    # continue into the established method-specific classifier below.
    if transport_code and transport_code != "ABCP_RPC_ERROR":
        error_type, suggested_action = _TRANSPORT_ERROR_TYPES.get(
            transport_code,
            ("transport_error", "report_transport_diagnostics_to_lead"),
        )
        classification: JsonDict = {
            "type": error_type,
            "errorCode": transport_code,
            "suggested_action": suggested_action,
            "method": str(method or result.get("method") or ""),
            "source": "abcp_transport",
        }
        if result.get("exceptionType"):
            classification["exceptionType"] = str(result["exceptionType"])
        if result.get("connectionFatal") is True:
            classification["connectionFatal"] = True
        if isinstance(result.get("requestSent"), bool):
            classification["requestSent"] = result["requestSent"]
        result["errorClassification"] = classification
        return result
    message = _extract_error_message(result)
    public_failure = public_action_failure(result)
    if message and _contains(
        message.lower(), "err_page_paused", "paused for human intervention"
    ):
        result["errorClassification"] = classify_browser_error(message, method=method)
        return result
    # `public_action_failure` is the ONLY path here that reads
    # `response.data.error` — `_extract_error_message` above deliberately does
    # not — and for a DOMAIN-ERROR READ that field is the PAGE's own
    # last-navigation error, which never clears on a risk-controlled page.
    # Every successful Page.getState of such a page was therefore labelled
    # `action_failure` with `replayForbidden`, and the model-facing projection
    # handed that verdict straight to the worker: "your read failed, inspect
    # the page state" — answered by a read that produced the same verdict.
    #
    # The exemption sits HERE, not at the top of the function, so that every
    # higher-priority signal is decided first: a transport failure above, and
    # in particular the ERR_PAGE_PAUSED branch, which must win over any code
    # because a paused page blocks every further action. Guarding the whole
    # function instead dropped a pause that arrived through `observation` on a
    # page whose data looked healthy.
    #
    # BOTH conditions in the predicate are load-bearing. The method set alone
    # would swallow a genuinely failed Page.getState; the call verdict alone
    # would swallow a failed ACTION whose only signal is a bare `data.error`,
    # which is how Runtime.evaluate reports (see
    # tools/browser_tools/runtime_eval._runtime_evaluation_error_text).
    #
    # Nothing is hidden from the model: the page's own error stays in
    # `response.data.error` where the platform put it. What stops is the
    # harness relabelling it as a failure of the call that read it.
    structured = (
        None if domain_state_read_succeeded(result, method)
        else classify_public_action_failure(public_failure, method=method)
    )
    if structured is not None:
        result["errorClassification"] = structured
        return result
    unknown_method = _unknown_method_classification(result, method=method)
    if unknown_method is not None:
        result["errorClassification"] = unknown_method
        return result
    if not message:
        return result
    result["errorClassification"] = classify_browser_error(message, method=method)
    return result


# ABCP's WebSocket transport — the one the harness uses — answers an unknown
# method with a bare `{"code": -32601, "message": "Action failed"}`: no `data`,
# no public code, no observation, no suggested_prompt. (Its MCP transport does
# return a full envelope; that path is not this one.) This is the only failure
# shape on 1.1.9 where the platform supplies no guidance at all, so it is the
# only one where the harness must author its own. Verified live against
# catalogRevision sha256:cfd8fb90….
_UNKNOWN_METHOD_RPC_CODE = -32601


def _unknown_method_classification(
    result: JsonDict, *, method: str = ""
) -> Optional[JsonDict]:
    if result.get("rpcCode") != _UNKNOWN_METHOD_RPC_CODE:
        return None
    if public_action_failure(result):
        # A build that does supply a public envelope owns the guidance.
        return None
    return {
        "type": "method_not_found",
        "errorCode": "harness:method-not-found",
        "suggested_action": "refresh_capabilities_and_use_a_current_method",
        "method": str(method or result.get("method") or ""),
        # `source` separates this from a platform verdict. The projection uses
        # it to decide that this suggestion is the harness's own and must be
        # shown rather than hidden behind a (nonexistent) platform prompt.
        "source": "harness_fallback",
        "next_instruction": (
            "The transport rejected this method name as unknown and returned no"
            " platform guidance. Call System.getCapabilities to read the"
            " current callable catalog and use a method it returns; do not"
            " guess a name, reuse a removed one, or retry this call unchanged."
        ),
    }


def _extract_error_message(result: JsonDict) -> Optional[str]:
    direct = result.get("error")
    rpc_data = result.get("rpcData")
    if direct:
        rendered = _stringify_error(direct)
        if rpc_data is not None:
            rendered = f"{rendered} {_stringify_error(rpc_data)}"
        return rendered
    if rpc_data is not None:
        return _stringify_error(rpc_data)
    response = result.get("response")
    if isinstance(response, dict):
        if response.get("error"):
            return _stringify_error(response.get("error"))
        observation = response.get("observation")
        if isinstance(observation, str) and _looks_like_error(observation):
            return observation
    return None


def _stringify_error(value: Any) -> str:
    if isinstance(value, dict):
        parts = [
            str(value.get(key))
            for key in ("code", "message", "data", "error")
            if value.get(key) is not None
        ]
        return " ".join(parts) if parts else str(value)
    return str(value)


def _looks_like_error(text: str) -> bool:
    lower = text.lower()
    return any(
        marker in lower
        for marker in (
            "err_",
            "error",
            "timed out",
            "timeout",
            "paused for human",
            "occluded",
            "mouse action blocked",
            "method not found",
        )
    )


def _contains(text: str, *needles: str) -> bool:
    return any(needle.lower() in text for needle in needles)


def _contains_any(text: str, markers: Any) -> bool:
    lower = text.lower()
    return any(str(marker).lower() in lower for marker in markers)
