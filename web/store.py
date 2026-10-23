import json
import os
import sqlite3
from pathlib import Path
from contextlib import contextmanager
from cryptography.fernet import Fernet


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = self.directory / 'vault.key'
        if not key.exists():
            fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(Fernet.generate_key())
        self.cipher = Fernet(key.read_bytes().strip())
        self.path = self.directory / 'vault.sqlite3'
        with self.db() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users(uid INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY, uid INTEGER, kind TEXT, body TEXT);
            CREATE INDEX IF NOT EXISTS owned ON items(uid, kind);
            CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, uid INTEGER, expires INTEGER);
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, ts TEXT DEFAULT CURRENT_TIMESTAMP, uid INTEGER, action TEXT, target TEXT);
            ''')
        os.chmod(self.path, 0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def allowed(self, uid, owner):
        with self.db() as db:
            return uid == owner or db.execute('SELECT 1 FROM users WHERE uid=?', (uid,)).fetchone() is not None

    def items(self, uid, kind):
        with self.db() as db:
            return [dict(json.loads(r['body']), id=r['id']) for r in db.execute('SELECT * FROM items WHERE uid=? AND kind=?', (uid, kind))]

    def get(self, uid, kind, item_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM items WHERE id=? AND uid=? AND kind=?', (item_id, uid, kind)).fetchone()
        return dict(json.loads(row['body']), id=row['id']) if row else None

    def put(self, uid, kind, item_id, body):
        with self.db() as db:
            db.execute('INSERT INTO items VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body WHERE items.uid=excluded.uid AND items.kind=excluded.kind', (item_id, uid, kind, json.dumps(body)))

    def remove(self, uid, kind, item_id):
        with self.db() as db:
            db.execute('DELETE FROM items WHERE id=? AND uid=? AND kind=?', (item_id, uid, kind))

    def encrypt(self, obj):
        return self.cipher.encrypt(json.dumps(obj).encode()).decode()

    def decrypt(self, value):
        return json.loads(self.cipher.decrypt(value.encode()))

    def log(self, uid, action, target=''):
        with self.db() as db:
            db.execute('INSERT INTO audit(uid,action,target) VALUES(?,?,?)', (uid, action, target))
