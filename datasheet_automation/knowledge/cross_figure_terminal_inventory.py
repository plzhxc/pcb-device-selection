"""Authenticate reviewed identities across two original package figures.

The caller supplies a trusted original-source snapshot. The registered review
binds both pages and their orientation; this consumer verifies its complete
physical correspondence without changing a proposed terminal inventory.
"""
from collections.abc import Mapping
from copy import deepcopy
import re

CROSS_FIGURE_SCHEMA = 'pcbagent.manufacturer_terminal_inventory.v5'
_NUMBER = re.compile(r'[1-9][0-9]?\Z')
_NAME = re.compile(r'[A-Za-z][A-Za-z0-9_+./-]*\Z')


def authenticate_cross_figure_inventory(inventory, extraction, knowledge, facts,
                                       proof, chunks, source_evidence):
    from datasheet_automation.knowledge.terminal_inventory import _authenticate_reviewed_receipt
    required = {'schema', 'supplier_part', 'document_sha256', 'device_name', 'package',
                'complete_figure', 'inventory_scope', 'number_figure', 'label_figure',
                'alignment', 'unindexed_features', 'package_evidence'}
    if (not isinstance(inventory, Mapping) or set(inventory) != required
            or inventory.get('schema') != CROSS_FIGURE_SCHEMA
            or inventory.get('supplier_part') != knowledge.get('supplier_part')
            or inventory.get('document_sha256') != proof.get('document_sha256')
            or inventory.get('document_sha256') != (knowledge.get('document') or {}).get('pdf_sha256')
            or inventory.get('device_name') != facts.get('device_name')
            or inventory.get('package') != facts.get('package')
            or inventory.get('complete_figure') is not True
            or inventory.get('inventory_scope') != 'single_package_electrical_contacts'):
        raise ValueError('exact same-source single-package cross-figure scope required')
    owner = chunks.get(proof.get('source_ref'))
    if owner is None:
        raise ValueError('registered complete correspondence review required')
    _authenticate_reviewed_receipt(owner, proof, knowledge,
        required_scopes={'terminal_inventory_completeness', 'cross_figure_terminal_identity', 'terminal_view_alignment'})
    import json
    if json.loads(owner['content']) != inventory:
        raise ValueError('candidate correspondence differs from registered independent review')
    document_sha = inventory['document_sha256']
    referenced = {proof['source_ref']}

    def citation(claim, scopes, *, expected_ref=None, expected_page=None, token=None):
        if (not isinstance(claim, Mapping) or set(claim) != {'source_ref', 'quote'}
                or not isinstance(claim['source_ref'], str) or not isinstance(claim['quote'], str)
                or len(claim['quote'].strip()) < 3 or claim['source_ref'] == proof['source_ref']
                or (expected_ref is not None and claim['source_ref'] != expected_ref)):
            raise ValueError('precise independent original-figure citation required')
        chunk = chunks.get(claim['source_ref'])
        checked = source_evidence({'chunk_id': chunk['id'], 'quote': claim['quote'],
                                   'fields': sorted(scopes)}, knowledge) if chunk else None
        if (not checked or checked.get('document_sha256') != document_sha
                or expected_page is not None and checked.get('page') != expected_page):
            raise ValueError('figure citation source SHA/page differs')
        _authenticate_reviewed_receipt(chunk, checked, knowledge, required_scopes=set(scopes))
        if token is not None and not re.search(r'(?<![A-Za-z0-9_])'+re.escape(token)+r'(?![A-Za-z0-9_])', claim['quote']):
            raise ValueError('actual terminal identity absent from its figure quote')
        referenced.add(claim['source_ref'])
        return checked

    package_evidence = inventory['package_evidence']
    if not isinstance(package_evidence, list) or not 1 <= len(package_evidence) <= 4:
        raise ValueError('same exact model and package citations required')
    package_proofs = [citation(c, {'device_name', 'package'}) for c in package_evidence]
    if not any(inventory['device_name'] in c['quote'] for c in package_proofs) or not any(
            inventory['package'] in c['quote'] for c in package_proofs):
        raise ValueError('exact package/model not present in reviewed source text')

    def position(row):
        if (not isinstance(row, Mapping) or set(row) != {'side', 'index_from_left', 'side_size'}
                or row['side'] not in {'near', 'far'}
                or type(row['side_size']) is not int or not 1 <= row['side_size'] <= 16
                or type(row['index_from_left']) is not int
                or not 0 <= row['index_from_left'] < row['side_size']):
            raise ValueError('bounded physical bank position required')
        return (row['side'], row['index_from_left'], row['side_size'])

    def figure(spec, kind):
        if (not isinstance(spec, Mapping) or set(spec) != {'source_ref', 'page', 'locator', 'view',
                'view_basis', 'view_evidence', 'terminals'}
                or not isinstance(spec['source_ref'], str) or spec['source_ref'] == proof['source_ref']
                or type(spec['page']) is not int or spec['page'] < 1
                or not isinstance(spec['locator'], str) or not spec['locator'].strip()
                or spec['view'] not in {'top', 'bottom'}
                or spec['view_basis'] not in {'explicit_caption', 'independent_visual_landmark'}
                or not isinstance(spec['view_evidence'], list) or not 1 <= len(spec['view_evidence']) <= 4
                or not isinstance(spec['terminals'], list) or not 2 <= len(spec['terminals']) <= 32):
            raise ValueError('actual figure view and a closed bounded contact inventory required')
        views = [citation(c, {'physical_terminal_identity'}, expected_ref=spec['source_ref'],
                         expected_page=spec['page']) for c in spec['view_evidence']]
        if spec['view_basis'] == 'explicit_caption' and not any(
                re.search(r'\b'+spec['view']+r'\s+view\b', c['quote'], re.I) for c in views):
            raise ValueError('explicit view basis needs its original caption')
        rows, identities, row_proofs = {}, set(), {}
        key = 'pin_number' if kind == 'number' else 'pin_name'
        permitted = {key, 'position', key.replace('pin_', '')+'_evidence',
                     'kind' if kind == 'number' else 'label_extent'}
        for row in spec['terminals']:
            if (not isinstance(row, Mapping) or set(row) != permitted
                    or not isinstance(row[key], str)
                    or not (_NUMBER if kind == 'number' else _NAME).fullmatch(row[key])
                    or kind == 'number' and row['kind'] != 'peripheral'
                    or kind == 'label' and row['label_extent'] not in {'single_contact', 'continuous_metal_bank'}):
                raise ValueError('printed contact identity and supported physical type required')
            p = position(row['position'])
            if p in rows or kind == 'number' and row[key] in identities:
                raise ValueError('physical position or number repeats')
            rows[p] = dict(row); identities.add(row[key])
            row_proofs[p] = citation(row[key.replace('pin_', '')+'_evidence'], {key},
                    expected_ref=spec['source_ref'], expected_page=spec['page'], token=row[key])
        for side in {'near', 'far'}:
            group = [p for p in rows if p[0] == side]
            if not group or len({p[2] for p in group}) != 1 or {p[1] for p in group} != set(range(group[0][2])):
                raise ValueError('both physical banks must be completely enumerated')
        if kind == 'label':
            for p, row in rows.items():
                if row['label_extent'] == 'continuous_metal_bank':
                    bank = [r for q, r in rows.items() if q[0] == p[0]]
                    if any(r['pin_name'] != row['pin_name'] or r['label_extent'] != 'continuous_metal_bank' for r in bank):
                        raise ValueError('a shared metal label must cover exactly one complete named bank')
        return rows, row_proofs, views

    numbers, number_proofs, number_views = figure(inventory['number_figure'], 'number')
    labels, label_proofs, label_views = figure(inventory['label_figure'], 'label')
    nf, lf = inventory['number_figure'], inventory['label_figure']
    if nf['source_ref'] == lf['source_ref'] or nf['page'] == lf['page']:
        raise ValueError('cross-figure branch requires distinct original pages')
    align = inventory['alignment']
    expected_transform = 'identity' if nf['view'] == lf['view'] else 'mirror_x'
    if (not isinstance(align, Mapping) or set(align) != {'transform', 'landmarks'}
            or align['transform'] != expected_transform
            or not isinstance(align['landmarks'], list) or not 1 <= len(align['landmarks']) <= 4):
        raise ValueError('declared source views and bounded transform disagree')

    def transform(p):
        return p if expected_transform == 'identity' else (p[0], p[2]-1-p[1], p[2])

    if len(numbers) != len(labels) or {transform(p) for p in labels} != set(numbers):
        raise ValueError('source figures do not have a complete physical-position bijection')
    pin1_proved = False; landmarks = []
    for landmark in align['landmarks']:
        if (not isinstance(landmark, Mapping) or set(landmark) != {'kind', 'label_position',
                'number_position', 'label_evidence', 'number_evidence'}
                or landmark['kind'] != 'explicit_pin1'):
            raise ValueError('first bounded consumer requires an explicit manufacturer Pin1 landmark')
        lp, np = position(landmark['label_position']), position(landmark['number_position'])
        if lp not in labels or np not in numbers or transform(lp) != np or numbers[np]['pin_number'] != '1':
            raise ValueError('Pin1 landmark is not preserved by the global transform')
        lproof = citation(landmark['label_evidence'], {'physical_terminal_identity'},
                           expected_ref=lf['source_ref'], expected_page=lf['page'])
        nproof = citation(landmark['number_evidence'], {'pin_number'},
                           expected_ref=nf['source_ref'], expected_page=nf['page'], token='1')
        if not re.search(r'\bPin\s*1\b', lproof['quote'], re.I):
            raise ValueError('label figure has no explicit Pin1 landmark')
        pin1_proved = True; landmarks.append({'label': lproof, 'number': nproof})
    if not pin1_proved:
        raise ValueError('orientation remains unresolved')
    features = inventory['unindexed_features']
    if not isinstance(features, list) or len(features) > 4:
        raise ValueError('unindexed package-feature boundary required')
    feature_proofs = []
    for feature in features:
        if (not isinstance(feature, Mapping) or set(feature) != {'kind', 'label', 'covered_number_positions', 'evidence', 'adds_terminal'}
                or feature['kind'] != 'metal_extension_of_numbered_bank'
                or feature['adds_terminal'] is not False
                or not isinstance(feature['evidence'], list) or not feature['evidence']):
            raise ValueError('undocumented separate terminal or unsupported feature cannot be ignored')
        covered = [position(p) for p in feature['covered_number_positions']]
        bank = [transform(p) for p, r in labels.items()
                if r['pin_name'] == feature['label'] and r['label_extent'] == 'continuous_metal_bank']
        if not covered or len(set(covered)) != len(covered) or set(covered) != set(bank):
            raise ValueError('metal extension must refer to the complete already-numbered shared bank')
        feature_proofs.extend(citation(c, {'physical_terminal_identity'}) for c in feature['evidence'])

    mapping = {numbers[transform(p)]['pin_number']: row['pin_name'] for p, row in labels.items()}
    actual, raw = facts.get('pins'), extraction.get('pins')
    if (not isinstance(actual, list) or not isinstance(raw, list)
            or type(extraction.get('pin_count')) is not int or extraction['pin_count'] != len(mapping)
            or extraction.get('pins_complete') is not True
            or len(actual) != len(mapping) or len(raw) != len(mapping)
            or any(not isinstance(row, Mapping) or row.get('pin_number') not in mapping
                   or row.get('pin_name') != mapping[row['pin_number']] for row in actual+raw)
            or len({row['pin_number'] for row in actual}) != len(mapping)
            or len({row['pin_number'] for row in raw}) != len(mapping)):
        raise ValueError('candidate must equal every source contact with no NULL, extra or duplicate row')
    for pin in actual:
        for field, spec in [('pin_number', nf), ('pin_name', lf)]:
            evidence = (pin.get('evidence') or {}).get(field) or {}
            citation({'source_ref': evidence.get('source_ref'), 'quote': evidence.get('quote')}, {field},
                     expected_ref=spec['source_ref'], expected_page=spec['page'], token=pin[field])
        if any(pin.get(field) is not None for field in ('pin_type', 'pin_description',
                'pin_connection_notes', 'pin_impedance', 'pin_frequency')):
            raise ValueError('cross-figure preparation is limited to contact identity fields')
    rows = [{'pin_numbers': [numbers[transform(p)]['pin_number']], 'pin_name': labels[p]['pin_name'],
             'number_position': list(transform(p)), 'label_position': list(p),
             'number_evidence': number_proofs[transform(p)], 'label_evidence': label_proofs[p]} for p in labels]
    return {**deepcopy(dict(proof)), 'derivation': {
        'method': 'complete_manufacturer_terminal_inventory', 'parser': 'reviewed_cross_figure_explicit_pin1_v5',
        'manufacturer_printed_pin_count': None, 'computed_terminal_count': len(mapping),
        'manufacturer_pin_numbers': sorted(mapping, key=int), 'source_rows': rows,
        'transform': expected_transform, 'landmarks': landmarks, 'package_evidence': package_proofs,
        'number_view_evidence': number_views, 'label_view_evidence': label_views,
        'unindexed_feature_evidence': feature_proofs, 'source_refs': sorted(referenced),
        'limitation': 'Two-page independently reviewed identity derivation; no electrical role, NC, voltage or added pad permission.'}}
