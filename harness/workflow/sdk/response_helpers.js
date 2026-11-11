// Harness response helpers v2. WebCross Workflow SDK returns action VALUES,
// unlike the host RPC envelope {data: value}. Read the live schema after upgrades.
export const helperVersion = "2";

export function requireArray(value, action) {
  if (!Array.isArray(value)) {
    const error = new TypeError(`${action}: expected an action-value array; inspect the raw receipt`);
    error.code = "unexpected-response-shape";
    error.action = action;
    error.received = value;
    throw error;
  }
  return value;
}

export function pageRows(value) {
  const rows = requireArray(value, "Page.list");
  if (rows.some(row => !row || typeof row.pageId !== "string")) {
    const error = new TypeError("Page.list: a row has no pageId; inspect the raw receipt");
    error.code = "unexpected-response-shape";
    error.received = value;
    throw error;
  }
  return rows; // Preserve all fields, including opener, URL and lifecycle.
}

export function errorFact(error, action) {
  // Preserve enumerable platform fields and standard Error properties.
  return { ...error, name: error?.name ?? null, message: error?.message ?? String(error),
    code: error?.code ?? null, details: error?.details ?? null, action };
}

// An observation failure is not an empty observation. Callers return this
// fact with completed/remaining work; only a successful read may enter a
// condition wait. No retry or choice of business recovery is hidden here.

export async function captureAction(action, invoke) {
  // Successful empty values and failed calls are distinct; no automatic retries.
  try { return { status: "succeeded", action, value: await invoke() }; }
  catch (error) { return { status: "failed", action, error: errorFact(error, action) }; }
}

export function changedKeys(before, after, keyOf) {
  // The author supplies identity semantics. Never guess indices or business keys.
  const previous = new Set(requireArray(before, "before").map(keyOf));
  return requireArray(after, "after").filter(row => !previous.has(keyOf(row)));
}
