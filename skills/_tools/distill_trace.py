#!/usr/bin/env python3
"""Distill a successful BrowserAgent trace (traces/<worker>.jsonl) into a draft
skill workflow.json + SKILL.md/fallback.yaml skeleton + distill_report.md.

Rules: see skills/DISTILLATION.md. Automates the mechanical 80%; the draft must
be human-reviewed (confirm labels, pin live selectors, drop redundant steps)
before freezing. Trace event schema per harness/diagnostics/judge_trace.py.

Usage:
    python3 skills/_tools/distill_trace.py <trace.jsonl> --slug <task-slug> [--out skills/<slug>]
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path
from typing import Any, Optional

ID_RE = r"[0-9a-fA-F-]+:\d+:\d+"          # canonical AXTree id (matches-guard value)
# trace methods that are recovery/probe/tab-mgmt noise — never become workflow steps
DROP_METHODS = {
    "System.describeAction", "System.describeEvent", "System.getCapabilities",
    "Page.getState", "Page.switchTo", "Page.create",
    "DOM.getAXTree", "DOM.getSemanticTree",   # locate-support; scaffold re-emits its own
}
# verbs stripped from purpose when guessing a label, and stopwords that end a label
_VERBS = r"(?:click|open|get|read|select|press|reveal|load|navigate|refresh|check|inspect|dismiss|expand|fetch)"
_STOPS = r"(?:tab|tabs|button|section|link|content|text|panel|heading|page|overlay|field|menu|item|for|to|via|after|of|on)"


def load_events(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def is_failure(ev: dict) -> bool:
    res = ev.get("result") or {}
    if res.get("error"):
        return True
    obs = ((res.get("response") or {}).get("observation") or "")
    return "failed" in obs.lower() and "Action" in obs


def label_from_purpose(purpose: str) -> Optional[str]:
    if not purpose:
        return None
    m = re.search(_VERBS + r"\s+(.+?)\s+" + _STOPS + r"\b", purpose, re.IGNORECASE)
    if not m:
        return None
    label = m.group(1).strip()
    # reject degenerate captures
    if not label or len(label) > 40 or label.lower() in {"the", "a", "all"}:
        return None
    return label


def slugify(label: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", label)
    if not words:
        return "target"
    head, *rest = words
    return head.lower() + "".join(w.capitalize() for w in rest)


def keep_params(params: dict) -> dict:
    """Strip runtime handles + purpose (promoted to step-level)."""
    return {k: v for k, v in (params or {}).items() if k not in ("pageId", "fleetId", "purpose")}


def eval_result_keys(ev: dict) -> list[str]:
    """If a Runtime.evaluate returned an object, return its top-level string-ish
    keys (used to synthesize an extract map). The raw browser_call records the
    returned value under result.response.data (workflow extract paths have no
    'data.' prefix, so the synthesized extract maps key -> key)."""
    res = ev.get("result") or {}
    data = ((res.get("response") or {}) if isinstance(res, dict) else {}).get("data")
    if isinstance(data, dict):
        return [str(k) for k in data.keys()
                if isinstance(data.get(k), (str, int, float)) or data.get(k) is None]
    return []


def locate_scaffold(label: str, slug: str, inner_action: dict) -> list[dict]:
    """getAXTree -> transform(find label -> id) -> if matches id -> inner_action(id=$vars.<slug>Id)."""
    var = f"{slug}Id"
    return [
        {"action": "DOM.getAXTree",
         "purpose": f"Read structure to resolve the {label!r} target id at runtime"},
        {"type": "transform",
         "input": "$cache.axTree.lines",
         "ops": [
             {"op": "find", "pattern": f'"{label}"', "mode": "contains"},
             {"op": "regex", "pattern": r"\[(" + ID_RE + r")\]", "group": 1},
         ],
         "output": var},
        {"type": "if",
         "condition": {"path": f"$vars.{var}", "operator": "matches", "value": ID_RE},
         "then": [inner_action]},
    ]


def distill(events: list[dict]) -> tuple[list[dict], list[str], list[str], dict]:
    notes: list[str] = []
    persist: list[str] = []
    variables: dict[str, str] = {}

    calls = [e for e in events if e.get("type") == "browser_call"]
    # object boundary: url-bearing navigate/create. Distill only the FIRST object.
    url_idx = [i for i, e in enumerate(calls)
               if e.get("method") in ("Page.navigate", "Page.create")
               and (e.get("params") or {}).get("url")]
    if len(url_idx) >= 2:
        calls = calls[url_idx[0]: url_idx[1]]
        notes.append(f"trace shows ≥{len(url_idx)} objects (url-bearing navs); distilled ONLY the first — caller loops per object.")
    elif url_idx:
        calls = calls[url_idx[0]:]

    steps: list[dict] = []
    for e in calls:
        if is_failure(e):
            continue
        method = e.get("method") or ""
        params = keep_params(e.get("params") or {})
        purpose = str((e.get("params") or {}).get("purpose") or "")

        if method in DROP_METHODS:
            continue

        if method == "Page.navigate":
            variables["detailUrl"] = ""
            steps.append({"action": "Page.navigate",
                          "params": {"url": "$vars.detailUrl"},
                          "purpose": purpose or "Open the target page (same tab)"})
            steps.append({"type": "listen", "event": "Page.loaded",
                          "timeout": 15000, "onTimeout": "continue"})
            notes.append(f"navigate url templatized -> $vars.detailUrl (trace url: {(e.get('params') or {}).get('url')})")
            continue

        if method == "Input.press":
            steps.append({"action": "Input.press",
                          "params": {k: params[k] for k in ("key", "modifiers") if k in params},
                          "onError": "continue",
                          "purpose": purpose or "key press"})
            continue

        if method in ("Input.click", "DOM.getText", "DOM.getAttribute"):
            has_id = bool(re.fullmatch(ID_RE, str(params.get("id", ""))))
            if has_id:
                label = label_from_purpose(purpose)
                if not label:
                    label = "__TODO_LOCATE__"
                    notes.append(f"⚠️ could not infer label from purpose {purpose!r}; left __TODO_LOCATE__ for {method}.")
                slug = slugify(label) if label != "__TODO_LOCATE__" else "target"
                inner = {"action": method,
                         "params": {**{k: v for k, v in params.items() if k != "id"},
                                    "id": f"$vars.{slug}Id" if label != "__TODO_LOCATE__" else "__TODO_LOCATE__"},
                         "purpose": purpose}
                if method == "DOM.getText":
                    # extract path is "textContent": internalRpc unwraps the call's
                    # data, whose text field is textContent (live-verified 2026-07-04)
                    inner["extract"] = {f"{slug}Text": "textContent"}
                    inner["onError"] = "continue"
                    persist.append(f"{slug}Text")
                elif method == "DOM.getAttribute":
                    attr = params.get("attribute", "value")
                    inner["extract"] = {f"{slug}{attr.capitalize()}": attr}
                    inner["onError"] = "continue"
                    persist.append(f"{slug}{attr.capitalize()}")
                else:  # Input.click
                    inner["onError"] = "stop"
                if label == "__TODO_LOCATE__":
                    steps.append(inner)  # leave bare; report flags it
                else:
                    steps.extend(locate_scaffold(label, slug, inner))
            else:  # selector-based or other locator — keep as-is, flag for durability
                step = {"action": method, "params": params, "purpose": purpose}
                if method == "DOM.getText":
                    slug = slugify(label_from_purpose(purpose) or str(params.get("selector") or "sel"))
                    step["extract"] = {f"{slug}Text": "textContent"}
                    step["onError"] = "continue"
                    persist.append(f"{slug}Text")
                    notes.append(f"selector-based {method} kept as-is; verify CSS selector durability: {params.get('selector')!r}")
                steps.append(step)
            continue

        if method == "Runtime.evaluate":
            # Slow path reads the eval's return value directly; the workflow needs
            # an explicit extract map. Synthesize one from the returned object's
            # keys ({k}Text -> k) so the candidate actually fills variables. If the
            # eval returned a scalar (no keys), keep verbatim — canary will reject.
            keys = eval_result_keys(e)
            step = {"action": "Runtime.evaluate", "params": params, "purpose": purpose}
            if keys:
                extract = {f"{k}Text" if not k.endswith("Text") else k: k for k in keys}
                step["extract"] = extract
                step["onError"] = "continue"
                persist.extend(extract.keys())
                notes.append(f"Runtime.evaluate: synthesized extract {extract} from returned keys — verify field names.")
            else:
                notes.append("Runtime.evaluate kept verbatim (no object return to map) — add extract before freezing.")
            steps.append(step)
            continue

        # unknown but successful method: keep verbatim, flag
        steps.append({"action": method, "params": params, "purpose": purpose})
        notes.append(f"kept unrecognized method {method} verbatim — review.")

    return steps, notes, persist, variables


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Distill a trace into a draft skill workflow.")
    ap.add_argument("trace")
    ap.add_argument("--slug", required=True)
    ap.add_argument("--out", default=None, help="output dir (default skills/<slug>)")
    args = ap.parse_args(argv)

    trace = Path(args.trace)
    if not trace.exists():
        print(f"trace not found: {trace}", file=sys.stderr)
        return 1
    out = Path(args.out) if args.out else Path(__file__).resolve().parents[1] / args.slug
    out.mkdir(parents=True, exist_ok=True)

    events = load_events(trace)
    steps, notes, persist, variables = distill(events)

    wf = {
        "description": f"DRAFT distilled from {trace.name} — REVIEW before freezing.",
        "variables": variables or {"detailUrl": ""},
        "errorConfig": {"onError": "stop", "maxRetries": 1},
        "steps": steps,
    }
    (out / "workflow.json").write_text(json.dumps(wf, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    skill_md = f"""---
name: {args.slug}
description: |
  DRAFT — distilled from {trace.name}. Fill triggers + confirm fields.
  Triggers on: domain=<host>, task_type=<...>, stage_hint=<...>, artifact fields ⊇ {{{', '.join(persist) or '<field>'}}}.
version: 1
domain: <host>
task_type: <web_scrape|...>
stage_hint: <...>
fields: [{', '.join(persist)}]
allow_auto_captcha: false
---

## 运行指令
1. 取运行期 pageId/fleetId（复用已打开 tab）。
2. 取运行期 variables：{', '.join(variables) or 'detailUrl'}。
3. `browser_call(Workflow.execute, {{ runId, pageId, fleetId, variables, steps:<workflow.json>, errorConfig }})`。
4. 返回后读 result.variables，由 harness `record_extraction` 落盘字段：{', '.join(persist) or '<TODO>'}。
5. 失败 → catch + `Workflow.getStatus(runId)`（见 skills/README.md §6）。

## ⚠️ 蒸馏 draft，冻结前人工过：见 distill_report.md
"""
    (out / "SKILL.md").write_text(skill_md, encoding="utf-8")

    fallback = f"""success_contract:
  workflow_no_error: true
  observation_prefix: "Workflow execution completed:"
  variables_any_nonempty: [{', '.join(persist)}]
  persisted_rows_at_least: 1
  fields_required: [<TODO>]
takeover:
  on_call_error:
    recover_via: Workflow.getStatus(runId)
    read: [status.failedStepPath, status.error, status.variables, "status.results[-1].step"]
    semantic_anchor: status.results[-1].step.purpose
hitl_boundary:
  detect: [Hitl.resumed]
  action: listen_then_pause
"""
    (out / "fallback.yaml").write_text(fallback, encoding="utf-8")

    report = [f"# distill_report — {args.slug}", "",
              f"- source trace: `{trace}`",
              f"- browser_call events: {sum(1 for e in events if e.get('type')=='browser_call')}",
              f"- distilled steps: {len(steps)}",
              f"- variables: {list(variables) or ['detailUrl']}",
              f"- fields to persist post-return (harness record_extraction): {persist or ['<TODO>']}",
              "", "## decisions / TODOs"]
    report += [f"- {n}" for n in notes] or ["- (none)"]
    report += ["", "## next (human)",
               "- confirm每个 transform 的 find label 与真实 AXTree 行匹配（点击会改 DOM，必要时合并/拆分 getAXTree）。",
               "- 补 domain/task_type/stage_hint/fields；填 fallback.yaml 的 fields_required。",
               "- 解决任何 `__TODO_LOCATE__`。",
               "- 过编译版 schema 校验后再冻结。"]
    (out / "distill_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")

    print(f"wrote draft skill -> {out}")
    print(f"  steps={len(steps)} persist={persist} todos={sum(1 for n in notes if '⚠️' in n)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
