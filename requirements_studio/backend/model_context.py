"""Compact model-only projections; stored requirements and evidence stay intact."""
from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path
from .convergence import FIELDS
from .coverage import CATALOG
from .module_catalog import CATALOG as MODULES,CATEGORIES,TYPES


def field_catalog():
    return [{'topic_id':t['id'],'title':t['title'],
             'fields':[[fid,f['label']] for fid,f in FIELDS.items() if f['topic_id']==t['id']]}
            for t in CATALOG]


def module_catalog():
    return {'categories':{c['id']:c['name'] for c in CATEGORIES.values()},
            'parents':[{'id':p['id'],'name':p['name'],'category_id':p['category_id'],
                        'functional_contract':p.get('description',''),
                        'abstract_roles':[port['role'] for port in p.get('ports',[])],
                        'children':[[c['id'],c['name']] for c in MODULES['modules'] if c['parent_id']==p['id']]}
                       for p in MODULES['parents']]}


def compact(context):
    result=copy.deepcopy(context)
    result['field_catalog']=field_catalog()
    result['module_catalog']=module_catalog()
    result['coverage_catalog']=[] # Superseded by the complete field catalog above.
    result['coverage_review']={k:v for k,v in result['coverage_review'].items()
                               if k not in ('topics','gaps','engineering_gaps')}
    document=result['current_document']
    for key in ('selection_items','convergence_history','domain_impacts','changed_ids','updated_at','created_at',
                'next_module_number','next_requirement_number','next_device_reference_number','coverage'):
        document.pop(key,None)
    document['question_history']=[{k:v for k,v in q.items() if k in ('id','question','decision_key','resolved_by')}
                                  for q in document.get('question_history',[])[-30:]]
    quotes={}
    def intern(value):
        if isinstance(value,list):return [intern(v) for v in value]
        if not isinstance(value,dict):return value
        answer={}
        for key,item in value.items():
            if key=='source_quote' and isinstance(item,str) and len(item)>60:
                qid='Q-'+hashlib.sha256(item.encode()).hexdigest()[:12]
                quotes[qid]=item;answer['source_quote_ref']=qid
            else:answer[key]=intern(item)
        return answer
    result=intern(result)
    result['evidence_quote_table']=quotes
    aliases={m['id']:f'M-{i:03d}' for i,m in enumerate((m for m in result['messages'] if m['role']=='user'),1)}
    def encode_ids(value):
        if isinstance(value,list):return [encode_ids(v) for v in value]
        if not isinstance(value,dict):return value
        return {k:aliases.get(v,v) if k=='source_message_id' and isinstance(v,str) else encode_ids(v) for k,v in value.items()}
    result=encode_ids(result)
    for message in result['messages']:
        message['id']=aliases.get(message['id'],message['id'])
    if 'current_message_id' in result:result['current_message_id']=aliases.get(result['current_message_id'],result['current_message_id'])
    result['message_aliases']={alias:real for real,alias in aliases.items()}
    # Select an owned span instead of asking the model to retype long user text.
    # The response contract enumerates these selectors; ownership is restored
    # from this server-built index, never inferred from generated prose.
    anchors={}
    for message in result['messages']:
        if message['role']!='user':continue
        text=message['content'];spans=[]
        if text:
            if len(text)<=3000:spans.append((0,len(text)))
            else:
                for start in range(0,len(text),2250):
                    spans.append((start,min(start+2500,len(text))))
                    if spans[-1][1]==len(text):break
        for quote in quotes.values():
            if not quote or len(quote)>3000:continue
            start=text.find(quote)
            if start>=0:spans.append((start,start+len(quote)))
        for start,end in dict.fromkeys(spans):
            span=text[start:end]
            # Stable across context trimming and retries: a cached selector must
            # never acquire another owner's span when M aliases are renumbered.
            real=result['message_aliases'][message['id']]
            anchors[f'S-{real}-{start}-{end}']={
                'message_id':message['id'],'start':start,'end':end,
                'sha256':hashlib.sha256(span.encode()).hexdigest(),'text':span}
    result['source_anchor_index']=anchors
    return result


def expand_sources(result,context,user_sources):
    expanded=[]
    for group in ('updates','coverage','field_resolutions','module_updates','numeric_constraints','device_reference_updates','device_constraints'):
        for index,row in enumerate(getattr(result,group)):
            alias=row.source_message_id
            quote=row.source_quote
            anchor=None
            if quote.startswith('@S-'):
                anchor=context.get('source_anchor_index',{}).get(quote[1:])
                if not isinstance(anchor,dict):raise ValueError('source_quote 引用了不存在的原话锚点，请选择source_anchor_index中的@S编号。')
                owner=anchor.get('message_id')
                real=context.get('message_aliases',{}).get(owner)
                if real not in user_sources or alias not in ('',owner,real):
                    raise ValueError('source_message_id 与原话锚点的用户消息归属不一致。')
                start,end=anchor.get('start'),anchor.get('end');original=user_sources[real]
                if type(start) is not int or type(end) is not int or not 0<=start<end<=len(original) or end-start>3000:
                    raise ValueError('原话锚点范围无效，不能提交来源。')
                span=original[start:end]
                if anchor.get('text')!=span or anchor.get('sha256')!=hashlib.sha256(span.encode()).hexdigest():
                    raise ValueError('原话锚点与真实用户消息不一致，不能提交来源。')
                row.source_message_id=real;row.source_quote=span
            else:
                row.source_message_id=context.get('message_aliases',{}).get(alias,alias)
            if quote=='@message':
                if row.source_message_id not in user_sources:raise ValueError('引文必须指向真实用户消息。')
                original=user_sources[row.source_message_id]
                if len(original)>3000:raise ValueError('源消息超过3000字，请使用已有@Q引文或连续原话片段，不要求用户重述。')
                row.source_quote=original
            elif quote.startswith('@Q-'):
                if quote[1:] not in context.get('evidence_quote_table',{}):raise ValueError('引用了不存在的原话锚点。')
                row.source_quote=context['evidence_quote_table'][quote[1:]]
            if alias!=row.source_message_id or row.source_quote!=quote:
                note={'path':f'{group}[{index}]','message_alias':alias,'source_message_id':row.source_message_id,'quote_selector':quote if quote.startswith('@') else 'literal'}
                if anchor:note.update(start=anchor['start'],end=anchor['end'],sha256=anchor['sha256'])
                expanded.append(note)
    return expanded


def normalize_abstract_ports(result,document):
    types={m['id']:m['type_id'] for m in document.get('modules',[])}
    types.update({m.id:m.type_id for m in result.module_updates})
    notes=[]
    for link in result.module_links:
        for end in ('source','target'):
            kind=TYPES.get(types.get(getattr(link,end)))
            if kind is None:continue # Unknown endpoints/type remain a hard contract error.
            field=end+'_port';port=getattr(link,field)
            if port and port not in {p['role'] for p in kind.get('ports',[])}:
                notes.append({'path':'module_links.'+link.id+'.'+field,'deferred_value':port,'reason':'不属于该类别的抽象角色，保留为设计待核对。'})
                setattr(link,field,'');link.state='design_stage'
                if '抽象端口待设计核对' not in link.description:link.description+='（抽象端口待设计核对）'
    return notes


def release_hash():
    root=Path(__file__).parent
    files=sorted([*root.glob('*.py'),*root.glob('resources/*.json')])
    return hashlib.sha256(b''.join(str(p.relative_to(root)).encode()+b'\0'+p.read_bytes() for p in files)).hexdigest()
