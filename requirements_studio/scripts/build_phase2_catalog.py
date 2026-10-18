#!/usr/bin/env python3
"""Extract the user's authoritative Word catalogue; never invent or rename types."""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path
from zipfile import ZipFile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT.parent / 'AgenticPCB_电路模块与子模块专业定义规范_60父模块197子模块_1.0.docx'
OUT = ROOT / 'backend/resources/module_catalog.json'
NS = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}


def extract_lines():
    with ZipFile(SOURCE) as archive:
        body = ET.fromstring(archive.read('word/document.xml')).find('w:body', NS)
    lines = []
    for block in body:
        if block.tag.endswith('}p'):
            text = ''.join(t.text or '' for t in block.findall('.//w:t', NS))
            if text.strip():
                lines.append(text.strip())
        elif block.tag.endswith('}tbl'):
            for row in block.findall('w:tr', NS):
                cells = [' '.join(''.join(t.text or '' for t in p.findall('.//w:t', NS))
                                  for p in cell.findall('w:p', NS)) for cell in row.findall('w:tc', NS)]
                lines.append(' | '.join(cells))
            lines.append('')
    return lines


def parameter(line):
    cells = line.split(' | ')
    if len(cells) != 6:
        return None
    match = re.fullmatch(r'(.+?)((?:[A-Z]|M_[\d_]+)\.[A-Za-z0-9_]+)', cells[0])
    if not match:
        return None
    return dict(zip(['name', 'id', 'unit', 'value_rule', 'conditions', 'required', 'match_rule'],
                    [match[1], match[2], *cells[1:]]))


def build():
    lines = extract_lines()
    categories = []
    for line in lines[:20]:
        m = re.match(r'第(\d+)类 \d+ (.+?)\s+(\d+) 个父模块／(\d+) 个子模块', line)
        if m:
            categories.append({'id': m[1], 'name': m[2], 'parents': int(m[3]), 'children': int(m[4])})
    group_start = lines.index('公共参数字段组')
    type_headers = [(i, re.match(r'类型ID (M_[\d_]+)\s+\|\s+父模块 (\d+\.\d+) (.*?)\s+\|\s+继承组 (.*)', line))
                    for i, line in enumerate(lines)]
    type_headers = [(i, m) for i, m in type_headers if m]
    groups = {}
    current = None
    for line in lines[group_start:type_headers[0][0]]:
        m = re.match(r'^([A-Z]) (.+)$', line)
        if m:
            current = m[1]
            groups[current] = {'id': current, 'name': m[2], 'parameters': []}
        row = parameter(line)
        if row and current:
            groups[current]['parameters'].append(row)
    cards = {}
    current_card = None
    for line in lines:
        m = re.match(r'^(\d+\.\d+\.\d+) (.*?)\s+\[(M_[\d_]+)\]$', line)
        if m:
            current_card = cards[m[3]] = []
        elif line.startswith('第四编'):
            current_card = None
        elif current_card is not None and re.match(r'^[①②③④⑤⑥⑦⑧⑨⑩]', line):
            current_card.append(line)
    parent_cards={}
    active_parent=None
    for line in lines:
        m=re.match(r'^父模块 (\d+\.\d+)\s+(.+)$',line)
        if m:
            active_parent='P_'+m[1].replace('.','_');parent_cards[active_parent]=[]
        elif re.match(r'^\d+\.\d+\.\d+ ',line) or line.startswith('第四编'):
            active_parent=None
        elif active_parent and line:
            parent_cards[active_parent].append(line)
    parents = {}
    modules = []
    for pos, (index, match) in enumerate(type_headers):
        type_id, parent_ref, parent_name, inheritance = match.groups()
        parent_id = 'P_' + parent_ref.replace('.', '_')
        parents.setdefault(parent_id, {'id': parent_id, 'spec_ref': parent_ref, 'name': parent_name,
                                      'category_id': parent_ref.split('.')[0], 'level': 'parent'})
        end = type_headers[pos+1][0]-1 if pos+1 < len(type_headers) else lines.index('第二编　工程化使用总则及报告勘误')
        body = lines[index+1:end]
        name = re.sub(r'^\d+\.\d+\.\d+\s+', '', lines[index-1])
        inherited = [g.strip() for g in inheritance.split('、')]
        special = [p for line in body if (p := parameter(line)) and p['id'].startswith(type_id+'.')]
        card = cards.get(type_id, [])
        ports = []
        for text in card[:1]:
            for role, attributes in re.findall(r'([A-Z][A-Z_0-9/+\-]*)〔([^〕]+)〕', text):
                parts = attributes.split('/')
                if len(parts) == 3:
                    ports.append({'role': role, 'direction': parts[0], 'kind': parts[1], 'required': parts[2]})
        modules.append({'id': type_id, 'spec_ref': type_id[2:].replace('_','.'), 'name': name,
                        'parent_id': parent_id, 'category_id': parent_ref.split('.')[0], 'level': 'submodule',
                        'description': next((s for s in body if s.startswith('定义')), ''),
                        'inherited_groups': inherited, 'parameters': special, 'ports': ports, 'engineering_card': card})
    assert len(parent_cards)==60
    for pid,parent in parents.items():
        card=parent_cards[pid]
        parent['description']=next(s.split('  ',1)[-1] for s in card if s.startswith('父模块契约'))
        parent['engineering_card']=card
        parent['ports']=[]
        for text in (s for s in card if s.startswith('抽象接口')):
            for role,attrs in re.findall(r'([A-Z][A-Z_0-9/+\-]*)（([^）]+)）',text):
                values=attrs.split('，')
                if len(values)==3:parent['ports'].append(dict(role=role,direction=values[0],kind=values[1],required=values[2]))
    assert len(categories) == 10 and len(parents) == 60 and len(modules) == 197
    assert len({m['id'] for m in modules}) == 197 and len(cards) == 197
    assert all(m['inherited_groups'] and all(g in groups for g in m['inherited_groups']) for m in modules)
    assert all(m['engineering_card'] and m['parameters'] for m in modules)
    result = {'schema': 'requirements-studio/module-catalog/1', 'source_file': SOURCE.name,
              'source_sha256': hashlib.sha256(SOURCE.read_bytes()).hexdigest(), 'revision': '1.0',
              'categories': categories, 'parents': list(parents.values()), 'modules': modules, 'parameter_groups': groups}
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'categories': len(categories), 'parents': len(parents), 'submodules': len(modules),
                      'specific_parameter_rows': sum(len(m['parameters']) for m in modules),
                      'common_parameter_rows': sum(len(g['parameters']) for g in groups.values()),
                      'source_sha256': result['source_sha256']}, ensure_ascii=False))


if __name__ == '__main__':
    build()
