"""Versioned SQLite setup and explicit upgrades with verified database backups."""
import argparse
from contextlib import closing
import os
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parent
SCHEMA_VERSION = 3


def version(conn):
    return conn.execute('PRAGMA user_version').fetchone()[0]


def apply_migrations(conn):
    """Called only for a fresh database or after making an upgrade backup."""
    conn.execute('BEGIN IMMEDIATE')
    try:
        for number in range(version(conn) + 1, SCHEMA_VERSION + 1):
            path = next((ROOT / 'migrations').glob(f'{number:03d}_*.sql'))
            _execute_script(conn, path.read_text())
        if conn.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('Foreign-key check failed; migration was rolled back.')
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def initialize(conn):
    current = version(conn)
    if current == 0:
        # Recheck under a write lock when multiple fresh workers start together.
        conn.execute('BEGIN IMMEDIATE')
        try:
            if version(conn) == 0:
                tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                if tables:
                    raise RuntimeError('Unversioned database contains tables; review it before upgrading.')
                scripts = [(ROOT / 'schema.sql').read_text()]
                scripts.extend(path.read_text() for path in sorted((ROOT / 'migrations').glob('*.sql')))
                for script in scripts:
                    _execute_script(conn, script)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    current = version(conn)
    if current != SCHEMA_VERSION:
        raise RuntimeError(f'Database schema {current}; expected {SCHEMA_VERSION}. Stop the app and run '
                           'python database.py DATABASE_PATH --backup BACKUP_PATH before restarting.')


def _execute_script(conn, script):
    """Execute complete SQL statements without executescript's implicit COMMIT."""
    statement = ''
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ''
    if statement.strip():
        raise ValueError('Incomplete migration statement')


def upgrade(database_path, backup_path):
    """Call with application writers stopped; retain the verified pre-upgrade backup."""
    source, target = Path(database_path).resolve(), Path(backup_path).resolve()
    if not source.is_file() or source == target:
        raise ValueError('Choose an existing database and a separate, new backup path.')
    with closing(sqlite3.connect(f'{source.as_uri()}?mode=rw', uri=True, timeout=15)) as conn:
        current = version(conn)
        if current == SCHEMA_VERSION:
            return False
        if current not in range(1, SCHEMA_VERSION):
            raise ValueError(f'Unsupported source schema: {current}')
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(target)) as snapshot:
            conn.backup(snapshot)
            if snapshot.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Backup integrity check failed; migration was not started.')
        conn.execute('PRAGMA foreign_keys=ON')
        apply_migrations(conn)
    return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Stop the app before upgrading. Preserve media separately.')
    parser.add_argument('database')
    parser.add_argument('--backup', required=True)
    args = parser.parse_args()
    changed = upgrade(args.database, args.backup)
    print(f'Database upgraded to schema {SCHEMA_VERSION}; verified pre-upgrade backup retained.' if changed else 'Database is already current.')
