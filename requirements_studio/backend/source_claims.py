"""Guard against promoting a part introduction into a product promise.

Only finite lexical claims with an explicit source boundary are checked. This
does not try to prove all natural-language semantics or electrical correctness.
"""
from __future__ import annotations
import re
from decimal import Decimal

from .knowledge import mentions
from .module_catalog import TYPES

PROTOCOL=re.compile(r'ISO\s*[-/]?\s*\d{4,6}(?:\s*[AB])?(?:\s*/\s*[AB])?|FeliCa|NTAG(?:全)?系列',re.I)
QUANTITY=re.compile(r'(?<![A-Za-z0-9.])([-+−]?\d+(?:\.\d+)?)\s*([kMG]Hz|MHz|Hz|[kMGT]i?B|mV|[uµμ]V|V|mA|[uµμ]A|A|mW|W|°C|℃)(?![A-Za-z0-9])',re.I)
PAREN=re.compile(r'（[^（）]{1,1400}）|\([^()]{1,1400}\)')
# Safe explanatory wording does not itself claim the chip description is a
# board requirement. Keep it visible, instead of deleting this information.
EXPLANATION=re.compile(r'器件(?:介绍|描述)|用户(?:介绍|描述)|不(?:直接)?(?:作为|构成).{0,15}(?:目标|要求|保证|承诺)|仅.{0,8}(?:参考|介绍)|按(?:原文|用户)描述.{0,25}(?:核验|核定|待设计)')


class SourceClaimError(ValueError):
    def __init__(self,failures):
        self.failures=failures
        super().__init__('来源范围校验：'+str(failures)[:1600]+'。器件介绍未被明确采用为产品要求；应保留描述与型号，修正正式承诺和关联分类。')


def answer_body(text):
    # The UI's quoted question is context, not something the customer asserted.
    return re.sub(r'关于「[^」]*」：','',text)


def atoms(text):
    result=set()
    for match in PROTOCOL.finditer(text):
        token=re.sub(r'\s+','',match.group()).upper()
        if token.startswith('NTAG'):token='NTAG泛系列'
        result.add(token)
    units={'V':('V','1'),'mV':('V','.001'),'A':('A','1'),'mA':('A','.001'),'W':('W','1'),'mW':('W','.001'),
           'Hz':('Hz','1'),'kHz':('Hz','1000'),'MHz':('Hz','1000000'),'GHz':('Hz','1000000000'),
           'KB':('B','1000'),'MB':('B','1000000'),'GB':('B','1000000000'),'KiB':('B','1024'),'MiB':('B','1048576'),
           '°C':('°C','1'),'℃':('°C','1')}
    for match in QUANTITY.finditer(text):
        unit,factor=units.get(match[2],(match[2].upper(),'1'))
        number=Decimal(match[1].replace('−','-'))*Decimal(factor)
        result.add(format(number.normalize(),'f')+unit)
    return result


def source_regions(text):
    names=mentions(text);descriptions=[];main=[];start=0
    for match in PAREN.finditer(text):
        prefix=text[max(0,match.start()-100):match.start()]
        # A nearby earlier part across a comma/sentence is not the owner.
        prefix=re.split(r'[，,。；;\n]',prefix)[-1]
        owners=[name for name in names if name in prefix]
        if not owners:continue
        owner=max(owners,key=lambda name:(prefix.rfind(name),len(name)))
        tail=prefix[prefix.rfind(owner)+len(owner):]
        # A network containing parts is the subject here, not the nearest chip.
        # Keep explicit board rails and interface goals in the product text.
        if re.search(r'组成|构成|电源(?:管理)?网络|系统|整板|板级|供电网络|输出接口',tail):continue
        if len(tail)>35 or re.search(r'要求|需要|必须|至少|接口|供电输入',tail):continue
        # Direct model parentheses and short device-class suffixes, e.g.
        # PN5321A3HN(...), RT9013双路LDO(...), MOSFET(...).
        if tail and not re.search(r'芯片|LDO|MOSFET|晶体管|三极管|运放|模块|器件|存储|Flash',tail,re.I):continue
        main.append(text[start:match.start()]);start=match.end()
        descriptions.append({'mention':owner,'start':match.start(),'end':match.end(),'text':match.group()})
    main.append(text[start:])
    return {'main_text':''.join(main),'device_descriptions':descriptions}


def claim_context(messages):
    regions=[]
    for message in messages:
        if message['role']!='user':continue
        row=source_regions(message['content'])
        if row['device_descriptions']:regions.append({'source_message_id':message['id'],**row})
    return {'regions':regions,'description_only_claims':sorted(exclusive_claims(messages)),
            'rule':'正文需求与器件括号介绍分开。型号继续约束设计，简介中的能力不是整板保证；没有明确采用的额外协议或内部电压不得升级为本版硬要求。产品功能条目以实际正文功能为主，简介内容在器件描述中集中保留，避免逐条重复排除清单。'}


def promise_atoms(text):
    """Explicit chip-description notes may stay beside a real requirement."""
    # A denial such as "FeliCa仅作器件介绍，不作为本版目标" must not
    # be mistaken for a promise to implement FeliCa.
    retained=[]
    for part in re.split(r'[，,。；;\n]',text):
        # Classify the scope of a clause, rather than requiring one exact
        # stock phrase. A negative product-scope statement may surround a
        # whole protocol list ("不把...扩为整板承诺"). A separate positive
        # function clause still remains subject to the guard below.
        denial=re.search(r'(?:不|未|没有).{0,80}(?:列为|作为|纳入|构成|视为|扩为|扩成|升为).{0,24}(?:目标|要求|指标|承诺|功能|保证)|(?:仅|只).{0,24}(?:介绍|描述|参考|资料)|(?:属于|来自|仅见于).{0,24}(?:器件|芯片).{0,8}(?:介绍|描述|资料)|^\s*(?:不|未)(?:需要|要求|支持|实现|提供|保证|承诺)',part)
        positive=re.search(r'^\s*(?:本(?:版|板).{0,8})?(?:必须|需要|支持|提供|实现|保证|达到)',part)
        if not denial or positive:retained.append(part)
    text='，'.join(retained)
    masked=list(text)
    protocols=atoms(' '.join(m.group() for m in PROTOCOL.finditer(text)))
    has_device=bool(mentions(text))
    marker=r'用户(?:提供的)?(?:描述|介绍)|器件(?:介绍|描述|额定|标称)|芯片(?:介绍|描述|额定|标称)|非整板|不(?:作为|构成).{0,8}(?:目标|保证|要求)'
    for region in source_regions(text)['device_descriptions']:
        if re.search(marker,region['text']):masked[region['start']:region['end']]=' '*(region['end']-region['start'])
    segments=[]
    for segment in re.split(r'[，,。；;\n]',''.join(masked)):
        info=bool(re.search(r'(?:器件|芯片).{0,12}(?:能力|额定|标称|描述|介绍)|用户提供的器件描述',segment)
                  or (has_device and re.search(r'用户(?:提供的)?描述',segment)))
        strong=bool(re.search(r'(?:整板|本板|电路).{0,12}(?:必须|要求|保证|需达到)',segment))
        if not info or strong:segments.append(segment)
    # Extra public protocols still belong in the device-description section,
    # even when someone appends a general "not verified" note to a function.
    return atoms(''.join(segments))|protocols


def exclusive_claims(messages):
    users=[m for m in messages if m['role']=='user'];regions=[source_regions(m['content']) for m in users]
    description_atoms=set().union(*(atoms(d['text']) for r in regions for d in r['device_descriptions'])) if regions else set()
    positive=[part for r in regions for part in re.split(r'[，,。；;\n]',answer_body(r['main_text']))
              if not re.search(r'不需要|不要求|不(?:作为|纳入|承诺)|取消|排除|不要|无需|仅为介绍|仅作参考',part)]
    explicit_atoms=set().union(*(atoms(part) for part in positive))
    return description_atoms-explicit_atoms


def violations(doc,messages):
    exclusive=exclusive_claims(messages)
    users=[m for m in messages if m['role']=='user']
    regions=[source_regions(m['content']) for m in users]
    other_role=r'NFC|RFID|射频(?:芯片|收发)|收发器|通信芯片|无线(?:芯片|收发)|传感器|测量芯片'
    # Apply classification checks to the actual non-controller device/module,
    # never to every MCU merely because some chip in the project has a core.
    noncontroller_owners=[d['mention'] for r in regions for d in r['device_descriptions']
                          if re.search(r'80C51|8051|Cortex|RISC|DSP',d['text'],re.I) and re.search(other_role,d['text'],re.I)
                          and not re.search(r'微控制器|单片机|\bMCU\b|通用处理器|主控',d['text'],re.I)]
    if not exclusive and not noncontroller_owners:return []
    adoption={m['id'] for m in users if not re.search(r'不希望|不需要|不要求|不要|并非',answer_body(m['content'])) and re.search(r'(?:简介|介绍).{0,30}(?:协议|功能).{0,15}(?:全部|都).{0,8}(?:需要|要|实现|支持)',answer_body(m['content']))}
    failures=[]
    def check(path,text,source='',explanatory=False,strict_numbers=False):
        if source in adoption or (explanatory and EXPLANATION.search(text)):return
        extra=sorted((atoms(text) if strict_numbers else promise_atoms(text))&exclusive)
        if extra:failures.append({'path':path,'description_only_claims':extra})
    for r in doc.get('requirements',[]):
        if r['state']=='stated' and r['category'] in ('functions','performance','architecture'):
            check('requirements.'+r['id'],r['value'],r.get('source_message_id',''),r['category']=='architecture')
    for m in doc.get('modules',[]):
        if m['state'] in ('confirmed','design_stage'):
            check('modules.'+m['id'],m['name']+'；'+m['purpose'],m.get('source_message_id',''))
            type_name=TYPES.get(m['type_id'],{}).get('name','')
            associated=[r['mention'] for r in doc.get('device_references',[]) if m['id'] in r.get('module_ids',[])]
            module_text=m['name']+' '+m['purpose']+' '+' '.join(associated)
            owner_associated=any(owner in module_text or (family and family.group() in module_text)
                                 for owner in noncontroller_owners for family in [re.match(r'[A-Za-z]+[0-9]+',owner)])
            if owner_associated and re.search(r'主控|单片机|微控制器|CPU|MCU',type_name,re.I):
                failures.append({'path':'modules.'+m['id'],'description_only_claims':['把器件内置内核介绍升级为已确认的通用主控模块类别；请按实际通信/传感/处理职责选规范类别，或保留明确待选建议']})
    for c in doc.get('device_constraints',[]):
        if c['kind']=='design_requirement':
            value=c['text']+' '+(' '.join(str(n)+c['unit'] for n in (c.get('minimum'),c.get('maximum')) if n is not None))
            check('device_constraints.'+c['id'],value,c.get('source_message_id',''),strict_numbers=True)
    for f in doc.get('field_resolutions',[]):
        if f['state']=='confirmed' and f['field_id'].startswith(('V3-2.','V3-3.','V3-5.')):
            check('field_resolutions.'+f['field_id'],f['conclusion'],f.get('source_message_id',''))
    for c in doc.get('numeric_constraints',[]):
        value=' '.join(str(n)+c['unit'] for n in (c.get('minimum'),c.get('maximum')) if n is not None)
        check('numeric_constraints.'+c['id'],value,c.get('source_message_id',''),strict_numbers=True)
    for link in doc.get('module_links',[]):
        if link.get('state')=='confirmed':check('module_links.'+link['id'],' '.join(str(link.get(k,'')) for k in ('description','voltage','rate','protocol')),link.get('source_message_id',''))
    for c in doc.get('intake_checks',[]):
        if c['state']=='confirmed':check('intake_checks.'+c['key'],c['conclusion'],c.get('source_message_id',''))
    if not adoption:
        check('summary',doc.get('summary',''))
        for i,step in enumerate(doc.get('flow',[])):check('flow.'+str(i),step['label'])
    return failures


def validate_document_claims(doc,messages):
    failures=violations(doc,messages)
    if failures:
        raise SourceClaimError(failures)
