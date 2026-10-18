"""Schema setup and additive migration are one durable transaction."""
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.store import Store
from backend.domain_store import DomainStore


class StoreInitializationTests(unittest.TestCase):
    def setUp(self):
        (ROOT/'.runtime/tests').mkdir(parents=True,exist_ok=True)
        self.temporary=tempfile.TemporaryDirectory(dir=ROOT/'.runtime/tests')
        self.path=Path(self.temporary.name)/'studio.sqlite3'

    def tearDown(self):self.temporary.cleanup()

    @staticmethod
    def fail_after_domain(self,db):
        assert db.in_transaction,'Core schema was committed before domain setup'
        DomainStore.init_domain(self,db)
        assert db.in_transaction,'Domain schema committed the migration early'
        raise RuntimeError('fixture migration interrupted')

    def test_new_database_rolls_back_the_entire_schema_on_failure(self):
        with patch.object(Store,'init_domain',self.fail_after_domain):
            with self.assertRaisesRegex(RuntimeError,'fixture migration interrupted'):Store(self.path)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),[])
        store=Store(self.path)
        self.assertEqual(store.create_project()['document']['version'],0)

    def test_failed_legacy_migration_preserves_rows_and_original_columns(self):
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE projects(id TEXT PRIMARY KEY,title TEXT NOT NULL,document TEXT NOT NULL,is_example INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL)')
            db.execute('CREATE TABLE runs(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),message_id TEXT NOT NULL,request_id TEXT NOT NULL,base_version INTEGER NOT NULL,status TEXT NOT NULL,error TEXT NOT NULL DEFAULT \'\',created_at TEXT NOT NULL,finished_at TEXT,UNIQUE(project_id,request_id))')
            db.execute("INSERT INTO projects VALUES('original','Original','{\"version\":7}',0,'unchanged')")
            original_columns=[row[1] for row in db.execute('PRAGMA table_info(runs)')]
        with patch.object(Store,'init_domain',self.fail_after_domain):
            with self.assertRaisesRegex(RuntimeError,'fixture migration interrupted'):Store(self.path)
        with sqlite3.connect(self.path) as db:
            self.assertEqual([row[1] for row in db.execute('PRAGMA table_info(runs)')],original_columns)
            self.assertEqual(db.execute('SELECT * FROM projects').fetchall(),[('original','Original','{"version":7}',0,'unchanged')])
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='domain_jobs'").fetchone())
        Store(self.path)
        with sqlite3.connect(self.path) as db:
            self.assertIn('kind',[row[1] for row in db.execute('PRAGMA table_info(runs)')])
            self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0],'ok')
            self.assertEqual(db.execute('SELECT document FROM projects').fetchone()[0],'{"version":7}')


if __name__=='__main__':unittest.main()
