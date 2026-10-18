"""Verified compressed SQLite snapshots without another large GPFS allocation."""
from contextlib import closing
from pathlib import Path
import os
import sqlite3
import zipfile


def backup_bundle(destination: Path, database: Path, assets: Path | None = None):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True)) as source, closing(sqlite3.connect(':memory:')) as snapshot:
        source.backup(snapshot)
        if snapshot.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('源数据库完整性未通过，停止发布。')
        payload = snapshot.serialize()
    with zipfile.ZipFile(destination, 'x', zipfile.ZIP_LZMA) as archive:
        archive.writestr('studio.sqlite3', payload)
        if assets and assets.exists():
            for file in assets.rglob('*'):
                if file.is_file():archive.write(file, 'dist/'+file.relative_to(assets).as_posix())
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip() is not None:raise ValueError('压缩备份校验失败，停止发布。')
    with destination.open('rb') as stream:os.fsync(stream.fileno())
    return destination
