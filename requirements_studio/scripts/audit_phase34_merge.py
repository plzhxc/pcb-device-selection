"""Read-only, row-by-row audit of the retained live-project merge.

Run prepare with idle/stopped services, then phase34_deploy.py --apply,
then check. The audit never changes either database or resumes any queue.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / '.runtime/customer-acceptance/merge-audit-before.json.gz'
REPORT = ROOT / '.runtime/customer-acceptance/merge-preservation.json'
TABLES = ('projects', 'messages', 'runs', 'events', 'revisions', 'input_queue',
          'domain_jobs', 'domain_job_events', 'evidence_snapshots',
          'domain_checks', 'handoff_artifacts')


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()


def database(path):
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.execute('BEGIN')
        assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
        for table in ('runs', 'domain_jobs'):
            active = db.execute(f"SELECT id FROM {table} WHERE status IN ('queued','running')").fetchall()
            assert not active, (str(path), table, 'active jobs', active)
        result = {}
        for table in TABLES:
            schema = db.execute(f'PRAGMA table_info({table})').fetchall()
            keys = [row[1] for row in sorted(schema, key=lambda row: row[5]) if row[5]]
            columns = [row[1] for row in schema]
            assert keys
            rows = {}
            for row in db.execute(f'SELECT * FROM {table}'):
                key = encoded([row[columns.index(name)] for name in keys]).decode()
                cells = [({'blob_sha256': digest(v), 'bytes': len(v)}
                          if isinstance(v, bytes) else v) for v in row]
                assert key not in rows
                rows[key] = digest(encoded(cells))
            result[table] = {'schema': schema, 'rows': rows}
        # JSON-normalize schema tuples before comparing a saved audit.
        return json.loads(encoded(result))


def files(directory):
    return {str(p.relative_to(directory)): digest(p.read_bytes())
            for p in directory.rglob('*') if p.is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'check'))
    args = parser.parse_args()
    main_db = database(ROOT / '.runtime/studio.sqlite3')
    source_db = database(ROOT / '.runtime/phase3/live/studio.sqlite3')
    target_runs = ROOT / '.runtime/runs'
    source_runs = ROOT / '.runtime/phase3/live/runs'
    if args.action == 'prepare':
        for table in TABLES:
            assert main_db[table]['schema'] == source_db[table]['schema'], table
            assert not main_db[table]['rows'].keys() & source_db[table]['rows'].keys(), table
        assert not {p.name for p in target_runs.iterdir()} & {p.name for p in source_runs.iterdir()}
        state = {'at': datetime.now(timezone.utc).isoformat(), 'main': main_db,
                 'source': source_db, 'source_run_files': files(source_runs)}
        AUDIT.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT.open('xb') as stream:
            stream.write(gzip.compress(encoded(state)))
        print(json.dumps({'ok': True, 'action': 'prepare',
                          'main_projects': len(main_db['projects']['rows']),
                          'source_projects': len(source_db['projects']['rows']),
                          'tables_without_key_conflicts': len(TABLES),
                          'source_run_files': len(state['source_run_files'])}))
        return
    state = json.loads(gzip.decompress(AUDIT.read_bytes()))
    assert source_db == state['source'], 'Source database changed after audit'
    counts = {}
    for table in TABLES:
        expected = state['main'][table]['rows'] | state['source'][table]['rows']
        assert main_db[table]['schema'] == state['main'][table]['schema'], table
        assert main_db[table]['rows'] == expected, (table, 'Missing, modified or unexpected rows')
        counts[table] = {'original': len(state['main'][table]['rows']),
                         'imported': len(state['source'][table]['rows']),
                         'after': len(expected)}
    assert files(source_runs) == state['source_run_files'], 'Source run files changed'
    for name, expected in state['source_run_files'].items():
        assert digest((target_runs / name).read_bytes()) == expected, name
    result = {'ok': True, 'at': datetime.now(timezone.utc).isoformat(),
              'tables': counts, 'unchanged_source': True,
              'all_original_rows_preserved': True, 'all_source_rows_preserved': True,
              'imported_run_files_verified': len(state['source_run_files']),
              'before_audit_sha256': digest(AUDIT.read_bytes()),
              'scope': 'Exact row values, including retained queues, history, evidence and handoff BLOBs; source run files copied byte-for-byte.'}
    REPORT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
