"""Compile the local validation schema for the CLI's strict output transport.

Local model defaults remain available for historical records. The wire format
requires every property, including nullable ones, and complete union branches.
"""
from __future__ import annotations

import copy
import json
from collections import Counter


def cli_output_schema(local_schema: dict) -> dict:
    schema = copy.deepcopy(local_schema)
    numeric = schema.get('$defs', {}).get('NumericConstraint')
    if numeric and numeric.get('anyOf'):
        # The Python model's partial property predicates are valid JSON Schema,
        # but strict Structured Outputs requires full objects in each branch.
        base = {k: v for k, v in numeric.items() if k != 'anyOf'}
        lower, upper = copy.deepcopy(base), copy.deepcopy(base)
        for branch, field in ((lower, 'minimum'), (upper, 'maximum')):
            prop = branch['properties'][field]
            prop.pop('anyOf', None)
            prop['type'] = 'number'
        upper['properties']['minimum'].pop('anyOf', None)
        upper['properties']['minimum']['type'] = 'null'
        schema['$defs']['NumericConstraint'] = {
            'title': numeric.get('title', 'NumericConstraint'),
            'anyOf': [lower, upper],
        }

    def require_properties(value):
        if isinstance(value, dict):
            value.pop('default', None)
            if 'properties' in value:
                value['required'] = list(value['properties'])
                value['additionalProperties'] = False
            for child in value.values():
                require_properties(child)
        elif isinstance(value, list):
            for child in value:
                require_properties(child)

    require_properties(schema)

    # Field IDs and source selectors recur in several record types. Share exact
    # enum schemas instead of spending the API's enum budget on duplicates.
    counts, enums = Counter(), {}

    def collect(value):
        if isinstance(value, dict):
            if 'enum' in value:
                key = json.dumps(value, ensure_ascii=False, sort_keys=True)
                counts[key] += 1
                enums[key] = value
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(schema)
    definitions = schema.setdefault('$defs', {})
    shared = {}
    for key, count in counts.items():
        if count > 1:
            name = f'SharedEnum{len(shared) + 1}'
            while name in definitions:
                name += '_'
            shared[key] = name

    def intern(value):
        if isinstance(value, dict):
            if 'enum' in value:
                key = json.dumps(value, ensure_ascii=False, sort_keys=True)
                if key in shared:
                    return {'$ref': '#/$defs/' + shared[key]}
            return {k: intern(v) for k, v in value.items()}
        if isinstance(value, list):
            return [intern(child) for child in value]
        return value

    schema = intern(schema)
    schema['$defs'].update({name: copy.deepcopy(enums[key]) for key, name in shared.items()})
    return schema
