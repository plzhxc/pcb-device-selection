"""A customer's finite pronoun answer must neither fail nor widen its scope."""
import copy
from types import SimpleNamespace
import unittest

from backend.device_domain import DeviceReferenceUpdate, apply_updates


class DeviceIntentContextTests(unittest.TestCase):
    def setUp(self):
        self.question = {'id': 'Q-001', 'question': '这两个候选型号怎样取舍？',
                         'requirement_ids': ['REQ-001', 'REQ-002'], 'status': 'pending'}
        self.doc = {'requirements': [{'id': f'REQ-00{i}', 'state': 'stated'} for i in range(1, 4)],
                    'modules': [], 'questions': [self.question], 'device_constraints': [],
                    'device_references': [self.reference(i, name) for i, name in
                                          [(1, 'RADIO-A'), (2, 'RADIO-B'), (3, 'POWER-C')]]}
        self.answer = '那就暂不锁定这两个型号，由设计补齐资料比较后决定。'
        self.text = '关于「' + self.question['question'] + '」：' + self.answer
        self.messages = [{'id': 'original', 'role': 'user', 'content': '采用RADIO-A、RADIO-B和POWER-C。'},
                         {'id': 'decision', 'role': 'user', 'content': self.text}]

    @staticmethod
    def reference(i, name):
        return {'id': f'REF-00{i}', 'mention': name, 'intent': 'must', 'identity_scope': 'exact_part',
                'purpose': '原用途', 'requirement_ids': [f'REQ-00{i}'], 'module_ids': [], 'quantity': i,
                'source_message_id': 'original', 'source_quote': '采用RADIO-A、RADIO-B和POWER-C。',
                'lifecycle': 'active', 'device_key': 'identity-' + str(i), 'identity_decision': None}

    def apply(self, indexes=(0, 1), **changes):
        updates = []
        for i in indexes:
            row = {k: v for k, v in self.doc['device_references'][i].items()
                   if k in DeviceReferenceUpdate.model_fields}
            row.update(intent='undecided', source_message_id='decision', source_quote=self.text)
            row.update(changes)
            updates.append(DeviceReferenceUpdate(**row))
        doc = copy.deepcopy(self.doc)
        apply_updates(doc, SimpleNamespace(device_reference_updates=updates,
                                          remove_device_reference_ids=[], device_constraints=[]),
                      self.doc, self.messages, {}, {})
        return doc

    def test_answer_resolves_only_the_two_saved_candidates_and_retains_identity_origin(self):
        doc = self.apply()
        self.assertEqual([r['intent'] for r in doc['device_references']], ['undecided', 'undecided', 'must'])
        self.assertEqual(doc['device_references'][0]['mention_source']['source_message_id'], 'original')
        self.assertEqual(doc['device_references'][0]['source_message_id'], 'decision')
        self.assertEqual(doc['device_references'][2], self.doc['device_references'][2])

    def test_a_bounded_intent_answer_cannot_change_quantity_identity_or_associations(self):
        doc = self.apply((0,), quantity=999, identity_scope='family', requirement_ids=['REQ-003'], purpose='新用途')
        row = doc['device_references'][0]
        self.assertEqual(row['quantity'], 1)
        self.assertEqual(row['identity_scope'], 'exact_part')
        self.assertEqual(row['device_key'], 'identity-1')
        self.assertEqual(row['requirement_ids'], ['REQ-001'])
        self.assertEqual(row['purpose'], '原用途')
        with self.assertRaisesRegex(ValueError, '器件型号'):
            self.apply((0,), mention='INVENTED-D')

    def test_the_same_answer_cannot_unlock_an_unrelated_power_part_or_force_a_model(self):
        with self.assertRaisesRegex(ValueError, '器件型号'):
            self.apply((2,))
        with self.assertRaisesRegex(ValueError, '器件型号'):
            self.apply((0,), intent='must')

    def test_wrong_candidate_count_or_missing_question_does_not_guess(self):
        self.question['requirement_ids'].append('REQ-003')
        with self.assertRaisesRegex(ValueError, '器件型号'):
            self.apply()
        self.doc['questions'] = []
        with self.assertRaisesRegex(ValueError, '器件型号'):
            self.apply()

    def test_free_text_is_supported_only_with_one_pending_question(self):
        self.text = self.answer
        self.messages[-1]['content'] = self.text
        self.assertEqual(self.apply()['device_references'][0]['intent'], 'undecided')
        self.doc['questions'].append({'id': 'Q-002', 'question': '另一个问题', 'requirement_ids': ['REQ-003']})
        with self.assertRaisesRegex(ValueError, '器件型号'):
            self.apply()

    def test_a_saved_delegation_remains_readable_after_the_question_is_resolved(self):
        self.doc = self.apply()
        self.doc['questions'] = []
        again = self.apply(quantity=999, purpose='未经确认的新用途')
        self.assertEqual(again['device_references'], self.doc['device_references'])

    def test_an_unrelated_answer_or_negated_consent_does_not_supply_authority(self):
        self.text = '关于「另一个问题」：' + self.answer
        self.messages[-1]['content'] = self.text
        with self.assertRaisesRegex(ValueError, '器件型号'):
            self.apply()
        self.text = '我没说这两个型号不锁定，仍要按原型号。'
        self.messages[-1]['content'] = self.text
        with self.assertRaisesRegex(ValueError, '器件型号'):
            self.apply()

    def test_forged_quote_is_still_rejected(self):
        with self.assertRaisesRegex(ValueError, '真实用户原话'):
            self.apply(source_quote='先不锁定这两个型号，请设计比较。')


if __name__ == '__main__':
    unittest.main()
