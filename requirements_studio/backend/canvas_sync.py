"""Trace device edits into the same requirements document and review gate."""
from __future__ import annotations

import copy
import re

CHOICE_FIELDS = ('mention', 'intent', 'identity_scope', 'device_key', 'quantity', 'lifecycle')
CLOSED = {'confirmed', 'design_stage', 'not_applicable'}


def pending(doc):
    return [c for c in doc.get('canvas_changes', []) if c['status'] == 'pending']


def _choice(row):
    return {k: copy.deepcopy(row.get(k)) for k in (*CHOICE_FIELDS, 'purpose')}


def record(doc, before, message_id, *, origin='conversation', result=None):
    old = {r['id']: r for r in before.get('device_references', [])}
    changed = []
    for row in doc.get('device_references', []):
        previous = old.get(row['id'])
        if previous is None and origin != 'canvas':
            continue
        if previous is None and not row.get('module_ids') and not row.get('requirement_ids'):
            continue
        comparison = (*CHOICE_FIELDS, 'purpose') if origin == 'canvas' else CHOICE_FIELDS
        if previous is not None and all(previous.get(k) == row.get(k) for k in comparison):
            continue
        if previous is None and row.get('lifecycle') == 'removed':
            continue
        changed.append((previous, row))
    if not changed:
        return
    doc['canvas_sync_version'] = 1
    history = copy.deepcopy(doc.get('canvas_changes', []))
    reviewed = set(getattr(result, 'reviewed_requirement_ids', []))
    reviewed.update(r.id for r in getattr(result, 'updates', []))
    resolved_fields = {fid for r in getattr(result, 'field_resolutions', []) for fid in r.field_ids}
    delivery_done = {r.key for r in getattr(result, 'delivery_checks', [])}
    modules = {m['id']: m for m in doc.get('modules', []) if m['state'] != 'not_applicable'}
    reqs = {r['id'] for r in doc['requirements'] if r['state'] != 'not_applicable'}
    reviews = {r['requirement_id']: r for r in doc.get('decision_reviews', [])}
    fields = set(doc.get('recheck_fields', []))
    from .convergence import mark_recheck
    for previous, row in changed:
        base = previous or {}
        mids = (set(base.get('module_ids', [])) | set(row.get('module_ids', []))) & modules.keys()
        links = [link for link in doc.get('module_links', []) if mids & {link['source'], link['target']}]
        related_modules = mids | ({mid for link in links for mid in (link['source'], link['target'])} & modules.keys())
        rids = set(base.get('requirement_ids', [])) | set(row.get('requirement_ids', []))
        rids.update(rid for mid in related_modules for rid in modules[mid]['requirement_ids'])
        rids &= reqs
        field_ids = {f['field_id'] for f in doc.get('field_resolutions', [])
                     if rids & set(f.get('requirement_ids', [])) and
                     (f['state'] in CLOSED or f['field_id'] in fields)}
        delivery_keys = {c['key'] for c in doc.get('delivery_checks', [])
                         if rids & set(c.get('requirement_ids', []))}
        if rids and doc.get('delivery_policy_version'):
            delivery_keys |= {'core_parts', 'capability_chain'}
        name = row['mention']
        reason = (f"器件由 {base['mention']} 调整为 {name}" if base.get('mention') and base['mention'] != name
                  else f'器件 {name} 的采用条件已调整')
        for rid in rids - reviewed:
            reviews[rid] = {'requirement_id': rid, 'message_id': message_id,
                            'reason': reason + '，需核对正文与实际方案。'}
        for resolution in doc.get('field_resolutions', []):
            if resolution['field_id'] in field_ids - resolved_fields:
                mark_recheck(resolution, reason + '，该条款正在同步。')
                fields.add(resolution['field_id'])
        for check in doc.get('delivery_checks', []):
            if check['key'] in delivery_keys - delivery_done:
                check['state'] = 'review'
        # Old chip descriptions must not become ratings of its replacement.
        retired = []
        if base.get('mention') and base['mention'] != name:
            retired = [c for c in doc.get('device_constraints', [])
                       if c['reference_id'] == row['id'] and c['kind'] == 'user_device_description']
            doc['device_constraints'] = [c for c in doc.get('device_constraints', []) if c not in retired]
        entry = {'id': 'EDIT-' + message_id + '-' + row['id'], 'reference_id': row['id'],
                 'source_message_id': message_id, 'origin': origin, 'reason': reason,
                 'before': _choice(base), 'after': _choice(row), 'status': 'pending',
                 'created_version': before.get('version', 0) + 1,
                 'requirement_ids': sorted(rids), 'module_ids': sorted(related_modules),
                 'link_ids': [link['id'] for link in links], 'field_ids': sorted(field_ids),
                 'delivery_keys': sorted(delivery_keys), 'retired_descriptions': retired,
                 'remaining': []}
        history.append(entry)
    doc.update(canvas_changes=history, decision_reviews=list(reviews.values()), recheck_fields=sorted(fields))
    update(doc)


def _obsolete(text, before, after):
    old, new = before.get('mention'), after.get('mention')
    if not old or old == new:
        return False
    if not re.search(r'(?<![A-Za-z0-9])' + re.escape(old) + r'(?![A-Za-z0-9])', text, re.I):
        return False
    # Explicit comparisons and retirement notes are valid history, not a live choice.
    return not (new and new.casefold() in text.casefold() and
                re.search(r'原|旧|之前|历史|不再|替换|取代|改为|改用|更换|取消|仅作参考', text))


def update(doc):
    """Only close edits whose actual saved dependencies and text are current."""
    resolutions = {r['field_id']: r for r in doc.get('field_resolutions', [])}
    checks = {c['key']: c for c in doc.get('delivery_checks', [])}
    req_reviews = {r['requirement_id'] for r in doc.get('decision_reviews', [])}
    fields_reviewing = set(doc.get('recheck_fields', []))
    # A follow-up discussion can reopen a dependency after the initial edit was
    # synchronized. Keep the latest applicable edit honest about that state.
    latest = {}
    for change in doc.get('canvas_changes', []):
        latest[change['reference_id']] = change
    candidates = [c for c in doc.get('canvas_changes', []) if c['status'] == 'pending' or
                  c['status'] == 'synchronized' and latest.get(c['reference_id']) is c]
    for change in candidates:
        remaining = []
        for rid in sorted(set(change['requirement_ids']) & req_reviews):
            remaining.append({'kind': 'requirement', 'id': rid, 'label': '关联需求待核对'})
        for fid in change['field_ids']:
            row = resolutions.get(fid)
            if fid in fields_reviewing or row and row['state'] == 'review':
                remaining.append({'kind': 'field', 'id': fid, 'label': '规范条款待同步'})
        for key in change['delivery_keys']:
            if checks.get(key, {}).get('state', 'missing') in ('missing', 'review'):
                remaining.append({'kind': 'delivery', 'id': key, 'label': '交接检查待更新'})
        targets = [('summary', 'summary', '产品摘要', doc.get('summary', ''))]
        targets += [('flow', 'step-'+str(i), '工作流程', step['label']) for i,step in enumerate(doc.get('flow',[]))]
        collections = (
            ('requirement', 'requirements', change['requirement_ids'], ('name', 'value')),
            ('module', 'modules', change['module_ids'], ('name', 'purpose', 'boundary')),
            ('interface', 'module_links', change['link_ids'], ('description', 'protocol', 'signals', 'voltage', 'conditions')),
            ('field', 'field_resolutions', change['field_ids'], ('conclusion',)),
            ('delivery', 'delivery_checks', change['delivery_keys'], ('conclusion', 'next_action')))
        for kind, collection, ids, names in collections:
            for row in doc.get(collection, []):
                key = row.get('id', row.get('field_id', row.get('key')))
                if key in ids and row.get('state') != 'not_applicable':
                    text = '\n'.join('、'.join(row[n]) if isinstance(row.get(n), list) else str(row.get(n, '')) for n in names)
                    targets.append((kind, key, row.get('name', row.get('field_id', key)), text))
        for row in doc.get('external_interfaces', []):
            if set(row.get('requirement_ids', [])) & set(change['requirement_ids']):
                targets.append(('external_interface', row['id'], row['name'],
                                '\n'.join(str(row.get(k, '')) for k in ('purpose', 'external_object', 'protocol', 'signals', 'voltage'))))
        for kind, key, label, text in targets:
            if _obsolete(text, change['before'], change['after']):
                remaining.append({'kind': kind, 'id': key, 'label': label,
                                  'reason': '仍保留被替换器件的当前方案表述', 'excerpt': text[:450]})
        # A newer edit can supersede the previous destination before its model run.
        current = next((r for r in doc.get('device_references', []) if r['id'] == change['reference_id']), None)
        if current and any(current.get(k) != change['after'].get(k) for k in CHOICE_FIELDS):
            change.update(status='superseded', remaining=[], synchronized_version=None)
        elif not remaining:
            change.update(status='synchronized', remaining=[], synchronized_version=doc.get('version', 0) + 1)
        else:
            change.update(status='pending',remaining=remaining,synchronized_version=None)
    completed = [c for c in doc.get('canvas_changes', []) if c['status'] != 'pending'][-20:]
    doc['canvas_changes'] = sorted(completed + pending(doc), key=lambda c: c['created_version'])


def view(doc):
    history = copy.deepcopy(doc.get('canvas_changes', []))
    waiting = [c for c in history if c['status'] == 'pending']
    return {'revision': 'canvas-sync/1', 'version': doc['version'], 'pending_count': len(waiting),
            'history': history, 'latest': history[-1] if history else None,
            'requirement_ids': sorted({rid for c in waiting for rid in c['requirement_ids']}),
            'module_ids': sorted({mid for c in waiting for mid in c['module_ids']}),
            'label': '正在同步器件调整与相关画布' if waiting else '器件调整已同步' if history else '当前方案保持同步'}
