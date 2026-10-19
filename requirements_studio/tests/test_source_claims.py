import unittest
from backend.source_claims import source_regions,exclusive_claims,validate_document_claims

TEXT='设计PN532调试器，采用PN5321A3HN（NXP 13.56MHz NFC读写芯片，内置80C51内核，支持ISO14443A/B、ISO18092和FeliCa、NTAG全系列协议）作为核心，由RT9013双路LDO（1.2V内核+3.3V IO）提供低噪声电源，搭配TPS62933芯片（3A输出）。USB-C接口（供电与通信二合一），支持13.56MHz读写、ISO14443A/B自动识别、NTAG21x系列标签读写。'


class SourceClaimTests(unittest.TestCase):
    def setUp(self):self.messages=[{'id':'input','role':'user','content':TEXT}]
    def doc(self,value,category='functions'):
        return {'requirements':[{'id':'REQ-001','state':'stated','category':category,'value':value,'source_message_id':'input'}]}

    def test_part_introduction_split_keeps_real_usb_interface_requirement(self):
        regions=source_regions(TEXT)
        self.assertEqual([r['mention'] for r in regions['device_descriptions']],['PN5321A3HN','RT9013','TPS62933'])
        self.assertIn('供电与通信二合一',regions['main_text'])
        self.assertNotIn('FeliCa',regions['main_text'])

    def test_common_user_functions_are_not_suppressed_by_overlapping_chip_intro(self):
        validate_document_claims(self.doc('13.56MHz读写、ISO14443A/B识别及NTAG21x标签读写'),self.messages)
        extra=exclusive_claims(self.messages)
        self.assertIn('FELICA',extra);self.assertIn('ISO18092',extra)
        self.assertNotIn('ISO14443A/B',extra)

    def test_extra_protocol_cannot_be_promoted_to_product_requirement(self):
        with self.assertRaisesRegex(ValueError,'来源范围校验'):
            validate_document_claims(self.doc('支持ISO18092和FeliCa'),self.messages)
        validate_document_claims(self.doc('支持ISO14443A/B；ISO18092、FeliCa和NTAG全系列仅作为器件介绍，不列为本版整板功能承诺。'),self.messages)
        validate_document_claims(self.doc('RT9013介绍括号中的1.2V与3.3V IO不构成本版输出电压目标。'),self.messages)
        with self.assertRaisesRegex(ValueError,'来源范围校验'):
            validate_document_claims(self.doc('支持FeliCa；器件介绍不作为工程验证结果。'),self.messages)

    def test_chip_voltage_description_not_product_rail_and_quantity_unit_normalized(self):
        for value in ('提供1.2V内核电源','整板连续输出3000mA'):
            with self.assertRaisesRegex(ValueError,'来源范围校验'):validate_document_claims(self.doc(value),self.messages)

    def test_real_review_exclusion_wording_is_not_a_product_promise(self):
        for value in (
            '板级功能为ISO14443A/B；器件简介中的ISO18092、FeliCa和NTAG全系列不作为本版整板保证。',
            'ISO18092、FeliCa及NTAG全系列属于PN532器件介绍，不作为整板功能承诺。',
            'ISO18092和FeliCa来自芯片介绍；本版只实现ISO14443A/B。',
            'ISO18092、FeliCa及NTAG泛系列能力只保留为PN532器件介绍，不作为本版整板功能承诺。',
            '不把器件介绍中的ISO18092、FeliCa或NTAG泛系列扩为整板承诺。',
            '不要求FeliCa；本版实现ISO14443A/B。',
        ):validate_document_claims(self.doc(value),self.messages)
        with self.assertRaisesRegex(ValueError,'来源范围校验'):
            validate_document_claims(self.doc('支持FeliCa；ISO18092属于PN532器件介绍，不作为整板功能承诺。'),self.messages)
        with self.assertRaisesRegex(ValueError,'来源范围校验'):
            validate_document_claims(self.doc('支持FeliCa但不保证读写距离；器件简介不作为实际验证。'),self.messages)

    def test_device_description_remains_available_as_selection_information(self):
        validate_document_claims(self.doc('用户描述芯片支持FeliCa和1.2V电源，尚待手册核验','selection'),self.messages)
        validate_document_claims(self.doc('采用TPS62933芯片（用户描述3A输出）提供稳定供电，负载预算待设计'),self.messages)
        validate_document_claims(self.doc('采用TPS62933芯片，3A为用户提供的器件描述；整板负载待设计'),self.messages)
        validate_document_claims(self.doc('采用RT9013；用户描述1.2V和3.3V，实际电源轨待核验'),self.messages)
        with self.assertRaisesRegex(ValueError,'来源范围校验'):
            validate_document_claims(self.doc('采用TPS62933芯片（用户描述3A输出），整板必须提供3A输出'),self.messages)
        with self.assertRaisesRegex(ValueError,'来源范围校验'):
            validate_document_claims(self.doc('采用PN5321A3HN（用户描述支持FeliCa），需要全部实现'),self.messages)

    def test_later_explicit_user_requirement_can_adopt_chip_capability(self):
        messages=self.messages+[{'id':'choice','role':'user','content':'本版也需要支持FeliCa。'}]
        validate_document_claims(self.doc('支持FeliCa'),messages)
        messages=self.messages+[{'id':'choice','role':'user','content':'芯片介绍中的协议全部都需要实现。'}]
        doc=self.doc('支持FeliCa和ISO18092');doc['requirements'][0]['source_message_id']='choice'
        validate_document_claims(doc,messages)

    def test_negative_statement_does_not_authorize_a_new_protocol(self):
        messages=self.messages+[{'id':'choice','role':'user','content':'不要求FeliCa。'}]
        with self.assertRaisesRegex(ValueError,'来源范围校验'):validate_document_claims(self.doc('支持FeliCa'),messages)
        messages=self.messages+[{'id':'choice','role':'user','content':'关于「是否把FeliCa作为功能？」：不需要。'}]
        with self.assertRaisesRegex(ValueError,'来源范围校验'):validate_document_claims(self.doc('支持FeliCa'),messages)

    def test_embedded_core_does_not_make_a_communication_chip_a_generic_mcu(self):
        from backend.module_catalog import TYPES
        kind=next(k for k,v in TYPES.items() if v['name']=='8位单片机与8051类子模块')
        doc={'modules':[{'id':'MOD-001','type_id':kind,'state':'confirmed','name':'NFC处理','purpose':'完成卡片读写','source_message_id':'input'}],'device_references':[{'mention':'PN5321A3HN','module_ids':['MOD-001']}]}
        with self.assertRaisesRegex(ValueError,'通用主控'):validate_document_claims(doc,self.messages)
        messages=self.messages+[{'id':'choice','role':'user','content':'另外需要一个通用主控模块。'}]
        with self.assertRaisesRegex(ValueError,'通用主控'):validate_document_claims(doc,messages)
        doc['modules'][0].update(name='另外的通用主控模块',purpose='执行独立控制逻辑')
        doc['device_references']=[]
        validate_document_claims(doc,messages)

    def test_real_mcu_without_the_word_controller_is_not_blocked_by_its_core_intro(self):
        doc={'modules':[{'id':'MOD-001','type_id':'M_10_2_3','state':'confirmed','name':'STM32采集与处理电路','purpose':'采集并整理传感数据','source_message_id':'input'}]}
        messages=[{'id':'input','role':'user','content':'设计采集板，采用STM32F407VET6（Cortex-M4，168MHz）完成数据采集。'}]
        validate_document_claims(doc,messages)
        validate_document_claims(doc,messages+self.messages)
        messages=[{'id':'input','role':'user','content':'设计采集板，采用STM32F407VET6（Cortex-M4，支持UART通信）完成数据采集。'}]
        validate_document_claims(doc,messages+self.messages)

    def test_handoff_brief_separates_user_device_description_from_real_constraints(self):
        from backend.handoff import generation_brief
        spec=dict(title='调试器',summary='',requirements=[],constraints=[{'id':'hard','minimum':5,'unit':'V'},{'id':'chip-description','kind':'user_device_description','text':'器件介绍1.2V'}],modules=[],module_links=[],workflow=[],acceptance_criteria=[],open_items=[])
        brief=generation_brief(spec)
        self.assertLess(brief.index('hard'),brief.index('用户提供的器件介绍'))
        self.assertGreater(brief.index('chip-description'),brief.index('用户提供的器件介绍'))


if __name__=='__main__':unittest.main()
