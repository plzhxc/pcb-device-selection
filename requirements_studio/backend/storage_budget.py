"""A bounded, crash-safe cushion for saving a returned model result.

Quota can change during inference. An unlinked open file reserves blocks until
the response arrives, then releases them for receipts and the DB transaction.
It cannot guarantee availability during an external filesystem failure.
"""
from __future__ import annotations
import asyncio
import os
from pathlib import Path
import tempfile
import threading

RESERVE_BYTES=8*1024*1024


class WriteBudget:
    def __init__(self,handle,allocated_bytes):
        self._handle=handle
        self.requested_bytes=RESERVE_BYTES
        self.allocated_bytes=allocated_bytes

    @classmethod
    async def reserve(cls,folder:Path):
        # Cancelling to_thread cannot stop its filesystem work. Transfer
        # ownership under a lock, so a late result is released by that thread
        # even when the awaiting task (or its event loop) has already ended.
        lock=threading.Lock();state={'abandoned':False,'budget':None}
        def acquire():
            budget=cls.acquire(folder)
            with lock:
                if state['abandoned']:
                    budget.release();return None
                state['budget']=budget
            return budget
        try:
            budget=await asyncio.to_thread(acquire)
        except BaseException:
            with lock:
                state['abandoned']=True
                budget=state['budget'];state['budget']=None
            if budget is not None:budget.release()
            raise
        with lock:state['budget']=None
        return budget

    @classmethod
    def acquire(cls,folder:Path):
        # TemporaryFile unlinks its name immediately on this Linux filesystem;
        # process exit also releases it, with no abandoned reservation to clean.
        handle=tempfile.TemporaryFile(dir=folder)
        try:
            for _ in range(RESERVE_BYTES//(1024*1024)):
                handle.write(os.urandom(1024*1024))
            handle.flush();os.fsync(handle.fileno())
            stat=os.fstat(handle.fileno())
            if stat.st_size!=RESERVE_BYTES:raise OSError('本轮保存空间预留未完整写入。')
            return cls(handle,stat.st_blocks*512)
        except BaseException:
            handle.close();raise

    def release(self):
        handle,self._handle=self._handle,None
        if handle is not None:handle.close()
