import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from backend.codex import Runner
from backend.store import Store


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder=ROOT/'.runtime/tests';folder.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=folder);self.runtime=Path(self.temp.name)
        self.binary=self.runtime/'codex'
        self.binary.write_text(f'#!{sys.executable}\nimport runpy,sys\nsys.path.insert(0,{str(ROOT/"tests")!r})\nrunpy.run_path({str(ROOT/"tests/provider_fixture.py")!r},run_name="__main__")\n',encoding='utf-8');self.binary.chmod(0o755)
        self.store=Store(self.runtime/'studio.sqlite3');self.project=self.store.create_project(example=True)
        self.patcher=patch('backend.codex.shutil.which',return_value=str(self.binary));self.patcher.start()

    async def asyncTearDown(self):
        self.patcher.stop();self.temp.cleanup()

    async def test_timeout_terminates_the_child_and_retains_document(self):
        runner=Runner(self.store,self.runtime,timeout_seconds=2)
        run,_=self.store.begin_run(self.project['id'],'[慢分析] 测试超时','timeout')
        await runner._run(run['id'])
        self.assertEqual(self.store.get_run(run['id'])['status'],'failed')
        self.assertIn('2 秒',self.store.get_run(run['id'])['error'])
        self.assertTrue(any('整理需求、检查缺项' in e['payload'].get('message','') for e in self.store.events(run['id'],0)))
        self.assertEqual(self.store.get_project(self.project['id'])['document'],self.project['document'])
        self.assertFalse(runner.processes)

    async def test_cancel_running_process_never_commits_a_partial_answer(self):
        runner=Runner(self.store,self.runtime)
        run,_=self.store.begin_run(self.project['id'],'[慢分析] 测试取消','cancel')
        runner.start(run['id'])
        for _ in range(100):
            if run['id'] in runner.processes:break
            await asyncio.sleep(.01)
        process=runner.processes[run['id']]
        await runner.cancel(run['id'])
        self.assertIsNotNone(process.returncode)
        self.assertEqual(self.store.get_run(run['id'])['status'],'cancelled')
        self.assertEqual(self.store.get_project(self.project['id'])['document'],self.project['document'])
        await runner.close()


if __name__=='__main__':unittest.main(verbosity=2)
