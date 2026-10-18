"""Validate the saved two-round acceptance evidence and write a concise receipt."""
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / '.runtime/verification/ui-audit'


def report(name):
    data = json.loads((OUT / name).read_text(encoding='utf-8'))
    assert data.get('ok') is True, name
    return data


def main():
    categories = {}
    for label, name, expected in [
        ('interaction', 'interaction.json', 48),
        ('connectivity', 'connectivity.json', 9),
        ('restart', 'restart.json', 7),
        ('keyboard', 'keyboard.json', 9),
        ('real_convergence', 'real-convergence.json', 11),
        ('production_read_only', 'production-ui.json', 11),
    ]:
        data = report(name)
        assert len(data['rounds']) == 2, name
        counts = []
        for row in data['rounds']:
            assert len(row['checks']) == expected, name
            assert not row.get('console_errors'), name
            if label == 'real_convergence':
                assert row['provider'] == 'real_codex_cli'
                assert row['coverage_closed'] == 50
                assert row['conversation_user_turns'] > 3
            if label == 'production_read_only':
                assert row['mutations'] == []
            counts.append(len(row['checks']))
        categories[label] = {'counts': counts, 'evidence': name}

    base_counts = []
    for n in (1, 2):
        data = report(f'base-final-round-{n}/browser.json')
        assert len(data['checks']) == 40 and not data['console_errors']
        assert data['provider'] == 'controlled_cli_fixture'
        base_counts.append(len(data['checks']))
    categories['base_ui'] = {'counts': base_counts, 'evidence': 'base-final-round-{1,2}/browser.json'}
    status = report('approved-status.json')
    assert len(status['rounds']) == 2
    for row in status['rounds']:
        assert row['approved_header_consistent'] and row['recheck_did_not_repeat_questions']
    categories['approval_status'] = {'counts': [2, 2], 'evidence': 'approved-status.json'}

    unit_counts = []
    for n in (1, 2):
        text = (OUT / f'unit-round-{n}.log').read_text(encoding='utf-8')
        count = int(re.search(r'Ran (\d+) tests', text).group(1))
        assert count == 45 and text.rstrip().endswith('OK')
        unit_counts.append(count)

    load = report('load.json')
    assert len(load['rounds']) == 2
    for row in load['rounds']:
        assert row['accepted'] == row['backpressure_409'] == 20
        assert row['duplicate_did_not_reenqueue']
        assert row['read_requests'] == 240 and row['processed_versions'] == 20
        assert row['parallel_projects'] == 12 and row['max_running_cli'] == 2
        assert row['errors_500'] == 0
    final = report('real-final-state.json')
    assert len(final['rounds']) == 2
    for row in final['rounds']:
        assert row['coverage_closed'] == row['coverage_total'] == 50
        assert row['review_status'] == 'ready'
        assert row['open_questions'] == row['recheck_count'] == row['unsettled_requirements'] == 0

    release = report('production-release.json')
    assert release['original_projects_preserved'] == 3
    assert release['documents_and_messages_identical']
    assets = report('release-assets.json')['assets']
    for relative, digest in assets.items():
        assert hashlib.sha256((ROOT / 'frontend/dist' / relative).read_bytes()).hexdigest() == digest
    sources = {}
    for folder in ('backend', 'frontend/src'):
        for file in sorted((ROOT / folder).rglob('*')):
            if file.is_file() and file.suffix in ('.py', '.tsx', '.ts', '.css', '.json'):
                sources[str(file.relative_to(ROOT))] = hashlib.sha256(file.read_bytes()).hexdigest()
    browser_count = sum(sum(v['counts']) for k, v in categories.items() if k != 'production_read_only')
    assert browser_count == 252
    result = {
        'ok': True, 'created_at': datetime.now(timezone.utc).isoformat(),
        'functional_browser_assertions': browser_count,
        'additional_production_read_only_assertions': sum(categories['production_read_only']['counts']),
        'core_tests_per_round': unit_counts, 'categories': categories,
        'real_user_turns': [r['conversation_user_turns'] for r in report('real-convergence.json')['rounds']],
        'real_coverage_closed': [50, 50], 'load_rounds': 2,
        'original_projects_preserved': 3, 'source_sha256': sources, 'release_assets_sha256': assets,
        'scope': 'Standalone requirements UI and multi-turn document convergence; excludes hardware validation and main-pipeline integration.',
    }
    (OUT / 'summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k not in ('source_sha256', 'release_assets_sha256', 'categories')}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
