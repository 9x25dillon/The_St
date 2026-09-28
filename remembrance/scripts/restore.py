"""Restore a server backup into a NEW EMPTY directory, never over live data."""
import argparse
from pathlib import Path
import sqlite3
from contextlib import closing
import json
import zipfile


def restore(archive, target):
    target = Path(target)
    if target.exists() and any(target.iterdir()):
        raise ValueError('Restore target must be empty; stop the server and use a new directory.')
    with zipfile.ZipFile(archive) as z:
        info = json.loads(z.read('backup.json'))
        if info.get('format') != 'remembrance-server-backup' or info.get('version') != 1:
            raise ValueError('Unsupported backup')
        for item in z.infolist():
            parts = Path(item.filename).parts
            if item.filename not in ('remembrance.sqlite3', 'backup.json') and not (len(parts) == 2 and parts[0] == 'media' and parts[1] not in ('.', '..')):
                raise ValueError('Unexpected backup path')
            if item.file_size > 32 * 1024 * 1024 and item.filename != 'remembrance.sqlite3':
                raise ValueError('Oversized media entry')
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        (target / 'media').mkdir(exist_ok=True, mode=0o700)
        z.extractall(target)
    with closing(sqlite3.connect(target / 'remembrance.sqlite3')) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Restored database failed integrity check')
        for (name,) in db.execute('SELECT filename FROM media'):
            if Path(name).name != name or not (target / 'media' / name).is_file():
                raise ValueError('Missing or invalid media in backup')
    print(f'Restored to {target}. Set ownership and a new SECRET_KEY; validate before routing traffic.')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('archive')
    p.add_argument('target')
    args = p.parse_args()
    restore(args.archive, args.target)
