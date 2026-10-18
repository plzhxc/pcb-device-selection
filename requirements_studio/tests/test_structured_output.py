"""Provider compatibility and unchanged evidence/bounds protection."""
import copy
import json
import unittest

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from backend.convergence import NumericConstraint
from backend.domain import AnalysisResult, analysis_schema, empty_document
from backend.structured_output import cli_output_schema


def walk(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


class StructuredOutputTests(unittest.TestCase):
    def schema(self, anchors=None):
        return cli_output_schema(analysis_schema(empty_document(), ['M-001'], anchors or ['@S-user-0-6']))

    def test_every_nested_object_and_union_branch_has_a_strict_contract(self):
        schema = self.schema()
        Draft202012Validator.check_schema(schema)
        self.assertEqual(schema['type'], 'object')
        self.assertNotIn('anyOf', schema)
        for item in walk(schema):
            if isinstance(item, dict) and 'properties' in item:
                self.assertEqual(set(item['required']), set(item['properties']))
                self.assertIs(item['additionalProperties'], False)
                self.assertEqual(item.get('type'), 'object')

    def test_numeric_union_requires_a_real_bound_without_losing_zero_or_negative_bounds(self):
        schema = self.schema()
        validator = Draft202012Validator({'$defs': schema['$defs'], '$ref': '#/$defs/NumericConstraint'})
        row = dict(id='NC-1', requirement_id='REQ-001', metric='voltage', scope='输入电压',
                   conditions='同一输入', minimum=None, maximum=None, unit='V',
                   source_message_id='', source_quote='@S-user-0-6')
        for low, high in [(0, None), (None, 5), (-5, 5), (0, 0)]:
            validator.validate({**row, 'minimum': low, 'maximum': high})
        self.assertFalse(validator.is_valid(row))
        self.assertFalse(validator.is_valid({k: v for k, v in row.items() if k != 'minimum'}))
        with self.assertRaises(ValidationError):
            NumericConstraint.model_validate({**row, 'minimum': 6, 'maximum': 5})
        with self.assertRaises(ValidationError):
            NumericConstraint.model_validate(row)

    def test_wire_optional_fields_are_present_but_remain_nullable(self):
        schema = self.schema()
        validator = Draft202012Validator({'$defs': schema['$defs'], '$ref': '#/$defs/DeviceReferenceUpdate'})
        row = dict(id='new-ref-1', mention='示例型号', intent='prefer', identity_scope='exact_part',
                   purpose='承接实际功能', requirement_ids=[], module_ids=[], quantity=None,
                   source_message_id='', source_quote='@S-user-0-6')
        validator.validate(row)
        self.assertFalse(validator.is_valid({k: v for k, v in row.items() if k != 'quantity'}))

    def test_source_selectors_and_taxonomy_restrictions_survive_compilation(self):
        schema = self.schema()
        validator = Draft202012Validator({'$defs': schema['$defs'], '$ref': '#/$defs/ModuleUpdate'})
        row = dict(id='new-module-1', type_id='P_1_1', name='供电模块', purpose='为产品供电',
                   boundary='具体电路待设计', quantity=1, state='proposed', requirement_ids=[],
                   source_message_id='', source_quote='@S-user-0-6')
        validator.validate(row)
        for fields in ({'source_quote': '@S-other-project'}, {'type_id': 'invented-category'},
                       {'source_message_id': 'different-user-message'}):
            self.assertFalse(validator.is_valid({**row, **fields}))

    def test_long_multiturn_sources_do_not_repeat_enum_values_past_the_provider_limit(self):
        anchors = [f'@S-{i:032x}-0-200' for i in range(80)]
        local = analysis_schema(empty_document(), [f'M-{i:03d}' for i in range(80)], anchors)
        before = copy.deepcopy(local)
        strict = cli_output_schema(local)
        count = lambda value: sum(len(v['enum']) for v in walk(value) if isinstance(v, dict) and 'enum' in v)
        self.assertGreater(count(local), 1000)
        self.assertLessEqual(count(strict), 1000)
        self.assertEqual(local, before)
        self.assertEqual(cli_output_schema(local), strict)
        self.assertNotIn('default', {k for v in walk(strict) if isinstance(v, dict) for k in v})

    def test_historical_responses_still_use_local_model_defaults(self):
        old = dict(reply='已整理', title='旧项目', summary='', updates=[], remove_ids=[], questions=[], flow=[], changes=[])
        parsed = AnalysisResult.model_validate(old)
        self.assertEqual(parsed.numeric_constraints, [])
        self.assertEqual(parsed.device_reference_updates, [])
        self.assertEqual(parsed.analysis_revision, 1)


if __name__ == '__main__':
    unittest.main()
