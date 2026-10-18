import asyncio
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from backend.storage_budget import WriteBudget,RESERVE_BYTES


class StorageBudgetTests(unittest.TestCase):
    def test_reservation_is_unlinked_and_release_is_idempotent(self):
        root=Path(__file__).resolve().parents[1]/'.runtime/tests';root.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            budget=WriteBudget.acquire(Path(temporary));handle=budget._handle
            self.assertEqual(os.fstat(handle.fileno()).st_size,RESERVE_BYTES)
            self.assertEqual(os.fstat(handle.fileno()).st_nlink,0)
            self.assertGreaterEqual(budget.allocated_bytes,RESERVE_BYTES)
            self.assertEqual(list(Path(temporary).iterdir()),[])
            budget.release();self.assertTrue(handle.closed);budget.release()


class ReservationCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_reservation_releases_the_late_thread_result(self):
        started=threading.Event();finish=threading.Event();released=threading.Event()
        class Budget:
            calls=0
            def release(self):self.calls+=1;released.set()
        budget=Budget()
        def slow_acquire(folder):
            started.set()
            if not finish.wait(5):raise RuntimeError('fixture thread did not finish')
            return budget
        with patch.object(WriteBudget,'acquire',side_effect=slow_acquire):
            task=asyncio.create_task(WriteBudget.reserve(Path('.')))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait,5))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
                self.assertFalse(released.is_set())
            finally:finish.set()
            self.assertTrue(await asyncio.to_thread(released.wait,5))
        self.assertEqual(budget.calls,1)
