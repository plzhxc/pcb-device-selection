"""One final model review of the actual reference-backed product architecture."""
import hashlib,json

TASK='最终参考能力复审：'
def signature(doc):
    groups={'requirements':('id','category','name','value','unit','state','implementation','priority'),
            'modules':('id','type_id','name','purpose','boundary','quantity','state','requirement_ids'),
            'device_references':('id','mention','intent','identity_scope','device_key','quantity','requirement_ids','module_ids','purpose','lifecycle'),
            'module_links':('id','source','target','kind','protocol','signals','voltage','rate','boundary','role','conditions','requirement_ids','description','state'),
            'external_interfaces':('id','name','purpose','external_object','protocol','signals','voltage','shared_link','state')}
    value={key:[{field:r.get(field) for field in fields} for r in doc.get(key,[])] for key,fields in groups.items()}
    value['sources']=[s['sha256'] for s in doc.get('reference_designs',[])];value['workflow']=doc.get('flow',[])
    value['constraints']={k:doc.get(k,[]) for k in ('numeric_constraints','device_constraints')}
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def pending(doc):
    return bool(doc.get('delivery_policy_version') and doc.get('reference_designs') and (doc.get('reference_audit') or {}).get('signature')!=signature(doc))

def record(doc,result,run):
    if run['kind']!='continuation' or TASK not in run.get('continuation_task',''):return
    if {r.key for r in result.delivery_checks}!={'scope_acceptance','capability_chain','interfaces_power','core_parts'}:
        raise ValueError('最终参考复审需明确返回功能验收、能力链、接口供电和核心方案四项检查；没有结论不能结束。')
    doc['reference_audit']={'signature':signature(doc),'run_id':run['id'],'scope':'需求与参考方案复审；不代表电气验证或生产BOM定型。'}
