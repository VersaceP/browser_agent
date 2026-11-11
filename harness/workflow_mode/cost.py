"""Task cost facts, with deduplicated ownership and explicit unknown coverage."""
from __future__ import annotations

from collections import Counter

TOKEN_CLASSES = ('cache_read', 'cache_creation', 'uncached_input', 'output')


def report(session):
    own = session.usage.snapshot()
    nodes = {session.logger.task_id: {'taskId': session.logger.task_id, 'role': session.profile,
        'tokenClasses': own['tokenClasses'], 'modelCalls': own['modelCalls'],
        'tools': own.get('tools', {}), 'netSeconds': own['netSeconds']}}
    missing_children = []
    for task_id, child in session.state.get('children', {}).items():
        cost = (child.get('result') or {}).get('cost') or {}
        if not cost.get('nodes'):
            missing_children.append(task_id)
        for node in cost.get('nodes', []):
            nodes.setdefault(node['taskId'], node)
    for child in session.state.get('delegations', []):
        task_id = child.get('browserTaskId')
        if not task_id or task_id in nodes:
            continue
        receipt = session.store.read_ref(child['receiptRef'])
        nested = receipt.get('cost') or (receipt.get('result') or {}).get('cost') or {}
        if nested.get('nodes'):
            for node in nested['nodes']:
                nodes.setdefault(node['taskId'], node)
            missing_children.extend(nested.get('missingChildCost') or [])
            continue
        usage = receipt.get('usage') or {}
        # Legacy Browser aggregate counters lack per-counter availability.
        # Preserve reported values separately; do not turn absent data into zero.
        available = usage.get('counterAvailability') or {}
        nodes[task_id] = {'taskId': task_id, 'role': 'browser_' + child.get('mode', 'author'),
            'tokenClasses': {name: usage.get(name) if available.get(name) is True else None for name in TOKEN_CLASSES},
            'reportedTokenClasses': {name: usage.get(name) for name in TOKEN_CLASSES},
            'modelCalls': usage.get('calls'), 'receiptRef': child['receiptRef'],
            'transportMetrics': receipt.get('transportMetrics')}
    totals = {name: sum(node['tokenClasses'][name] for node in nodes.values())
              if not missing_children and all(node['tokenClasses'].get(name) is not None for node in nodes.values()) else None
              for name in TOKEN_CLASSES}
    methods = Counter()
    failures = 0
    ledger_complete = True
    execution_count = 0
    for execution in session.state.get('executions', []):
        if not execution.get('receiptRef'):
            ledger_complete = False
            continue
        receipt = session.store.read_ref(execution['receiptRef'])
        data = ((receipt.get('response') or {}).get('data') or {})
        from harness.workflow.call_facts import call_facts
        facts = call_facts(data)
        ledger_complete &= facts.get('metadataComplete') is True
        execution_count += 1
        for call in data.get('results') or []:
            methods[call.get('action') or call.get('kind') or 'unknown'] += 1
            failures += call.get('status') == 'failed'
    rounds = [node.get('modelCalls') for node in nodes.values()]
    nodes[session.logger.task_id]['platform'] = {'workflowExecutions': execution_count,
        'recordedCalls': sum(methods.values()), 'failedRecordedCalls': failures,
        'metadataComplete': ledger_complete, 'callsByMethod': dict(methods)}
    nodes[session.logger.task_id]['transportMetrics'] = getattr(session.logger, 'transport_metrics', None)
    return {'scope': 'Distinct task owners; resumed Workflow counters are cumulative. Browser counters are reported per delegate.',
        'nodes': list(nodes.values()), 'tokenClasses': totals,
        'modelCalls': sum(rounds) if not missing_children and all(n is not None for n in rounds) else None,
        'missingChildCost': missing_children,
        'platform': {'workflowExecutions': execution_count, 'recordedCalls': sum(methods.values()),
            'failedRecordedCalls': failures, 'metadataComplete': ledger_complete,
            'callsByMethod': dict(methods), 'coverage': 'This task Workflow ledgers only; Browser I/O remains in its linked task evidence.'},
        'timing': {'sessionNetSeconds': own['netSeconds'],
            'wholeTaskNetSeconds': own['treeTiming']['netSeconds'] if own['treeTimingComplete'] else None,
            'taskTreeObserved': own['treeTiming'],
            'historicalCoverageComplete': own['treeTimingComplete'],
            'coverage': 'Active Harness time across resumes; in-process task-tree terminal/HITL waits are excluded as an interval union. HITL includes control-release confirmation. Child durations are never summed. Inactive time between runs and unobserved external waits are outside this metric.'},
        'providerRetriesThisProcess': {key: value for key, value in session.logger.usage_aggregator.summary().items() if key.endswith('_retries')},
        'modelCallCoverage': 'Returned model results; provider retries are separate. Failed requests without a returned result may be absent.',
        'interpretation': 'No business verdict, optimization threshold or cost-based stop. Unknown counters remain null; smaller messages or fewer calls alone do not prove an improvement.'}
