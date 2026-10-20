"""Bounded, non-executing reference ENET/BOM intake and traceable BOM projection."""
from __future__ import annotations
import base64,csv,hashlib,io,json,re
from pathlib import PurePath

MAX_BYTES=2*1024*1024

def model_name(value):
    return re.sub(r'_C[0-9]+$','',str(value).strip(),flags=re.I)

def parse_reference(filename,encoded):
    filename=PurePath(filename.replace('\\','/')).name
    if not filename or len(filename)>180 or not filename.lower().endswith(('.enet','.json','.csv')):raise ValueError('请上传 ENET 网表或 BOM CSV 文件。')
    try:raw=base64.b64decode(encoded,validate=True)
    except ValueError as exc:raise ValueError('附件内容无法解码。') from exc
    if not 1<=len(raw)<=MAX_BYTES:raise ValueError('参考文件需在 2 MiB 以内。')
    text=None
    for encoding in ('utf-8-sig','gb18030'):
        try:text=raw.decode(encoding);break
        except UnicodeDecodeError:pass
    if text is None:raise ValueError('参考文件应为 UTF-8 或 GB18030 文本。')
    components=[];warnings=[];kind='bom_csv'
    clean=lambda value:str(value or '').strip()[:300]
    if filename.lower().endswith(('.enet','.json')):
        try:data=json.loads(text)
        except (ValueError,RecursionError) as exc:raise ValueError('ENET 应为合法 JSON 网表。') from exc
        if not isinstance(data,dict) or not isinstance(data.get('components'),dict):raise ValueError('没有找到 ENET components；PCB 布局 JSON 不能替代原理图网表。')
        if len(data['components'])>2000:raise ValueError('本轮参考最多接收 2000 个元件对象。')
        kind='enet'
        for key,c in data['components'].items():
            if not isinstance(c,dict):continue
            p=c.get('props',{})
            if not isinstance(p,dict):raise ValueError('参考元件属性应为对象，请核对网表格式。')
            if str(p.get('Add into BOM','yes')).lower() in ('no','false','0'):continue
            name=next((clean(p.get(k)) for k in ('Manufacturer Part','DeviceName','Name','Value') if p.get(k) and not str(p[k]).startswith('=')),'')
            if not name:name='未标型号'
            pins=[]
            pinmap=c.get('pinInfoMap',{})
            if not isinstance(pinmap,dict) or len(pinmap)>1000:raise ValueError('参考元件引脚结构超出解析范围。')
            for pin_id,pin in pinmap.items():
                if isinstance(pin,dict) and pin.get('net'):pins.append({'pin':clean(pin.get('number',pin_id)),'signal':clean(pin.get('name')),'net':clean(pin['net']),'designator':clean(p.get('Designator',key)),'instance':clean(key)})
            components.append({'instance':clean(key),'designator':clean(p.get('Designator',key)),'model':model_name(name),'original_model':name,
                'supplier_part':clean(p.get('Supplier Part')) or (re.search(r'_(C[0-9]+)$',name,re.I).group(1) if re.search(r'_(C[0-9]+)$',name,re.I) else ''),
                'package':clean(p.get('FootprintName') or p.get('3D Model Title') or p.get('Supplier Footprint')),
                'manufacturer':clean(p.get('Manufacturer')),'quantity':1,'pins':pins,'role':'','channel':clean(p.get('Channel ID'))})
    else:
        reader=csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:raise ValueError('CSV 没有表头。')
        def column(row,names):return next((clean(row.get(n)) for n in names if row.get(n)), '')
        for i,row in enumerate(reader):
            if i>=2000:raise ValueError('参考 BOM 最多接收 2000 行。')
            name=column(row,('名称','型号','Manufacturer Part','Part Number','Name','Comment','元件名称'))
            if not name:continue
            refs=column(row,('位号','Designator','Designators','Reference','元件位号'))
            qty=column(row,('数量','Quantity','Qty')) or str(len([r for r in re.split('[,，、 ]+',refs) if r]) or 1)
            if not re.fullmatch(r'[1-9][0-9]{0,4}',qty):raise ValueError('BOM 数量必须为正整数，请核对第 '+str(i+2)+' 行。')
            components.append({'instance':'csv-'+str(i+1),'designator':refs,'model':model_name(name),'original_model':name,
                'supplier_part':column(row,('采购号','供应商编号','LCSC Part #','Supplier Part')),'package':column(row,('封装','Footprint','Package')),
                'manufacturer':column(row,('制造商','Manufacturer')),'quantity':int(qty),'pins':[],'role':column(row,('功能','Function','Role')),'channel':''})
    if not components:raise ValueError('文件中没有可解析的 BOM 元件。')
    grouped={};seen_refs=set()
    for c in components:
        if c['designator'] in seen_refs:warnings.append('存在重复位号，观测实例数量不能直接视为层次展开后的实际用量。')
        seen_refs.add(c['designator'])
        key=(c['model'],c['package'],c['supplier_part'])
        core=bool(re.match(r'^(?:U|IC|Q|LDO|MIC|ANT|X|H|CN|USB|BAT|SPK|IIC)[0-9]',c['designator'],re.I)) or bool(re.search(r'芯片|MCU|稳压器|MOS|连接器|RF器件|晶振',c['role'],re.I))
        if re.search(r'螺丝|螺柱|安装孔|测试点|TestPoint|^(?:TP|PAD|未标型号)$',c['model'],re.I):core=False
        if key not in grouped:grouped[key]={'key':'PART-'+hashlib.sha256(json.dumps(key,ensure_ascii=False).encode()).hexdigest()[:16],
            **{k:c[k] for k in ('model','original_model','supplier_part','package','manufacturer','role')},'designators':[],'observed_quantity':0,'core':core,'pins':[]}
        g=grouped[key];g['designators'].append(c['designator']);g['observed_quantity']+=c['quantity'];g['pins']+=c['pins'][:100]
    bom=list(grouped.values())
    return {'id':'SRC-'+hashlib.sha256(raw).hexdigest()[:20],'filename':filename,'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'kind':kind,
            'bom':bom,'instance_count':sum(c['quantity'] for c in components),'warnings':list(dict.fromkeys(warnings)),
            'quantity_basis':'文件中可见实例/CSV行数量；没有推断隐藏复用实例，也不等于本版已采用的生产BOM。',
            'original_content_base64':encoded}

def reference_message(source):
    lines=['[参考资料] '+source['filename'],'这是用户提供的设计参考，以下是从文件实际读取的核心器件；不是本版强制器件或已验证电气参数。']
    for p in [p for p in source['bom'] if p['core']][:80]:
        lines.append('- '+','.join(p['designators'])+'：'+p['model']+'；参考观测数量 '+str(p['observed_quantity'])+('；采购号 '+p['supplier_part'] if p['supplier_part'] else ''))
    lines.append('请按已确定用途分析哪些适用，提出容易理解的方案和取舍；我没有要求照搬所有器件。')
    return '\n'.join(lines)

def qualified_reference(source):
    """Recover per-instance pin ownership for already saved attachments too.

    This is a read-only projection of the retained original, not a rewrite of
    the historical quantity, classification, identity, or customer decision.
    """
    if source.get('kind')!='enet' or not source.get('original_content_base64'):return source
    if all('designator' in pin for part in source['bom'] for pin in part['pins']):return source
    parsed=parse_reference(source['filename'],source['original_content_base64']);by_key={p['key']:p for p in parsed['bom']}
    return {**source,'bom':[{**p,'pins':by_key.get(p['key'],p)['pins']} for p in source['bom']]}

def signal_connections(source):
    nets={}
    for part in source['bom']:
        if not part['core']:continue
        for pin in part['pins']:
            net=pin['net']
            if re.search(r'^(?:[AD]?GND|VCC|VDD|VBAT|VBUS|VIN|VOUT|BAT\+|USB_VBUS)(?:$|[_0-9.])',net,re.I):continue
            owner=(part['model'],pin.get('designator',','.join(part['designators'])))
            nets.setdefault(net,set()).add(owner)
    return [{'net':net,'endpoints':[{'model':model,'designator':ref} for model,ref in sorted(owners)]} for net,owners in nets.items() if len(owners)>1][:100]

def passive_connections(source):
    instances={}
    for part in source['bom']:
        for pin in part['pins']:
            ref=pin.get('designator','')
            if re.match(r'^(?:R|C|L|FB)[0-9]',ref,re.I):
                row=instances.setdefault(ref,{'designator':ref,'model':part['model'],'terminals':[]})
                row['terminals'].append({k:pin[k] for k in ('pin','signal','net')})
    return [r for r in instances.values() if len({p['net'] for p in r['terminals']})==2]

def prompt_reference(source):
    source=qualified_reference(source)
    passives=passive_connections(source)
    return {**{k:source.get(k) for k in ('id','filename','sha256','instance_count','warnings','quantity_basis','source_message_id')},
            'signal_connections':signal_connections(source),
            'passive_connections':passives[:300],'passive_connections_truncated':len(passives)>300,
            'topology_note':'共享网络只是直接同网摘要。电容/电阻等支路可能把不同网名连接；不能因核心两端网名不同就说参考缺连接。电容耦合不等于直流导通，拓扑记录不代表偏置、波形或电气适用性已验证；参考外围参数不是用户硬要求。',
            'core_parts':[{**{k:p[k] for k in ('key','model','supplier_part','package','observed_quantity','designators','role')},'pins':p['pins'][:48]} for p in source['bom'] if p['core']][:80]}

def bom_view(doc,domain=None):
    refs=[r for r in (domain or {}).get('references',doc.get('device_references',[])) if r.get('lifecycle','active')=='active']
    rows=[]
    for source in doc.get('reference_designs',[]):
        for part in source['bom']:
            matches=[r for r in refs if model_name(r['mention']).casefold()==part['model'].casefold()]
            ref=next((r for r in matches if r.get('module_ids')),matches[0] if matches else {})
            used=bool(ref.get('module_ids') and ref.get('intent') in ('must','prefer'))
            proposed=bool(ref.get('module_ids') and not (ref.get('intent')=='reference' and ref.get('intent_source')))
            rows.append({**part,'source_id':source['id'],'source_filename':source['filename'],'reference_id':ref.get('id',''),'module_ids':ref.get('module_ids',[]),
                         'status':'adopted' if used else 'proposed' if proposed else 'reference',
                         'quantity':ref.get('quantity') if used else None,'resolution_status':ref.get('resolution_status','unresolved'),
                         'identity':ref.get('identity'),'purpose':ref.get('purpose',''),'quantity_basis':'已采用的参考方案' if ref.get('reference_quantity_source') else '用户给定' if used and ref.get('quantity') else '待本版实现核定'})
    current=[]
    for ref in refs:
        identity=ref.get('identity') or {}
        exact=ref.get('resolution_status')=='matched' or bool(ref.get('device_key'))
        reference=next((p for p in rows if p['reference_id']==ref['id'] and model_name(ref['mention']).casefold()==p['model'].casefold()),{})
        if not ref.get('module_ids') or ref['intent']=='undecided':continue
        if ref['intent']=='reference' and reference.get('status')!='proposed':continue
        current.append({'reference_id':ref['id'],'model':ref['mention'],'quantity':ref.get('quantity'),
                        'intent':ref['intent'],'status':'adopted' if ref['intent'] in ('must','prefer') else 'proposed',
                        'purpose':ref.get('purpose',''),'module_ids':ref['module_ids'],
                        'supplier_part':identity.get('supplier_part','') if exact else reference.get('supplier_part',''),
                        'package':identity.get('package','') if exact else reference.get('package',''),
                        'resolution_status':ref.get('resolution_status','unresolved'),
                        'source_label':'采用的参考方案' if ref.get('reference_quantity_source') else '用户当前决定' if ref.get('adoption_source') or ref['intent']=='must' else '已有方案，适用性待核验',
                        'reference_source_id':reference.get('source_id',''),
                        'reference_observed_quantity':reference.get('observed_quantity')})
    return {'sources':[{k:s.get(k) for k in ('id','filename','sha256','kind','instance_count','warnings','quantity_basis')} for s in doc.get('reference_designs',[])],
            'rows':rows,'core_count':sum(p['core'] for p in rows),'core_adopted':sum(p['core'] and p['status']=='adopted' for p in rows),
            'proposed_count':sum(p['core'] and p['status']=='proposed' for p in rows),'current_rows':current}

def current_bom_csv(rows):
    out=io.StringIO();writer=csv.writer(out)
    writer.writerow(['本版型号','本版数量','使用意图','当前状态','用途','采购号','封装','数量与方案来源'])
    labels={'must':'必须使用','prefer':'优先考虑','reference':'仅作参考'}
    for r in rows:writer.writerow([r['model'],r['quantity'] if r['quantity'] is not None else '待核定',labels.get(r['intent'],r['intent']),'已采用' if r['status']=='adopted' else '建议待采用',r['purpose'],r['supplier_part'],r['package'],r['source_label']])
    return ('\ufeff'+out.getvalue()).encode()

def bom_csv(rows):
    out=io.StringIO();writer=csv.writer(out);writer.writerow(['参考文件','位号','型号','采购号','封装','参考观测数量','本版数量','状态','本版用途','数量依据'])
    for p in rows:writer.writerow([p.get('source_filename',''),','.join(p['designators']),p['model'],p['supplier_part'],p['package'],p['observed_quantity'],p.get('quantity') or '',p.get('status','reference'),p.get('purpose',''),p.get('quantity_basis','')])
    return ('\ufeff'+out.getvalue()).encode()
