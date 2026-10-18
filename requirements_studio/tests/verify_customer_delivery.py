"""Audit an already human-reviewed UI journey; never creates or answers a project."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import sys
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.handoff import build_spec, verify_archive
from check_case import check_case


def verify(directory: Path, key: str) -> dict:
    index = json.loads((directory / 'index.json').read_text())
    entry = next(row for row in index['cases'] if row['key'] == key)
    project = json.loads(gzip.decompress((directory / (key + '.json.gz')).read_bytes()))
    assert project['id'] == entry['project_id']
    assert project['review']['status'] == 'approved'
    assert any(row['kind'] == 'explicit_ui_approval' and row.get('review_note') for row in entry['actions'])
    assert not entry.get('browser_errors'), entry.get('browser_errors')
    archive_path = directory / (key + '-handoff.zip')
    content = archive_path.read_bytes()
    archive = verify_archive(content)
    assert archive['ok'], archive
    with ZipFile(archive_path) as package:
        assert package.read('requirements.md').decode() == project['markdown']
        assert json.loads(package.read('requirement-spec.json')) == build_spec(project)
    adapter = json.loads((directory / (key + '-adapter.json')).read_text())
    assert adapter['accepted'], adapter
    source_check = None
    if index['input_mode'] == 'original_txt':
        original = Path(entry['source']).read_bytes()
        assert hashlib.sha256(original).hexdigest() == entry['source_sha256']
        catalog = json.loads((ROOT / 'tests/phase4_case_catalog.json').read_text())
        case = next(row for row in catalog if row['id'] == entry['case_id'])
        first = next(row['content'] for row in project['messages'] if row['role'] == 'user')
        assert first == case['prompt'], 'Initial input differs from original TXT'
        choices = directory / (key + '-customer-decisions.json')
        decisions = json.loads(choices.read_text()) if choices.exists() else None
        roles = directory / (key + '-customer-roles.json')
        role_decisions = json.loads(roles.read_text()) if roles.exists() else None
        source_check = check_case(entry['case_id'], project, intent_decisions=decisions, role_decisions=role_decisions)
        assert source_check['ok'], source_check
    result = {
        'ok': True, 'key': key, 'project_id': project['id'],
        'version': project['document']['version'], 'source_check': source_check,
        'verified_at': datetime.now(timezone.utc).isoformat(),
        'checker_sha256': hashlib.sha256((ROOT / 'tests/check_case.py').read_bytes()).hexdigest(),
        'initial_checker_sha256': entry.get('checker_sha256'),
        'snapshot_sha256': hashlib.sha256((directory / (key + '.json.gz')).read_bytes()).hexdigest(),
        'archive_verification': archive, 'zip_sha256': hashlib.sha256(content).hexdigest(),
        'adapter': adapter, 'user_turns': entry['user_turns'],
        'human_review': next(row['review_note'] for row in reversed(entry['actions'])
                             if row['kind'] == 'explicit_ui_approval'),
        'scope': 'Requirements and downstream intake only; no hardware or netlist correctness claim.',
    }
    (directory / (key + '-verification.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    return {name: result[name] for name in ('ok', 'key', 'project_id', 'version', 'user_turns')}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('keys', nargs='+')
    args = parser.parse_args()
    for key in args.keys:
        print(json.dumps(verify(args.directory, key), ensure_ascii=False))
