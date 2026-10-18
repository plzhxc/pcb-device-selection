"""Conservative, dimensional checks. Database provenance is not a hardware PASS."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from .device_domain import digest

UNITS = {'V': ('voltage', 'V', Decimal(1)), 'mV': ('voltage', 'V', Decimal('.001')),
         'A': ('current', 'A', Decimal(1)), 'mA': ('current', 'A', Decimal('.001')),
         'uA': ('current', 'A', Decimal('.000001')), 'µA': ('current', 'A', Decimal('.000001')),
         'μA': ('current', 'A', Decimal('.000001')), 'W': ('power', 'W', Decimal(1)),
         'mW': ('power', 'W', Decimal('.001')), 'Hz': ('frequency', 'Hz', Decimal(1)),
         'kW': ('power', 'W', Decimal(1000)), 'KW': ('power', 'W', Decimal(1000)), '千瓦':('power','W',Decimal(1000)),
         '瓦':('power','W',Decimal(1)), '毫瓦':('power','W',Decimal('.001')),
         'uV':('voltage','V',Decimal('.000001')), 'µV':('voltage','V',Decimal('.000001')), 'μV':('voltage','V',Decimal('.000001')),
         'kV':('voltage','V',Decimal(1000)),
         'kHz': ('frequency', 'Hz', Decimal(1000)), 'MHz': ('frequency', 'Hz', Decimal(1000000)),
         'GHz': ('frequency', 'Hz', Decimal(1000000000)), '°C': ('temperature', 'degC', Decimal(1)),
         '℃': ('temperature', 'degC', Decimal(1)), 'degC': ('temperature', 'degC', Decimal(1)),
         'K': ('temperature', 'degC', Decimal(1)), 'bit': ('resolution', 'bit', Decimal(1)),
         'bits': ('resolution', 'bit', Decimal(1)), '位': ('resolution', 'bit', Decimal(1)),
         '伏':('voltage','V',Decimal(1)), '安':('current','A',Decimal(1)), '毫安':('current','A',Decimal('.001')),
         '元': ('currency', 'CNY', Decimal(1)),
         'CNY': ('currency', 'CNY', Decimal(1))}
NUMBER = r'[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?'
UNIT = r'(?:GHz|MHz|kHz|Hz|mV|[uµμ]V|kV|V|mA|[uµμ]A|A|mW|kW|KW|W|degC|°C|℃|K|bits?|CNY|千瓦|毫瓦|瓦|毫安|伏|安|位|元)'


def decimal(value):
    try:
        n = Decimal(str(value))
        return n if n.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def convert(value, unit):
    item = UNITS.get(unit)
    n = decimal(value)
    if not item or n is None:
        return None
    result = n * item[2]
    if unit == 'K': result -= Decimal('273.15')
    return result, item[0], item[1]


def parse_value(text, fallback_unit=''):
    value = str(text or '').strip().replace('−', '-').replace('～', '~').replace('至', '~')
    range_match = re.fullmatch(rf'\s*({NUMBER})\s*({UNIT})?\s*(?:~|\.\.|到|\s-\s)\s*({NUMBER})\s*({UNIT})?\s*', value)
    if range_match:
        a, u1, b, u2 = range_match.groups(); unit = u2 or u1 or fallback_unit
        x, y = convert(a, u1 or unit), convert(b, unit)
        if x and y and x[1] == y[1] and x[0] <= y[0]:
            return {'minimum': str(x[0]), 'maximum': str(y[0]), 'dimension': x[1], 'unit': x[2], 'form': 'range'}
        return None
    single = re.fullmatch(rf'\s*({NUMBER})\s*({UNIT})?\s*', value)
    if single:
        n = convert(single[1], single[2] or fallback_unit)
        if n:
            return {'minimum': str(n[0]), 'maximum': str(n[0]), 'dimension': n[1], 'unit': n[2], 'form': 'point'}
    return None


def quoted_quantity(value,unit,quote):
    expected=convert(value,unit)
    if expected is None:return False
    for match in re.finditer(rf'(?<![A-Za-z0-9_.])({NUMBER})\s*({UNIT})',quote):
        if match[2][-1].isascii() and re.match(r'[A-Za-z]',quote[match.end():]):continue
        found=convert(match[1],match[2])
        if found and found[:2]==expected[:2]:return True
    for match in re.finditer(rf'(?<![A-Za-z0-9_.])({NUMBER})\s*(?:~|～|至|到|–)\s*({NUMBER})\s*({UNIT})',quote):
        if match[3][-1].isascii() and re.match(r'[A-Za-z]',quote[match.end():]):continue
        found=convert(match[1],match[3])
        if found and found[:2]==expected[:2]:return True
    return False


def normalize_attribute(row):
    raw = str(row.get('field_value_text') if row.get('field_value_text') is not None else
              row.get('field_value_num') if row.get('field_value_num') is not None else '')
    parsed = parse_value(raw, row.get('field_unit') or '')
    status = 'normalized' if parsed else 'raw'
    issue = ''
    n = row.get('field_value_num')
    if parsed and n is not None and parsed['form'] == 'point':
        numeric = convert(n, row.get('field_unit') or '')
        if numeric and numeric[1] == parsed['dimension'] and numeric[0] != Decimal(parsed['minimum']):
            status = 'source_conflict'; issue = '原始文本与结构数值/单位不一致，保留原值，暂不用于自动通过。'
    key = row['field_key']
    return {'field_key': key, 'label': row.get('field_name_zh') or key, 'raw_value': raw,
            'raw_numeric': str(n) if n is not None else None, 'raw_unit': row.get('field_unit'),
            'normalized': parsed, 'validation_state': status, 'issue': issue,
            'source_evidence_status': row.get('evidence_status', 'UNKNOWN'),
            'is_missing': bool(row.get('is_missing')), 'is_derived': bool(row.get('is_derived_value')),
            'is_required_in_catalog': bool(row.get('is_required')), 'conditions': '',
            'rating_kind': 'unspecified', 'locator': row.get('source_locator', ''),
            'source_field': row.get('source_column', ''), 'source_checksum': row.get('source_checksum'),
            'taxonomy_version_id': str(row.get('taxonomy_version_id', '')),
            'value_origin': row.get('value_origin', ''), 'priority_rank': row.get('priority_rank') or 9999}


ALIASES = {'input_voltage': ('input_voltage_range', 'operating_voltage_vin', 'supply_voltage'),
           'output_voltage': ('output_voltage', 'output_voltage_range'),
           'output_current': ('output_current',),
           'operating_temperature': ('operating_temperature', 'operating_temperature_range'),
           'switching_frequency': ('switching_frequency',), 'resolution': ('resolution',),
           'interface': ('interface', 'interface_type'), 'feature': (), 'price': ('price',)}


def evaluate_constraint(constraint, reference, evidence, dependency_hash):
    assertions = [(a, e['id']) for e in evidence for a in e.get('assertions', [])
                  if a['field_key'] in ALIASES.get(constraint['field_key'], ())]
    base = {'id': 'CHK-' + digest([constraint['id'], reference['id']])[:16], 'rule_id': constraint['field_key'] + '/1',
            'reference_id': reference['id'], 'requirement_ids': [constraint['requirement_id']],
            'constraint_id': constraint['id'], 'status': 'unknown', 'blocking': False,
            'evidence_ids': list(dict.fromkeys(eid for _, eid in assertions)), 'dependency_hash': dependency_hash,
            'title': reference['mention'] + ' · ' + constraint['field_key'],
            'description': '尚未取得同对象、同工况的可比较资料。', 'formula': '', 'missing': []}
    if not assertions:
        base['missing'] = ['对应参数及其适用条件']; return base
    if any(a['validation_state'] == 'source_conflict' for a, _ in assertions):
        base.update(description='资料的原值与结构单位不一致，需要补证。', missing=['单位冲突的有依据解决'])
        return base
    numeric = [(a, eid) for a, eid in assertions if a.get('normalized') and not a.get('is_missing')]
    if not numeric:
        base['missing'] = ['可解释的数值、单位或明确的功能声明']; return base
    low = convert(constraint.get('minimum'), constraint['unit']) if constraint.get('minimum') is not None else None
    high = convert(constraint.get('maximum'), constraint['unit']) if constraint.get('maximum') is not None else None
    candidate = next(((a, eid) for a, eid in numeric if (low or high) and
                      a['normalized']['dimension'] == (low or high)[1]), None)
    if not candidate:
        base['missing'] = ['同维度单位与明确界限']; return base
    a, eid = candidate; n = a['normalized']; cmin, cmax = Decimal(n['minimum']), Decimal(n['maximum'])
    rmin, rmax = low[0] if low else None, high[0] if high else None
    relation = constraint['relation']; violated = False
    if relation == 'covers': violated = (rmin is not None and cmin > rmin) or (rmax is not None and cmax < rmax)
    elif relation == 'at_least': violated = rmin is not None and cmax < rmin
    elif relation == 'at_most': violated = rmax is not None and cmin > rmax
    elif relation == 'equals': violated = (rmin is not None and cmax < rmin) or (rmax is not None and cmin > rmax)
    else: return base
    base['formula'] = f'需求 [{rmin},{rmax}] {n["unit"]}；目录 [{cmin},{cmax}] {n["unit"]}；关系 {relation}'
    # Numeric catalog text alone lacks guarantee/port/conditions. A contradiction is a
    # useful issue, but only explicit scope + operating evidence can become a blocker.
    conditions_known = bool(a.get('conditions')) and a.get('rating_kind') in ('recommended_operating', 'guaranteed')
    quote=constraint['source_quote'].casefold();port=constraint['port'].strip()
    explicit = bool(port) and reference['mention'].casefold() in quote
    if constraint['field_key']=='input_voltage':
        # A board rail or VOUT is not the selected device's VIN. Require an
        # actual supply-pin name in the user's words before making a hard claim.
        pins=re.findall(r'(?<![A-Za-z0-9])(?:VIN|VDD|VCC|AVDD|DVDD|PVDD)(?:[0-9]+)?(?![A-Za-z0-9])',port,re.I)
        explicit=explicit and bool(pins) and any(re.search(r'(?<![a-z0-9])'+re.escape(pin.casefold())+r'(?![a-z0-9])',quote) for pin in pins)
        if not explicit:
            base.update(description='尚未明确这项整板电源或其他端口与该器件VIN/VDD的实际连接，不能按器件输入范围判定整板通过或冲突。',
                        missing=['用户明确的器件输入供电端口与连接边界'])
            return base
    if violated:
        blocking = conditions_known and explicit and constraint['kind'] == 'design_requirement' and reference['intent'] == 'must'
        base.update(status='contradicted' if blocking else 'conditional', blocking=blocking,
                    description='已明确的工作条件与硬约束冲突，需要决定。' if blocking else
                    '目录数值与这项描述不同；需要核对对象、工作条件与手册，尚不判定整个方案不可行。')
    else:
        base.update(status='supported' if conditions_known and explicit else 'conditional',
                    description='该项在所列条件下得到资料支持。' if conditions_known and explicit else
                    '目录数值初步相符；推荐工作条件、端口或保证性质仍待核对。')
    return base


def checks_for(doc, references, evidence):
    from .device_domain import input_fingerprint
    dependency = input_fingerprint(doc)
    result = []
    for reference in references:
        related = [e for e in evidence if e.get('reference_id') == reference['id'] and not e.get('stale')]
        for e in related:
            for a in e.get('assertions', []):
                if a['validation_state'] == 'source_conflict':
                    result.append({'id': 'CHK-' + digest([reference['id'], a['field_key'], 'unit'])[:16],
                                   'rule_id': 'source-unit/1', 'reference_id': reference['id'],
                                   'requirement_ids': reference.get('requirement_ids', []), 'status': 'unknown',
                                   'blocking': False, 'title': reference['mention'] + ' · 单位需要核对',
                                   'description': a['issue'], 'evidence_ids': [e['id']], 'formula': '',
                                   'dependency_hash': dependency, 'missing': ['原值与结构字段冲突的核实']})
        for constraint in doc.get('device_constraints', []):
            if constraint['reference_id'] == reference['id']:
                result.append(evaluate_constraint(constraint, reference, related, dependency))
    return result
