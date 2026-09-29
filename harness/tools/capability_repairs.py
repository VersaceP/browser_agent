"""Small, lossless parameter relocations; never infer a target or query view."""
from __future__ import annotations

import copy

from harness.tools.argument_pipeline import (
    SchemaIssue, apply_required_schema_defaults, capability_input_schema, validate_schema,
)


def prepare_capability_arguments(method, params, schema):
    """Return copied params, value-free audit entries, and unresolved issues.

    Run before target/policy guards. The caller must still validate the complete
    result against the live schema. Conflicting aliases are rejected atomically.
    """
    prepared = copy.deepcopy(params)
    repairs, issues = [], []
    if not isinstance(prepared, dict):
        return params, [], [SchemaIssue((), 'type', 'Action params must be an object')]
    properties = schema.get('properties') if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        # No authoritative shape: do not invent aliases or schema semantics.
        return prepared, [], []

    def move(source, destination, old, new, key):
        if key in new and new[key] != old[source]:
            issues.append(SchemaIssue(tuple(destination.split('.')), 'conflict',
                                      f'conflicts with {source}; supply only {destination} with the intended value'))
            return
        new[key] = old.pop(source)
        repairs.append({'from': source, 'to': destination, 'operation': 'relocate'})

    query = prepared.get('query')
    misplaced_targets = (method == 'DOM.getAXTree' and 'targets' in prepared
                         and 'targets' not in properties)
    if misplaced_targets and not isinstance(query, dict):
        issues.append(SchemaIssue(('query',), 'required',
            'targets requires an explicit query.view and query.targets; '
            'cannot infer the intended view or fall back to a full-page read'))
    if method == 'DOM.getAXTree' and isinstance(query, dict):
        query_schema = properties.get('query')
        branches = query_schema.get('oneOf', []) if isinstance(query_schema, dict) else []
        if not isinstance(branches, list):
            branches = []
        view = query.get('view')
        selected = [branch['properties'] for branch in branches
                    if isinstance(branch, dict) and isinstance(branch.get('properties'), dict)
                    and isinstance(branch['properties'].get('view'), dict)
                    and isinstance(view, str)
                    and branch['properties']['view'].get('const') == view]
        selected_properties = selected[0] if len(selected) == 1 else {}
        if misplaced_targets and 'targets' in selected_properties:
            move('targets', 'query.targets', prepared, query, 'targets')
        elif misplaced_targets:
            issues.append(SchemaIssue(('targets',), 'misplaced',
                'Cannot relocate targets without one schema-supported query.view; '
                'supply the intended view and canonical query.targets explicitly'))
        # These options belong to particular views. Never silently discard
        # them: the model may have chosen the wrong view rather than the key.
        owners = {'maxDepth': {'dom'}, 'includeShadowDom': {'dom'},
                  'textMode': {'text'}, 'attributes': {'attributes'}}
        if len(selected) == 1 and view in {'dom', 'text', 'attributes', 'state'}:
            for key, views in owners.items():
                if key in query and view not in views and key not in selected_properties:
                    issues.append(SchemaIssue(('query', key), 'query_view',
                        f'not valid for query.view={view}; use it only with {sorted(views)}. '
                        'Remove this field if the current view is intended; otherwise correct query.view.'))
    if (method == 'Input.type' and 'target' in prepared and 'target' not in properties
            and any(key in properties for key in ('id', 'selector'))):
        target = prepared.get('target')
        if (isinstance(target, dict) and target and set(target) <= {'id', 'selector'}
                and all(key in properties for key in target)):
            for key in list(target):
                if key in prepared and prepared[key] != target[key]:
                    issues.append(SchemaIssue((key,), 'conflict',
                        f'conflicts with target.{key}; supply only {key} with the intended value'))
                else:
                    prepared[key] = target[key]
                    repairs.append({'from': f'target.{key}', 'to': key, 'operation': 'relocate'})
            del prepared['target']
        else:
            # These schemas may strip unknown keys. Do not ignore an explicit
            # but ambiguous target just because a top-level fallback is valid.
            issues.append(SchemaIssue(('target',), 'misplaced',
                'Use the schema-supported top-level id/selector. The target alias '
                'must contain only those fields; no automatic repair was applied'))
    if issues:
        return params, [], issues
    return prepared, repairs, []


def prepare_workflow_literal_arguments(params, method_schemas):
    """Repair only literal action params, never evaluate workflow expressions.

    Traversal follows step containers, not arbitrary business data. Definitions
    are copied; saved source definitions and their hashes remain unchanged.
    Dynamic params stay under the platform's execution-time validation.
    """
    prepared = copy.deepcopy(params)
    repairs, issues = [], []

    def dynamic(value):
        if isinstance(value, str):
            # Platform resolveParams resolves whole values starting with '$'.
            # Embedded currency, braces and object keys are literal content.
            return value.startswith('$')
        if isinstance(value, dict):
            return any(dynamic(v) for v in value.values())
        if isinstance(value, list):
            return any(dynamic(v) for v in value)
        return False

    def visit(steps, path):
        if not isinstance(steps, list):
            return
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            prefix = path + (str(index),)
            method = step.get('action')
            args = step.get('params')
            if (step.get('type', 'action') == 'action'
                    and method in {'Input.type', 'DOM.getAXTree'}
                    and isinstance(args, dict)):
                schema = capability_input_schema(method_schemas, method)
                fixed, changes, errors = prepare_capability_arguments(method, args, schema)
                if dynamic(args) and changes:
                    # Resolved values are not available in the Harness. Never
                    # let a known misplaced target silently become a full read.
                    errors.append(SchemaIssue((), 'dynamic_arguments',
                        'This action mixes misplaced fields and Workflow references. '
                        'Use the canonical field paths before execution, or resolve '
                        'the values and issue a direct action; no automatic repair was applied.'))
                elif changes:
                    fixed, _ = apply_required_schema_defaults(fixed, schema)
                    # Binding and purpose are supplied by the engine; preserve
                    # all other schema constraints, including local references.
                    action_schema = copy.deepcopy(schema or {})
                    action_schema['required'] = [
                        key for key in action_schema.get('required', [])
                        if key not in {'pageId', 'fleetId', 'purpose'}
                    ]
                    errors += validate_schema(fixed, action_schema)
                issues.extend(SchemaIssue(prefix + ('params',) + e.path, e.keyword, e.message)
                              for e in errors)
                if not errors:
                    step['params'] = fixed
                    for change in changes:
                        root = '.'.join(prefix + ('params',))
                        repairs.append({**change, 'from': root + '.' + change['from'],
                                        'to': root + '.' + change['to']})
            for key in ('then', 'else', 'body'):
                visit(step.get(key), prefix + (key,))

    visit(prepared.get('steps'), ('steps',))
    if issues:
        return params, [], issues
    return prepared, repairs, []
