"""Observed author intervals; no optimization score, stop rule or extra model call."""
from __future__ import annotations

import copy
from collections import Counter

TOKEN_CLASSES = ('uncached_input', 'cache_read', 'cache_creation', 'output')


def checkpoint(session):
    usage = session.usage.snapshot()
    aggregator = getattr(session.logger, 'usage_aggregator', None)
    retries = ({k: v for k, v in aggregator.summary().items() if k.endswith('_retries')}
               if aggregator else None)
    return copy.deepcopy({k: usage.get(k) for k in ('tokenClasses', 'modelCalls', 'netSeconds', 'tools')} | {
        'providerRetries': retries, 'runId': session.logger.run_id,
        'experimentCount': len(session.state.get('authorExperiments', []))})


def _difference(before, after):
    return after - before if (type(before) in (int, float) and type(after) in (int, float)
                             and after >= before) else None


def interval(before, after):
    """Unavailable or reset counters remain unknown, including across processes."""
    same_run = before.get('runId') == after.get('runId')
    def delta(left, right):
        return _difference(left, right) if same_run else None
    tools = {}
    for name in before.get('tools', {}).keys() | after.get('tools', {}).keys():
        left, right = before.get('tools', {}).get(name, {}), after.get('tools', {}).get(name, {})
        row = {k: delta(left.get(k, 0), right.get(k, 0)) for k in ('calls', 'failed', 'durationMs')}
        if row['calls'] != 0:
            tools[name] = row
    old, new = before.get('providerRetries'), after.get('providerRetries')
    return {'tokenClasses': {k: delta(before.get('tokenClasses', {}).get(k), after.get('tokenClasses', {}).get(k))
                             for k in TOKEN_CLASSES},
            'modelCalls': delta(before.get('modelCalls'), after.get('modelCalls')),
            'netSeconds': delta(before.get('netSeconds'), after.get('netSeconds')),
            'tools': tools,
            'providerRetries': ({k: delta(old.get(k), new.get(k)) for k in old.keys() | new.keys()}
                                if isinstance(old, dict) and isinstance(new, dict) else None)}


def capture(session, *, final=False):
    """Close at a model boundary, after tool accounting, or at final handoff.

    A group of parallel executions shares one interval; never allocate the same
    model/author cost to each execution. Intermediate edits/tests/reads belong
    to this interval. The next assessment belongs to the following interval.
    """
    if session.profile != 'author':
        return
    current = checkpoint(session)
    previous = getattr(session, '_author_cost_anchor', None)
    if previous is None:
        session._author_cost_anchor = current
        return
    experiments = session.state.get('authorExperiments', [])[previous['experimentCount']:]
    if not experiments and not final:
        return
    rows, failures, methods, complete = 0, 0, Counter(), True
    for experiment in experiments:
        receipt = session.store.read_ref(experiment['executionRef'])
        data = ((receipt.get('response') or {}).get('data') or {})
        from harness.workflow.call_facts import call_facts
        complete &= call_facts(data).get('metadataComplete') is True
        for call in data.get('results') or []:
            if not isinstance(call, dict):
                complete = False
                continue
            rows += 1
            failures += call.get('status') == 'failed'
            methods[call.get('action') or call.get('kind') or 'unknown'] += 1
    value = {'kind': 'handoff' if final else 'validation', 'runId': current['runId'],
             'startCounters': previous, 'endCounters': current,
             'previousIntervalRef': session.state.get('authorCostLatest', {}).get('receiptRef'),
             'executionRefs': [e['executionRef'] for e in experiments],
             'packageHashes': list(dict.fromkeys(e['contentHash'] for e in experiments)),
             **interval(previous, current),
             'coverage': {'modelCounters': 'Session Usage: returned author-model results only',
                          'auxiliaryModelCost': None,
                          'note': 'Compaction/other auxiliary models and failed requests without usage are not attributed here. '
                                  'Use task-wide evaluation for complete cross-role totals.'},
             'workflowIO': {'executions': len(experiments), 'recordedCalls': rows,
                            'failedRecordedCalls': failures, 'metadataComplete': complete,
                            'callsByMethod': dict(methods)},
             'interpretation': 'Author effort since the previous boundary in this run: model, edits, reads, '
                'offline checks and live validation. Human waits are excluded. Assessment after this '
                'boundary is charged to the next interval. Tool durations may overlap or contain human waits; '
                'never sum them as wall time. Workflow I/O covers linked ledgers only. '
                'Compare equivalent goals, inputs, starting states, results and validation scope before '
                'claiming savings. Package execution cost and author effort are distinct. No automatic verdict.'}
    ref = session.store.receipt(session.state, 'workflow.author.cost_interval', value)
    latest = {k: value[k] for k in ('kind', 'executionRefs', 'tokenClasses', 'modelCalls', 'netSeconds', 'providerRetries')}
    latest.update(receiptRef=ref, workflowIO={k: value['workflowIO'][k] for k in
                                           ('executions', 'recordedCalls', 'failedRecordedCalls', 'metadataComplete')})
    session.state['authorCostLatest'] = latest
    session._author_cost_anchor = current
    session.save()
