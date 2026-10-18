"""Separate product decisions from implementation work and reference templates."""
import re
from .module_catalog import TYPES,CATEGORIES

CLOSED={'confirmed','not_applicable','design_stage'}
CORE={'V3-1.1:F03','V3-1.1:F04',
      'V3-2.1.1:F01','V3-2.1.1:F02','V3-2.1.1:F03'}


def ui_review_question(question):
    """A review-button invitation must not block the button that performs it.

    This routes only unlinked workflow questions. Actual product or module
    decisions, including their review/acceptance requirements, remain pending.
    """
    if question.get('requirement_ids') or question.get('field_ids'):return False
    if question.get('decision_key') not in {'overall_requirements_review','enter_overall_review','start_requirements_review','approve_requirements_document'}:return False
    return bool(re.search(r'(?:整体|需求|文档|说明书|草稿).{0,12}(?:审阅|审核|确认)',question.get('question','')))


def statistical_note(requirement):
    """Explicit counting notes do not describe an extra physical circuit.

    Keep every actual port/channel target as hardware. A mixed row containing
    physical operations remains a functional requirement until it is split.
    """
    name=requirement.get('name','');value=requirement.get('value','')
    if not re.search(r'(?:数量口径|统计口径|规模参考|参考规模|规模说明|数量参考|数量说明|元器件规模|元件规模)$',name):return False
    if not re.search(r'参考|近似|不.{0,10}(?:总数|上限|硬性数量)',value):return False
    if re.search(r'提供|支持|实现|采集|驱动|控制|测量|输出|输入',value):return False
    return True


def product_field(field):
    if field['chapter'] in (1,2):return field['id']!='V3-1.3:F08'
    if field['chapter']==3:
        return field['topic_id']!='V3-3.1.1' or field['id'] in {'V3-3.1.1:F01','V3-3.1.1:F02'}
    return field['topic_id']=='V3-4.2.1'


def module_topics(doc):
    topics=set()
    parent_topics={
        '电源转换模块':['5.2.1.2'],'隔离电源模块':['5.2.1.2'],
        '电源时序管理模块':['5.2.1.5'],'受电端Sink协议模块':['5.2.1.1'],
        '电池管理模块':['5.2.1.3','5.2.1.4'],'电源备份模块':['5.2.1.5'],
        '主控与处理模块':['5.2.2'],'数码管与点阵显示模块':['5.2.5.1'],
        'LCD与OLED显示模块':['5.2.5.1'],'蜂鸣器与扬声器模块':['5.2.5.4'],
        '拾音器模块':['5.2.5.6'],'语音识别模块':['5.2.5.6'],
        'CAN接口模块':['5.2.4.8'],'以太网接口模块':['5.2.4.6'],
        '功率驱动模块':['5.2.6'],
    }
    active=[m for m in doc.get('modules',[]) if m['state']!='not_applicable']
    if active:topics.update({'V3-5.1','V3-5.3'})
    for m in active:
        kind=TYPES.get(m['type_id'],{});parent=TYPES.get(kind.get('parent_id'),kind)
        topics.update('V3-'+t for t in parent_topics.get(parent.get('name'),[]))
        if CATEGORIES.get(kind.get('category_id'),{}).get('name','').startswith('传感器与采集'):topics.add('V3-5.2.3')
        if any(word in parent.get('name','') for word in ('存储','eMMC')):topics.add('V3-5.2.7')
        if parent.get('name') in ('过压保护模块','浪涌限流与缓启动模块','防反接模块'):topics.add('V3-5.2.8')
        text=' '.join([kind.get('name',''),m.get('name',''),m.get('purpose','')])
        for word,topic in [('Wi-Fi','5.2.4.1'),('蓝牙','5.2.4.2'),('LoRa','5.2.4.3'),('蜂窝','5.2.4.4'),('RS485','5.2.4.7'),('RS232','5.2.4.7'),('按键','5.2.5.5'),('触控','5.2.5.2'),('指示灯','5.2.5.3')]:
            if word in text:topics.add('V3-'+topic)
    return topics


def decision_scope(doc,fields):
    """Unknown engineering detail remains unknown; it is not a user questionnaire."""
    selected=module_topics(doc)
    # A quote mentioning USB, for example, does not establish a new power limit.
    # Unknown optional bounds stay unknown; actual unanswered product questions and
    # unsettled REQs are still guarded separately by the review gate.
    required={f['id'] for f in fields if f['id'] in CORE or
              (product_field(f) and ((f['state'] in CLOSED and f.get('source_quote')) or
               (f.get('recheck_evidence') and f['recheck_evidence'].get('state','confirmed') in CLOSED) or f['state']=='conflict'))}
    decisions=[f for f in fields if f['id'] in required]
    pending=[f for f in decisions if f['state'] not in CLOSED]
    rechecks=set(doc.get('recheck_fields',[]))
    module_rechecks=[f['id'] for f in fields if f['id'] in rechecks and f['topic_id'] in selected]
    hardware=bool(doc.get('modules')) or any(r['category']=='functions' and r.get('implementation','hardware')=='hardware' and r['state']!='not_applicable' for r in doc['requirements'])
    engineering=[f for f in fields if hardware and not product_field(f) and f['state']!='not_applicable' and
                 (f['chapter']<5 or f['topic_id'] in selected)]
    return {'decision_fields':decisions,'decision_total':len(decisions),
            'decision_closed':sum(f['state'] in CLOSED for f in decisions),
            'decision_pending':[f['id'] for f in pending],
            'decision_rechecks':[f['id'] for f in pending if f['id'] in rechecks],
            'module_rechecks':module_rechecks,
            'engineering_fields':engineering,
            'engineering_pending':sum(f['state'] not in CLOSED for f in engineering),
            'module_topics':sorted(selected)}


def review_reply(reply,doc):
    """A model cannot announce review readiness while the saved gate still fails."""
    from .coverage import review_document
    review=review_document(doc)
    claim=r'(?:现可|已可|可以|可供|可直接|即可|可整体)(?:进入)?(?:整体)?(?:审核|审阅)|可进入(?:整体)?(?:审核|审阅)|(?:可以|可)(?:通过|使用|在).{0,24}(?:整体审阅|整体审核|审阅入口|审核入口)|(?:需求|说明书|本版).{0,8}(?:已完整|已收敛)|无需(?:补充|确认|回答)'
    overclaims=any(re.search(claim,s) and not re.search(r'如果|后|才能|才可|不能|暂不|尚未',s)
                   for s in re.split(r'(?<=[。！？\n])',reply))
    if review['status']!='draft' or not overclaims:return reply
    next_steps=[]
    if review['open_questions']:next_steps.append('回答当前仍待明确的产品问题')
    if review['module_pending']:next_steps.append('核对并确认建议中的模块类别、用途和数量')
    if review['module_gaps']:next_steps.append('为必要硬件功能补齐模块承接')
    if review['unsettled_requirements']:next_steps.append('处理待明确或冲突的实际需求')
    if review['consistency_issues']:next_steps.append('处理已列出的一致性问题')
    if not review['has_flow']:next_steps.append('依据已有功能整理工作流程')
    if not next_steps:next_steps.append('依据已有资料完成产品决定和关联内容的核对')
    changes='\n'.join('- '+change for change in doc['changes'])
    return '本轮已整理：\n'+changes+'\n\n当前仍为草稿，下一步需要'+ '；'.join(next_steps)+'。已有要求已保存，可以继续补充。'
