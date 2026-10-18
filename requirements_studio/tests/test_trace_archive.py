import errno
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from backend.trace_archive import archive_run_inputs


class TerminalInputArchiveTests(unittest.TestCase):
    def setUp(self):
        base=Path(__file__).resolve().parents[1]/'.runtime/tests';base.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=base);self.root=Path(self.temp.name)

    def tearDown(self):self.temp.cleanup()

    def test_root_and_repair_inputs_are_lossless_but_answers_and_receipts_stay_readable(self):
        expected={}
        for rel in ('context.json','response.schema.json','repair-1/context.json'):
            file=self.root/rel;file.parent.mkdir(parents=True,exist_ok=True)
            expected[rel]=('完整原话与修复上下文。'*300).encode();file.write_bytes(expected[rel])
        for name in ('answer.json','receipt.json','validation-error.json'):(self.root/name).write_bytes(b'{}')
        result=archive_run_inputs(self.root);self.assertEqual(result['files'],3)
        with zipfile.ZipFile(self.root/'inputs.zip') as archive:
            manifest=json.loads(archive.read('inputs-manifest.json'))['sha256']
            for rel,raw in expected.items():
                self.assertEqual(archive.read(rel),raw);self.assertEqual(manifest[rel],hashlib.sha256(raw).hexdigest())
                self.assertFalse((self.root/rel).exists())
        for name in ('answer.json','receipt.json','validation-error.json'):self.assertEqual((self.root/name).read_bytes(),b'{}')
        self.assertEqual(archive_run_inputs(self.root),{'files':0})

    def test_failed_atomic_replace_preserves_original_inputs_and_previous_archive(self):
        source=self.root/'context.json';source.write_text('原记录'*100)
        archive_run_inputs(self.root);before=(self.root/'inputs.zip').read_bytes()
        source.write_text('后续补充'*100)
        with patch.object(Path,'replace',side_effect=OSError(errno.EDQUOT,'fixture disk quota')):
            with self.assertRaises(OSError):archive_run_inputs(self.root)
        self.assertEqual(source.read_text(),'后续补充'*100)
        self.assertEqual((self.root/'inputs.zip').read_bytes(),before)
        self.assertFalse(list(self.root.glob('*.tmp')))

    def test_new_repair_inputs_extend_the_existing_verified_archive(self):
        (self.root/'context.json').write_text('原上下文');archive_run_inputs(self.root)
        repair=self.root/'repair-1';repair.mkdir();(repair/'context.json').write_text('复核后的上下文')
        archive_run_inputs(self.root)
        with zipfile.ZipFile(self.root/'inputs.zip') as archive:
            self.assertEqual(archive.read('context.json').decode(),'原上下文')
            self.assertEqual(archive.read('repair-1/context.json').decode(),'复核后的上下文')
