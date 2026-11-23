import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, data_dir):
        self.root = Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "incoming").mkdir(exist_ok=True)
        self.path = self.root / "files.sqlite3"
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS uploads (
                  id TEXT PRIMARY KEY, name TEXT NOT NULL, mime TEXT NOT NULL,
                  size INTEGER NOT NULL, received INTEGER NOT NULL DEFAULT 0,
                  created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                  id TEXT PRIMARY KEY, name TEXT NOT NULL, mime TEXT NOT NULL,
                  size INTEGER NOT NULL, source TEXT NOT NULL, source_kind TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'pending', drive_id TEXT,
                  drive_url TEXT, permission_id TEXT, error TEXT,
                  telegram_chat INTEGER, telegram_message INTEGER,
                  created REAL NOT NULL, updated REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created);
                CREATE UNIQUE INDEX IF NOT EXISTS jobs_telegram_message
                  ON jobs(telegram_chat, telegram_message) WHERE telegram_chat IS NOT NULL;
            """)
            db.execute("UPDATE jobs SET status='pending' WHERE status='uploading'")
            db.execute("""UPDATE jobs SET status=CASE WHEN drive_id IS NULL
                         THEN 'pending' ELSE 'ready' END WHERE status='deleting'""")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def one(self, table, item_id):
        if table not in ("jobs", "uploads"):
            raise ValueError("Unknown table")
        with self.db() as db:
            row = db.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
            return dict(row) if row else None

    def jobs(self):
        with self.db() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM jobs ORDER BY created DESC")]

    def enqueue(self, item_id, name, mime, size, source, kind, chat=None, message=None):
        now = time.time()
        with self.db() as db:
            db.execute("""INSERT INTO jobs
                (id,name,mime,size,source,source_kind,telegram_chat,telegram_message,created,updated)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (item_id, name, mime, size, source, kind, chat, message, now, now))

    def next_job(self):
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE status='pending' ORDER BY created LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE jobs SET status='uploading', updated=? WHERE id=?", (time.time(), row['id']))
                return dict(row)

    def update_job(self, item_id, **fields):
        allowed = {'status', 'drive_id', 'drive_url', 'permission_id', 'error', 'name'}
        if not fields or not set(fields) <= allowed:
            raise ValueError('Invalid job fields')
        fields['updated'] = time.time()
        keys = list(fields)
        with self.db() as db:
            db.execute(
                'UPDATE jobs SET ' + ','.join(f'{key}=?' for key in keys) + ' WHERE id=?',
                [fields[k] for k in keys] + [item_id]
            )

    def delete_job(self, item_id):
        with self.db() as db:
            db.execute('DELETE FROM jobs WHERE id=?', (item_id,))

    def reclaim_expired_uploads(self):
        cutoff = time.time() - 24 * 3600
        with self.db() as db:
            stale = [row['id'] for row in db.execute('SELECT id FROM uploads WHERE created<?', (cutoff,))]
            db.execute('DELETE FROM uploads WHERE created<?', (cutoff,))
        for item_id in stale:
            (self.root / 'incoming' / (item_id + '.part')).unlink(missing_ok=True)
