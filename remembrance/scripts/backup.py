"""Consistent database + media backup. Pause writes/erasure during backup.
Usage: python scripts/backup.py /path/to/data /path/to/backup.zip
Uses sqlite backup API to include committed WAL data. Output is sensitive.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import zipfile


def backup(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    if not (source / 'remembrance.sqlite3').is_file():
        raise ValueError('Source database not found')
    if target == source or source in target.parents:
        raise ValueError('Write backups outside the live data directory')
    with tempfile.TemporaryDirectory() as tmp:
        db_copy = Path(tmp) / 'remembrance.sqlite3'
        with closing(sqlite3.connect(f'file:{source / "remembrance.sqlite3"}?mode=ro', uri=True)) as live, closing(sqlite3.connect(db_copy)) as snapshot:
            live.backup(snapshot)
            if snapshot.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Database integrity check failed')
            names = [r[0] for r in snapshot.execute('SELECT filename FROM media')]
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, 'wb') as stream, zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as z:
                z.write(db_copy, 'remembrance.sqlite3')
                for name in names:
                    if Path(name).name != name:
                        raise ValueError('Unsafe stored filename')
                    z.write(source / 'media' / name, 'media/' + name)
                z.writestr('backup.json', json.dumps({'format':'remembrance-server-backup','version':1,'created':datetime.now(timezone.utc).isoformat(),'media_files':len(names)}))
        except Exception:
            target.unlink(missing_ok=True)
            raise
    print(f'Backup written: {target} ({len(names)} media files). Encrypt before off-site storage.')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('source')
    p.add_argument('target')
    args = p.parse_args()
    backup(args.source, args.target)
