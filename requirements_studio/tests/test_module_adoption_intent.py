"""A real module-adoption click is not permission to change existing parts."""
import copy
from types import SimpleNamespace
import unittest

from backend.device_domain import DeviceReferenceUpdate, apply_updates


ADOPTION = '''确认本版功能模块方案：
- AHT10温湿度采集模块：传感器与采集类模块 / 温度传感器模块，1个独立电路；用途：采集环境温度和相对湿度。
- TC6291B供电模块：电源类模块 / Boost升压子模块，1个独立电路；用途：升压供电。
我采用上述功能类别、用途和数量。具体器件、拓扑、真实引脚、PCB与接口实现委托设计阶段确定，必须满足此前已明确的产品要求和使用边界；这不是电路已验证。'''


class ModuleAdoptionIntentTests(unittest.TestCase):
    def setUp(self):
        self.original_text = '采用AHT10进行温湿度采集，并列出TC6291B。'
        self.doc = {
            'requirements': [{'id': 'REQ-001', 'state': 'stated'},
                             {'id': 'REQ-002', 'state': 'stated'}],
            'modules': [{'id': 'MOD-001', 'state': 'proposed', 'type_id': 'M_10_2_1',
                         'name': '功能一', 'purpose': '承接测试功能', 'quantity': 1,
                         'requirement_ids': ['REQ-001']},
                        {'id': 'MOD-002', 'state': 'proposed', 'type_id': 'M_10_2_1',
                         'name': '功能二', 'purpose': '承接测试功能', 'quantity': 1,
                         'requirement_ids': ['REQ-002']}],
            'device_constraints': [],
            'device_references': [self.reference('REF-001', 'AHT10', 'must', 2),
                                  self.reference('REF-002', 'TC6291B', 'reference', None)],
        }
        self.messages = [{'id': 'original', 'role': 'user', 'content': self.original_text},
                         {'id': 'adoption', 'role': 'user', 'content': ADOPTION}]

    def reference(self, key, name, intent, quantity):
        return {'id': key, 'mention': name, 'intent': intent, 'identity_scope': 'exact_part',
                'purpose': '原用途', 'requirement_ids': ['REQ-001'], 'module_ids': ['MOD-001'],
                'quantity': quantity, 'source_message_id': 'original',
                'source_quote': self.original_text, 'lifecycle': 'active',
                'device_key': 'identity-' + key, 'identity_decision': None}

    def apply(self, index=0, removals=None, **changes):
        old = self.doc['device_references'][index]
        row = {k: v for k, v in old.items() if k in DeviceReferenceUpdate.model_fields}
        row.update(source_message_id='adoption', source_quote=ADOPTION)
        row.update(changes)
        result = SimpleNamespace(device_reference_updates=[] if removals else [DeviceReferenceUpdate(**row)],
                                 remove_device_reference_ids=removals or [], device_constraints=[])
        doc = copy.deepcopy(self.doc)
        apply_updates(doc, result, self.doc, self.messages, {}, {})
        return doc

    def test_observed_click_cannot_relax_existing_device_intents(self):
        for prior, attempted in [('must', 'reference'), ('must', 'prefer'),
                                 ('must', 'undecided'), ('prefer', 'reference'),
                                 ('reference', 'must')]:
            with self.subTest(prior=prior, attempted=attempted):
                self.doc['device_references'][0]['intent'] = prior
                before = copy.deepcopy(self.doc)
                with self.assertRaisesRegex(ValueError, '模块方案|模块采纳'):
                    self.apply(intent=attempted)
                self.assertEqual(self.doc, before)

    def test_circuit_count_cannot_change_or_erase_physical_part_quantity(self):
        for quantity in (1, None):
            with self.subTest(quantity=quantity):
                with self.assertRaisesRegex(ValueError, '模块方案|模块采纳'):
                    self.apply(quantity=quantity)
        self.doc['device_references'][0]['quantity'] = None
        with self.assertRaisesRegex(ValueError, '模块方案|模块采纳'):
            self.apply(quantity=1)

    def test_click_cannot_change_part_identity_or_identity_scope(self):
        for changes in ({'mention': 'INVENTED-SENSOR'}, {'identity_scope': 'family'}):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, '模块方案|模块采纳'):
                    self.apply(**changes)

    def test_using_a_new_reference_alias_cannot_bypass_the_preservation_rule(self):
        with self.assertRaisesRegex(ValueError, '模块方案|模块采纳'):
            self.apply(id='new-reference', intent='reference')

    def test_one_repair_error_identifies_every_unsupported_part_in_the_batch(self):
        updates = []
        for old, intent in zip(self.doc['device_references'], ('reference', 'must')):
            row = {k: v for k, v in old.items() if k in DeviceReferenceUpdate.model_fields}
            row.update(intent=intent, source_message_id='adoption', source_quote=ADOPTION)
            updates.append(DeviceReferenceUpdate(**row))
        with self.assertRaises(ValueError) as error:
            apply_updates(copy.deepcopy(self.doc),
                          SimpleNamespace(device_reference_updates=updates,
                                          remove_device_reference_ids=[], device_constraints=[]),
                          self.doc, self.messages, {}, {})
        self.assertIn('AHT10', str(error.exception))
        self.assertIn('TC6291B', str(error.exception))

    def test_reusing_original_source_cannot_justify_relaxation_during_adoption(self):
        with self.assertRaisesRegex(ValueError, '模块方案|模块采纳'):
            self.apply(intent='reference', source_message_id='original', source_quote=self.original_text)

    def test_adoption_cannot_remove_an_existing_device_reference(self):
        with self.assertRaisesRegex(ValueError, '模块方案|模块采纳'):
            self.apply(removals=['REF-001'])

    def test_functional_updates_preserve_intent_quantity_identity_and_their_evidence(self):
        doc = self.apply(purpose='采集温湿度并交给主控', module_ids=['MOD-001', 'MOD-002'])
        row = doc['device_references'][0]
        for key in ('intent', 'mention', 'quantity', 'identity_scope', 'device_key',
                    'source_message_id', 'source_quote'):
            self.assertEqual(row[key], self.doc['device_references'][0][key], key)
        self.assertEqual(row['purpose'], '采集温湿度并交给主控')
        self.assertEqual(row['module_ids'], ['MOD-001', 'MOD-002'])
        self.assertEqual(doc['device_references'][1]['intent'], 'reference')

    def test_independent_explicit_customer_decision_before_adoption_is_still_usable(self):
        self.doc['device_references'][1]['intent'] = 'must'
        text = 'TC6291B仅作参考，不强制最终采用，充电和备电功能保留。'
        self.messages.insert(1, {'id': 'decision', 'role': 'user', 'content': text})
        doc = self.apply(1, intent='reference', source_message_id='decision', source_quote=text)
        self.assertEqual(doc['device_references'][1]['intent'], 'reference')
        self.assertEqual(doc['device_references'][0]['intent'], 'must')

    def test_later_explicit_customer_change_remains_supported(self):
        text = 'AHT10改为仅作参考，温湿度采集功能保留。'
        self.messages.append({'id': 'later-decision', 'role': 'user', 'content': text})
        doc = self.apply(intent='reference', source_message_id='later-decision', source_quote=text)
        self.assertEqual(doc['device_references'][0]['intent'], 'reference')


if __name__ == '__main__':
    unittest.main()
