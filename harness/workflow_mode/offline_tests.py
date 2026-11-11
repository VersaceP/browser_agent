"""Offline regression tests shipped inside a Workflow package (tests/**/*.test.js).

Each case drives the package's modules with recorded fixtures and a mock api. They run in the installed WebCross
Workflow runtime (its esbuild compiler and QuickJS VM), the same engine that
executes the program in production: no fs, network, process or timers. A test
that only passes on another JS engine would be a false certification, so there
is no system-Node fallback.

Test files import the harness kit:  import {test, assert, fixture} from './testkit.js'
(path relative to the test file). Text fixtures under tests/fixtures/ are read
with fixture('tests/fixtures/name.txt') inside a test body.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from harness.workflow import offline_runtime

# Shown to the author in the tool description and system prompt. Every
# statement here was checked against the installed engine (0.9.78).
USAGE = """Offline tests run tests/**/*.test.js in the installed WebCross engine (esbuild + QuickJS).
- Register cases with the Harness kit; it is provided at run time, do not write your own:
    import {test, assert, fixture, fixtureJSON} from './testkit.js';   // path relative to the test file
    test('name', () => { assert.deepEqual(parse(fixtureJSON('tests/fixtures/page.json')), {...}); });
  assert: ok, equal, notEqual, deepEqual, match, throws(fn, code|RegExp), rejects(promise, code|RegExp).
  A file that registers no test() case fails as "no registered cases".
- Fixtures: files under tests/fixtures/ are read inside a test body with fixture(path) (text) or
  fixtureJSON(path) (parsed JSON). Paths are package-relative, e.g. 'tests/fixtures/page.json'.
- Imports: only package-relative static imports of .js/.ts files ('./x.js'; a TypeScript module is
  imported with its .js suffix). `import x from './data.json'` is not supported (the package holds
  only .js/.ts sources): use fixtureJSON() in tests, or `export default {...}` in a .js module.
  Third-party code works when its source is copied into the package and imported relatively
  ('./vendor/lib.js', ESM or CommonJS). Bare specifiers ('lodash') and node: built-ins are rejected.
- No fs, network, process or timers. Drive modules with recorded fixtures and a mock api object.
- Do not inspect the WebCross installation to diagnose a test result; read the returned
  status, error and hint fields."""

TEST_GLOB = "tests/**/*.test.js"
KIT_PATH = "tests/testkit.js"
ENTRY = "__harness_test_entry.js"

TESTKIT = r"""
const cases = [];
let fixtures = {};
export class AssertionError extends Error {
  constructor(message) { super(message); this.name = "AssertionError"; this.code = "assertion-failed"; }
}
function show(value) { try { return JSON.stringify(value); } catch (e) { return String(value); } }
function same(a, b) {
  if (Object.is(a, b)) return true;
  if (!a || !b || typeof a !== "object" || typeof b !== "object") return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  const ka = Object.keys(a), kb = Object.keys(b);
  return ka.length === kb.length && ka.every(k => Object.prototype.hasOwnProperty.call(b, k) && same(a[k], b[k]));
}
function expected(error, matcher) {
  if (matcher === undefined) return true;
  const text = String((error && error.message) || error);
  if (matcher instanceof RegExp) return matcher.test(text) || matcher.test(String(error && error.code));
  if (typeof matcher === "string") return (error && error.code) === matcher || text.includes(matcher);
  if (typeof matcher === "function") return matcher(error) === true;
  return false;
}
export const assert = {
  ok(value, message) { if (!value) throw new AssertionError(message || "expected truthy, got " + show(value)); },
  equal(actual, wanted, message) { if (!Object.is(actual, wanted)) throw new AssertionError(message || show(actual) + " !== " + show(wanted)); },
  notEqual(actual, wanted, message) { if (Object.is(actual, wanted)) throw new AssertionError(message || "unexpected " + show(actual)); },
  deepEqual(actual, wanted, message) { if (!same(actual, wanted)) throw new AssertionError(message || show(actual) + " does not deep-equal " + show(wanted)); },
  match(text, pattern, message) { if (!pattern.test(String(text))) throw new AssertionError(message || show(text) + " does not match " + pattern); },
  throws(fn, matcher, message) {
    try { fn(); } catch (error) { if (expected(error, matcher)) return error; throw new AssertionError(message || "unexpected error " + show(String(error && error.message))); }
    throw new AssertionError(message || "expected an exception");
  },
  async rejects(value, matcher, message) {
    try { await (typeof value === "function" ? value() : value); }
    catch (error) { if (expected(error, matcher)) return error; throw new AssertionError(message || "unexpected rejection " + show(String(error && error.message))); }
    throw new AssertionError(message || "expected a rejection");
  },
};
export function test(name, fn) { cases.push({ name: String(name), fn }); }
export function fixture(path) {
  if (!Object.prototype.hasOwnProperty.call(fixtures, path)) throw new Error("Unknown fixture: " + path);
  return fixtures[path];
}
export function fixtureJSON(path) { return JSON.parse(fixture(path)); }
export function __setFixtures(value) { fixtures = value || {}; }
export async function __run() {
  const results = [];
  for (const item of cases) {
    try { await item.fn(); results.push({ name: item.name, ok: true }); }
    catch (error) {
      results.push({ name: item.name, ok: false, code: (error && error.code) || null,
        error: String((error && error.message) || error).slice(0, 2000) });
    }
  }
  return { results };
}
"""


def discover(work_dir: Path) -> list[Path]:
    root = Path(work_dir).resolve()
    return [p for p in sorted(root.glob(TEST_GLOB))
            if p.is_file() and not p.is_symlink() and p.resolve().is_relative_to(root)]


def _test_sources(root: Path) -> tuple[dict[str, str], dict[str, str]]:
    sources, fixtures = {}, {}
    for path in sorted((root / "tests").rglob("*")) if (root / "tests").is_dir() else []:
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if path.suffix in {".js", ".ts"}:
            sources[relative] = path.read_text(encoding="utf-8")
        elif relative.startswith("tests/fixtures/"):
            try:
                fixtures[relative] = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue  # Binary fixtures are not readable inside QuickJS.
    sources[KIT_PATH] = TESTKIT
    return sources, fixtures


def run(work_dir: Path, *, timeout_s: float = 300.0, content_hash: str | None = None) -> dict[str, Any]:
    """Return facts per test file; a failing test never raises."""
    root = Path(work_dir).resolve()
    files = discover(root)
    base = {"contentHash": content_hash, "glob": TEST_GLOB, "files": [p.relative_to(root).as_posix() for p in files]}
    if not files:
        return {**base, "status": "absent",
                "interpretation": "No offline regression tests ship with this package. Known variations are unverified offline."}
    runtime = offline_runtime.locate()
    if runtime is None:
        return {**base, "status": "unavailable", "error": offline_runtime.unavailable_reason()}
    shipped = offline_runtime.package_sources(root)
    tests, fixtures = _test_sources(root)
    jobs, rejected = [], {}
    for path in files:
        relative = path.relative_to(root).as_posix()
        entry = (f"import {{__run, __setFixtures}} from './{KIT_PATH}';\n"
                 f"import './{relative}';\n"
                 "export default async function main(api, input) { __setFixtures(input.fixtures); return await __run(); }\n")
        package = offline_runtime.reachable({**shipped, **tests, ENTRY: entry}, ENTRY)
        used = {name: text for name, text in fixtures.items()
                if any(name in source for source in package.values())} or fixtures
        problem = offline_runtime.check_limits(package, {"fixtures": used})
        if problem:
            rejected[relative] = problem
            continue
        jobs.append((relative, {"workflow": {"schemaVersion": 1, "name": "offline-test " + relative,
            "entry": ENTRY, "timeoutMs": 60000, "files": package}, "input": {"fixtures": used}}))
    started = time.monotonic()
    try:
        outcomes = offline_runtime.run_jobs([job for _, job in jobs], timeout_s=timeout_s) if jobs else []
    except (OSError, RuntimeError, ValueError) as exc:
        return {**base, "status": "unavailable", "error": str(exc)[:3000], "runtime": runtime["version"]}
    results = [{"file": name, "status": "rejected", "error": problem} for name, problem in rejected.items()]
    for (name, job), outcome in zip(jobs, outcomes):
        cases = ((outcome.get("output") or {}).get("results") or []) if outcome["status"] == "succeeded" else []
        item = {"file": name, "status": outcome["status"], "cases": cases,
                "failed": [c["name"] for c in cases if not c.get("ok")], "error": outcome.get("error")}
        hint = _hint(root / name, outcome, cases)
        if hint:
            item["hint"] = hint
        results.append(item)
    passed = all(r["status"] == "succeeded" and r.get("cases") and not r["failed"] for r in results)
    cases = [c for r in results for c in r.get("cases", [])]
    return {**base, "status": "passed" if passed else "failed",
            "totals": {"tests": len(cases), "pass": sum(c.get("ok", False) for c in cases),
                       "fail": sum(not c.get("ok", False) for c in cases)},
            "results": results, "durationMs": int((time.monotonic() - started) * 1000),
            "runtime": {"engine": "WebCross Workflow (esbuild + QuickJS)", "version": runtime["version"]},
            "interpretation": "A file with no registered cases or a compile/uncaught error counts as failed."}


def _hint(path: Path, outcome: dict[str, Any], cases: list) -> str | None:
    """Name the likely authoring mistake; the raw status and error stay as returned."""
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        source = ""
    message = str((outcome.get("error") or {}).get("message") or "")
    if outcome["status"] == "succeeded" and not cases:
        if "testkit.js" not in source:
            return ("No test cases registered: this file does not import the Harness kit. Use "
                    "import {test, assert, fixtureJSON} from './testkit.js' and register cases with test().")
        return "No test cases registered: the file imports testkit.js but never calls test()."
    if ".json" in message and ("Import source is missing" in message or "invalid_key" in message):
        return ("JSON modules cannot be imported. Read JSON fixtures with fixtureJSON('tests/fixtures/x.json') "
                "inside a test body, or move the data into a .js module with export default.")
    if "Only package-relative static imports" in message:
        return ("Bare package names and node: built-ins are not resolvable. Copy third-party source into the "
                "package (for example src/vendor/lib.js) and import it with a relative path.")
    return None


def summary(result: dict[str, Any], receipt_ref: str | None = None) -> dict[str, Any]:
    keep = ("status", "contentHash", "files", "totals", "durationMs", "error", "runtime", "interpretation")
    value = {k: result[k] for k in keep if k in result}
    cases = [{"file": r["file"], "name": c.get("name"), "ok": c.get("ok"),
              **({"evidence": {"ref": receipt_ref, "pointer": f"/results/{i}/cases/{j}"}}
                 if receipt_ref else {})}
             for i, r in enumerate(result.get("results", [])) for j, c in enumerate(r.get("cases", []))]
    # A failure after the first 30 cases must be visible on the first read.
    ordered = [case for case in cases if not case.get('ok')] + [case for case in cases if case.get('ok')]
    value["registeredCases"] = ordered[:30]
    value["registeredCaseCount"] = len(cases)
    value['registeredCasesTruncated'] = len(cases) > 30
    if receipt_ref:
        value["evidenceLocations"] = {key: {"ref": receipt_ref, "pointer": "/" + key}
                                      for key in (*keep, "results") if key in result}
    value["coverageNote"] = ("Case names describe registered tests, not measured execution coverage. "
                             "A passing pure-function test does not verify live interaction or a repaired stage.")
    failing = [{"file": r["file"], "status": r["status"], "failed": r.get("failed"), "error": r.get("error"),
                **({"hint": r["hint"]} if r.get("hint") else {}),
                "messages": [c.get("error") for c in r.get("cases", []) if not c.get("ok")][:10]}
               for r in result.get("results", [])
               if r["status"] != "succeeded" or r.get("failed") or not r.get("cases")]
    if failing:
        value["failing"] = failing
    return value
