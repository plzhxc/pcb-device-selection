from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
from backend.store import Store
from backend.history_codec import history_text,history_document
from compact_history import compact_database,fingerprints


class HistoricalDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'.runtime/tests')
        self.path=Path(self.temp.name)/'studio.sqlite3';self.store=Store(self.path)
        self.project=self.store.create_project();self.pid=self.project['id']

    def tearDown(self):self.temp.cleanup()

    def versions(self):
        update={'id':'','category':'functions','name':'采样与记录','value':'保留用户原话、数量和单位；不得默改设计目标。'*250,'unit':'','priority':'must','state':'stated'}
        first=self.store.edit_requirement(self.pid,0,update)
        with self.store.connect() as db:
            raw=history_text(db.execute('SELECT document FROM revisions WHERE project_id=? AND version=1',(self.pid,)).fetchone()[0])
            db.execute('UPDATE revisions SET document=? WHERE project_id=? AND version=1',(raw,self.pid))
        update.update(id=first['document']['requirements'][0]['id'],value='新决定仍需保留最早的依据与完整记录。'*250)
        second=self.store.edit_requirement(self.pid,1,update)
        return first,second

    def test_plain_and_packed_versions_survive_restart_with_identical_public_history(self):
        first,second=self.versions();expected=self.store.get_project(self.pid)
        reopened=Store(self.path);actual=reopened.get_project(self.pid)
        self.assertEqual(actual['document'],expected['document']);self.assertEqual(actual['revisions'],expected['revisions'])
        self.assertEqual(actual['messages'],expected['messages']);self.assertEqual(actual['markdown'],expected['markdown'])
        with reopened.connect() as db:
            rows=db.execute('SELECT version,document FROM revisions ORDER BY version').fetchall()
        self.assertIsInstance(rows[0]['document'],str);self.assertIsInstance(rows[1]['document'],bytes)
        self.assertEqual(history_document(rows[0]['document']),first['document'])
        self.assertEqual(history_document(rows[1]['document']),second['document'])

    def test_offline_compaction_preserves_every_logical_version_and_readable_backup(self):
        self.versions();before=self.store.get_project(self.pid)
        with self.store.connect() as db:
            for row in db.execute('SELECT project_id,version,document FROM revisions').fetchall():
                db.execute('UPDATE revisions SET document=? WHERE project_id=? AND version=?',(history_text(row['document']),row['project_id'],row['version']))
            expected=fingerprints(db)
        backup=Path(self.temp.name)/'before-history.zip'
        result=compact_database(self.path,backup)
        self.assertEqual(result['logical_sha256'],expected);self.assertTrue(result['all_document_bytes_unchanged'])
        self.assertLess(result['database_bytes_after'],result['database_bytes_before'])
        after=Store(self.path).get_project(self.pid)
        for key in ('document','messages','revisions','markdown'):self.assertEqual(after[key],before[key])
        with zipfile.ZipFile(backup) as archive,closing(sqlite3.connect(':memory:')) as saved:
            self.assertIsNone(archive.testzip());saved.deserialize(archive.read('studio.sqlite3'))
            self.assertEqual(fingerprints(saved),expected)
