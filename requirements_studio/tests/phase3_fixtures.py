"""Explicit controlled data. Never loaded by the ordinary studio launcher."""
from __future__ import annotations
import copy
import time
import uuid
from backend.domain import empty_document,now
from backend.experience import CORE
from backend.device_domain import reconcile_selection_items


class FixtureCatalog:
    enabled=True
    def __init__(self,delay=0):self.delay=delay;self.calls=[];self.fail=False
    def status(self,refresh=False):
        return {'available':not self.fail,'read_only':True,'published_devices':2,'manual_records':1,'freshness':'fixture',
                'checked_at':now(),'release':{'release_code':'controlled_fixture','published_at':now()}}
    def invalidate(self,refs):pass
    def taxonomy(self):return {'version':'fixture','nodes':[]}
    def identity(self,model='TPS5450DDAR',key='datas:source-device:8883',maker='Fixture TI'):
        number=key.rsplit(':',1)[1];supplier='C114425' if number=='8883' else 'C'+number
        return {'device_key':key,'model':model,'manufacturer':maker,'supplier_part':supplier,
                'supplier_namespace':'lcsc','package':'DDA','leaf_code':'l_step_down_dc_dc_converter','leaf_name':'降压型DC-DC转换器',
                'taxonomy_path':'fixture / 电源','source_locator':'fixture.devices:'+number,'document_locator':'DataSheetBaseV2.DataSheet:lichuang_id='+supplier,
                'origin':'controlled_fixture','device_form':'IC','identity_warning':'','build_id':'fixture','checksum':'fixture'}
    def search(self,text,mode='auto',limit=8,offset=0,leaf_codes=None):
        if self.fail:raise RuntimeError('controlled source offline')
        if text.upper()=='LM358':items=[self.identity('LM358','datas:source-device:100','Fixture A'),self.identity('LM358','datas:source-device:101','Fixture B')]
        elif text.upper()=='BSS138':items=[self.identity('BSS138','datas:source-device:200','Fixture MOS')]
        elif 'ABSENT' in text.upper():items=[]
        else:items=[self.identity()]
        return {'query':text,'status':'ambiguous' if len(items)>1 else 'matched' if items else 'not_found',
                'match_mode':'model_exact','items':items,'has_more':False,'offset':offset,'next_offset':offset+limit,'read_only':True,'checked_at':now()}
    def device(self,key):
        self.calls.append(key)
        raw=[{'field_key':'input_voltage_range','field_name_zh':'输入电压范围','field_value_text':'5.5V~36V','field_value_num':None,'field_unit':'V','evidence_status':'PROVEN','priority_rank':1},
             {'field_key':'switching_frequency','field_name_zh':'开关频率','field_value_text':'500kHz','field_value_num':'500','field_unit':'Hz','evidence_status':'PROVEN','priority_rank':4},
             {'field_key':'price','field_name_zh':'单价','field_value_text':None,'field_value_num':None,'field_unit':None,'evidence_status':'EXPECTED_BUT_MISSING','is_missing':1,'priority_rank':5}]
        from backend.engineering_checks import normalize_attribute
        identity=self.identity(key=key)
        if key.endswith(':100') or key.endswith(':101'):
            identity=self.identity('LM358',key,'Fixture A' if key.endswith(':100') else 'Fixture B')
            identity['supplier_part']='C'+key.rsplit(':',1)[1]
        elif key.endswith(':200'):identity=self.identity('BSS138',key,'Fixture MOS')
        return {'identity':identity,'assertions':[normalize_attribute(r) for r in raw],
                'checked_at':now(),'version_context':{'build_id':'fixture','taxonomy_versions':['51'],'checksum':'fixture'}}
    def resolve(self,ref):
        if self.delay:time.sleep(self.delay)
        if ref.get('device_key'):
            device=self.device(ref['device_key'])
            return {'status':'matched','match_mode':'user_identity','items':[device['identity']],'device':device}
        found=self.search(ref['mention'])
        if found['status']=='matched' and ref['identity_scope']!='family':found['device']=self.device(ref.get('device_key') or found['items'][0]['device_key'])
        return found
    def spans(self,identity,topics=None):
        if self.fail:raise RuntimeError('controlled document offline')
        long_text=('\n受控长片段：'+('REFERENCE_WITH_A_VERY_LONG_PART_SUFFIX_1234567890'*120)) if identity['model']=='BSS138' else ''
        return {'status':'available','items':[{'id':'469187396438325949','lichuang_id':identity['supplier_part'],'database':'DataSheetBaseV2','collection':'DataSheet',
                'title':'受控手册第1页','content':'受控测试手册：宽输入电压范围为：5.5V至36V。开关频率500kHz。'+long_text,
                'page_number':1,'page_idx':0,'source_run':'controlled_fixture','ingested_at':'2026-10-16T00:00:00Z','retrieval_mode':'fixture'}]}


def seed(store,title='受控领域案例',include_constraint=False,ambiguous=True):
    p=store.create_project();pid=p['id'];mid=uuid.uuid4().hex
    quote='用于室内实验室电源测试，采用TPS5450DDAR，VIN直接输入5V；LM358仅作参考，价格未指定。确认电源转换模块的类别、用途和一个独立电路，具体外围待设计。'
    d=empty_document();d.update(title=title,summary=quote,convergence_version=2,domain_schema_version=1)
    d['requirements']=[{'id':'REQ-001','category':'functions','name':'室内电源功能','value':quote,'unit':'','state':'stated','priority':'must','implementation':'hardware',
                        'source_message_id':mid,'source_quote':quote,'source_type':'user','updated_at':now()}]
    d['field_resolutions']=[{'field_id':fid,'state':'confirmed','conclusion':quote,'requirement_ids':['REQ-001'],'source_message_id':mid,'source_quote':quote} for fid in CORE]
    d['modules']=[{'id':'MOD-001','type_id':'P_1_1','name':'实验电源转换','purpose':'为室内实验提供电源转换功能','boundary':'具体器件外围和热设计待设计核对。','quantity':1,
                   'state':'confirmed','requirement_ids':['REQ-001'],'source_message_id':mid,'source_quote':quote}]
    d['flow']=[{'label':'接入电源并提供输出','requirement_ids':['REQ-001']}]
    d['device_references']=[{'id':'REF-001','mention':'TPS5450DDAR','intent':'must','identity_scope':'exact_part','purpose':'电源转换','requirement_ids':['REQ-001'],
                            'module_ids':['MOD-001'],'quantity':None,'source_message_id':mid,'source_quote':quote,'lifecycle':'active','device_key':None}]
    if ambiguous:d['device_references'].append({**d['device_references'][0],'id':'REF-002','mention':'LM358','intent':'reference','purpose':'模拟参考'})
    if include_constraint:d['device_constraints']=[{'id':'DC-001','reference_id':'REF-001','requirement_id':'REQ-001','field_key':'input_voltage','kind':'design_requirement',
      'relation':'covers','minimum':5,'maximum':5,'text':'','unit':'V','port':'VIN','conditions':'VIN直接接5V','source_message_id':mid,'source_quote':quote}]
    reconcile_selection_items(d)
    with store.connect() as db:
        db.execute('INSERT INTO messages VALUES(?,?,?,?,?)',(mid,pid,'user',quote,now()));store._save(db,pid,d,1)
    return store.get_project(pid)
