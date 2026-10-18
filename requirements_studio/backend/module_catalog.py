"""Authoritative module types extracted from the user's professional specification."""
import json
from pathlib import Path

CATALOG = json.loads((Path(__file__).parent / 'resources/module_catalog.json').read_text(encoding='utf-8'))
CATEGORIES = {x['id']: x for x in CATALOG['categories']}
TYPES = {x['id']: x for x in [*CATALOG['parents'], *CATALOG['modules']]}


def summary():
    return {'revision': CATALOG['revision'], 'source_file': CATALOG['source_file'],
            'source_sha256': CATALOG['source_sha256'], 'categories': CATALOG['categories'],
            'types': [{k: v for k, v in row.items() if k in ('id','name','spec_ref','parent_id','category_id','level')}
                      for row in TYPES.values()]}


def detail(type_id):
    row = TYPES.get(type_id)
    if row is None:
        raise ValueError('请选择规范目录中的模块类别。')
    inherited = [p for group in row.get('inherited_groups', [])
                 for p in CATALOG['parameter_groups'][group]['parameters']]
    return {**row, 'category_name': CATEGORIES[row['category_id']]['name'],
            'parent_name': TYPES.get(row.get('parent_id'), {}).get('name'),
            'inherited_parameters': inherited,
            'children': [r['id'] for r in CATALOG['modules'] if r['parent_id'] == type_id]}


def prompt_catalog():
    # Names, functional hierarchy and boundaries matter; IDs are internal references.
    return [{'id': p['id'], 'name': p['name'], 'category': CATEGORIES[p['category_id']]['name'],
             'functional_contract':p.get('description',''), 'abstract_roles':[port['role'] for port in p.get('ports',[])],
             'children': [{'id': c['id'], 'name': c['name']} for c in CATALOG['modules'] if c['parent_id'] == p['id']]}
            for p in CATALOG['parents']]
