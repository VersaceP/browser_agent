// Package-owned deterministic dispatch and loss-aware reports. No page access
// occurs at import/description time. Business handling remains in stage.run.
export function defineStages(definitions) {
  const table = new Map();
  for (const stage of definitions) {
    if (!stage || typeof stage.id !== 'string' || !stage.id || table.has(stage.id))
      throw new Error('Stage IDs must be nonempty and unique');
    if (stage.kind === 'composite' ? !Array.isArray(stage.children) : typeof stage.run !== 'function')
      throw new Error('A stage needs run(), or kind=composite with children');
    table.set(stage.id, stage);
  }
  function expand(id, stack = []) {
    const stage = table.get(id);
    if (!stage) throw new Error('Unknown stage: ' + id);
    if (stack.includes(id)) throw new Error('Cyclic stage composition: ' + id);
    return stage.kind === 'composite'
      ? stage.children.flatMap(child => expand(child, [...stack, id])) : [stage];
  }
  for (const id of table.keys()) expand(id);
  const describeStages = () => definitions.filter(stage => stage.visibility !== 'internal').map(stage => {
    const {run, ...description} = stage;
    return JSON.parse(JSON.stringify(description));
  });
  function planStages(input = {}) {
    const wanted = input.stages === undefined
      ? definitions.filter(stage => stage.default).map(stage => stage.id) : input.stages;
    if (!Array.isArray(wanted) || wanted.some(id => typeof id !== 'string') || new Set(wanted).size !== wanted.length)
      throw new Error('stages must be an array of unique public stage IDs');
    for (const id of wanted)
      if (!table.has(id) || table.get(id).visibility === 'internal') throw new Error('Unknown public stage: ' + id);
    // Validate the whole selection before any side effect. Shared children of
    // composites run once, preserving first occurrence order.
    const selected = [...new Map(wanted.flatMap(id => expand(id)).map(stage => [stage.id, stage])).values()];
    return {requestedStages: wanted, selected};
  }
  async function runStages(api, input = {}) {
    const {requestedStages, selected} = planStages(input);
    const stages = Object.create(null);
    for (const stage of selected) stages[stage.id] = await stage.run(api, input, stages);
    return {requestedStages, executedStages: selected.map(stage => stage.id), stages};
  }
  return {describeStages, planStages, runStages};
}

export function utf8Bytes(text) {
  let bytes = 0;
  for (let i = 0; i < text.length; i++) {
    const code = text.charCodeAt(i);
    if (code < 0x80) bytes++;
    else if (code < 0x800) bytes += 2;
    else if (code >= 0xD800 && code <= 0xDBFF && i + 1 < text.length &&
             text.charCodeAt(i + 1) >= 0xDC00 && text.charCodeAt(i + 1) <= 0xDFFF) { bytes += 4; i++; }
    else bytes += 3;
  }
  return bytes;
}

export function buildReport(facts, {diagnostics, summary: suppliedSummary, maxBytes = 4 * 1024 * 1024} = {}) {
  if (!Number.isInteger(maxBytes) || maxBytes < 1024 || maxBytes > 4 * 1024 * 1024)
    throw new Error('Report byte budget must be 1024..4194304');
  // The caller supplies exact essential facts. No per-field clipping or
  // reconstruction from a truncated diagnostics object is permitted here.
  const essential = JSON.parse(JSON.stringify(facts));
  const items = Array.isArray(essential.items) ? essential.items : [];
  const counts = Object.create(null);
  for (const item of items) counts[item.status] = (counts[item.status] || 0) + 1;
  const summary = suppliedSummary === undefined ? {outcome: essential.outcome, itemCounts: counts, itemCount: items.length,
    itemsPointer: '/items', unresolvedPointer: '/unresolved',
    interpretation: 'Package facts; Agent judges original goal coverage.'} : JSON.parse(JSON.stringify(suppliedSummary));
  const value = {...essential, protocolVersion: 1, summary,
    delivery: {essentialComplete: true, diagnosticsIncluded: diagnostics !== undefined}};
  // Do not advertise addresses absent in the actual returned report.
  if (suppliedSummary === undefined) {
    if (!Array.isArray(essential.items)) delete summary.itemsPointer;
    if (!Array.isArray(essential.unresolved)) delete summary.unresolvedPointer;
  }
  if (diagnostics !== undefined) value.diagnostics = diagnostics;
  if (utf8Bytes(JSON.stringify(value)) <= maxBytes) return value;
  delete value.diagnostics;
  value.delivery = {essentialComplete: true, diagnosticsIncluded: false,
    diagnosticsOmitted: diagnostics !== undefined, reason: 'output-byte-budget',
    recovery: 'Omitted diagnostics were not retained here. Existing call values may exist; re-observation is new evidence. Do not replay writes for diagnostics.'};
  if (utf8Bytes(JSON.stringify(value)) <= maxBytes) return value;
  // Essential facts themselves do not fit. Never turn a sampled subset into a
  // success claim or advertise a nonexistent persisted copy.
  const unavailable = {protocolVersion: 1, outcome: 'unknown',
    summary: {outcome: 'unknown', reason: 'essential-report-exceeds-output-budget'},
    delivery: {essentialComplete: false, requiredBytes: utf8Bytes(JSON.stringify(value)), limitBytes: maxBytes,
      recovery: 'Original essential report unavailable. Read retained call evidence or reconcile current state; no write replay to recover evidence.'}};
  // A small count summary may survive, but all evidence pointers are deliberately
  // absent: their source report did not fit. Never advertise dangling addresses.
  unavailable.summary.observedItemCount = items.length;
  return unavailable;
}
