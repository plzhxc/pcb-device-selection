import copy
import hashlib
import sys
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from backend.domain import AnalysisResult,analysis_schema,empty_document
from backend.model_context import compact,expand_sources


class SourceAnchorTests(unittest.TestCase):
    def context(self,texts,quote=''):
        document=empty_document()
        if quote:document['requirements']=[{'source_quote':quote}]
        source={'current_document':document,'coverage_review':{},'messages':[
            {'id':f'user-{i}','role':'user','content':text} for i,text in enumerate(texts)]}
        original=copy.deepcopy(source);result=compact(source)
        self.assertEqual(source,original)
        return result,{m['id']:m['content'] for m in source['messages']}

    def result(self,selector,source=''):
        return AnalysisResult.model_validate(dict(reply='已记录',title='测试',summary='',updates=[dict(
            id='new-1',category='functions',name='功能',value='范围',unit='',priority='must',state='stated',
            source_message_id=source,source_quote=selector)],remove_ids=[],questions=[],flow=[],changes=[]))

    def test_repeated_answers_keep_distinct_message_ownership(self):
        context,users=self.context(['确认。','确认。'])
        self.assertEqual(set(context['source_anchor_index']),{'S-user-0-0-3','S-user-1-0-3'})
        for n in (1,2):
            result=self.result(f'@S-user-{n-1}-0-3');notes=expand_sources(result,context,users)
            self.assertEqual(result.updates[0].source_message_id,f'user-{n-1}')
            self.assertEqual(result.updates[0].source_quote,'确认。')
            self.assertEqual(notes[0]['sha256'],hashlib.sha256('确认。'.encode()).hexdigest())

    def test_long_unicode_message_is_fully_covered_and_historical_span_retained(self):
        text=''.join(f'第{i:04d}项：保留中文、5V和换行。\n' for i in range(600))
        historical=text[2150:2850]
        context,users=self.context([text],historical);covered=bytearray(len(text))
        for key,anchor in context['source_anchor_index'].items():
            self.assertLessEqual(len(anchor['text']),3000)
            covered[anchor['start']:anchor['end']]=b'1'*(anchor['end']-anchor['start'])
            result=self.result('@'+key);expand_sources(result,context,users)
            self.assertEqual(result.updates[0].source_quote,text[anchor['start']:anchor['end']])
        self.assertTrue(all(covered))
        self.assertTrue(any(a['text']==historical for a in context['source_anchor_index'].values()))
        self.assertEqual(context['messages'][0]['content'],text)

    def test_unknown_anchor_wrong_owner_or_changed_span_cannot_commit(self):
        context,users=self.context(['USB-C输入5V。','确认。'])
        with self.assertRaisesRegex(ValueError,'不存在'):expand_sources(self.result('@S-999-001'),context,users)
        selector=next(iter(context['source_anchor_index']))
        with self.assertRaisesRegex(ValueError,'归属'):expand_sources(self.result('@'+selector,'M-002'),context,users)
        for key,value in [('start',-1),('end',5000),('sha256','forged'),('text','被改写原话')]:
            bad=copy.deepcopy(context);bad['source_anchor_index'][selector][key]=value
            with self.assertRaises(ValueError):expand_sources(self.result('@'+selector),bad,users)
        with self.assertRaisesRegex(ValueError,'不一致'):
            expand_sources(self.result('@'+selector),context,{'user-0':'USB-C输入9V。','user-1':'确认。'})

    def test_selector_keeps_owner_when_older_context_is_trimmed(self):
        source={'current_document':empty_document(),'coverage_review':{},'messages':[
            {'id':'older','role':'user','content':'确认。'},
            {'id':'latest','role':'user','content':'改为USB供电。'}]}
        full=compact(source);source['messages']=source['messages'][1:];trimmed=compact(source)
        selector=next(key for key,a in full['source_anchor_index'].items() if a['message_id']=='M-002')
        self.assertIn(selector,trimmed['source_anchor_index'])
        self.assertEqual(trimmed['source_anchor_index'][selector]['message_id'],'M-001')
        result=self.result('@'+selector);expand_sources(result,trimmed,{'latest':'改为USB供电。'})
        self.assertEqual(result.updates[0].source_message_id,'latest')
        with self.assertRaisesRegex(ValueError,'不存在'):
            expand_sources(self.result('@S-older-0-3'),trimmed,{'latest':'改为USB供电。'})

    def test_schema_enumerates_selectors_and_legacy_contract_stays_supported(self):
        schema=analysis_schema(empty_document(),['M-001'],['@S-001-001'])
        for definition in schema['$defs'].values():
            props=definition.get('properties',{})
            if 'source_message_id' in props:self.assertEqual(props['source_message_id']['enum'],[''])
            if 'source_quote' in props:
                self.assertIn('@S-001-001',props['source_quote']['enum'])
                self.assertEqual('' in props['source_quote']['enum'],props['source_quote'].get('minLength',0)==0)
        legacy=analysis_schema(empty_document(),['M-001'])
        self.assertEqual(legacy['$defs']['RequirementUpdate']['properties']['source_message_id']['enum'],['','M-001'])
        self.assertNotIn('enum',legacy['$defs']['RequirementUpdate']['properties']['source_quote'])


if __name__=='__main__':unittest.main()
