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
                  telegram_user INTEGER, telegram_username TEXT,
                  uploaded INTEGER NOT NULL DEFAULT 0, speed REAL NOT NULL DEFAULT 0,
                  upload_started REAL, upload_finished REAL,
                  created REAL NOT NULL, updated REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created);
                CREATE UNIQUE INDEX IF NOT EXISTS jobs_telegram_message
                  ON jobs(telegram_chat, telegram_message) WHERE telegram_chat IS NOT NULL;
                CREATE TABLE IF NOT EXISTS users (
                  telegram_id INTEGER PRIMARY KEY, username TEXT,
                  display_name TEXT, enabled INTEGER NOT NULL DEFAULT 0,
                  is_admin INTEGER NOT NULL DEFAULT 0,
                  drive_folder_id TEXT, created REAL NOT NULL, updated REAL NOT NULL
                );
            """)
            existing = {row['name'] for row in db.execute('PRAGMA table_info(jobs)')}
            migrations = {
                'telegram_user': 'INTEGER', 'telegram_username': 'TEXT',
                'uploaded': 'INTEGER NOT NULL DEFAULT 0',
                'speed': 'REAL NOT NULL DEFAULT 0', 'upload_started': 'REAL',
                'upload_finished': 'REAL',
            }
            for name, definition in migrations.items():
                if name not in existing:
                    db.execute(f'ALTER TABLE jobs ADD COLUMN {name} {definition}')
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

    def enqueue(self, item_id, name, mime, size, source, kind, chat=None, message=None,
                telegram_user=None, telegram_username=None):
        now = time.time()
        with self.db() as db:
            db.execute("""INSERT INTO jobs
                (id,name,mime,size,source,source_kind,telegram_chat,telegram_message,
                 telegram_user,telegram_username,created,updated)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item_id, name, mime, size, source, kind, chat, message,
                 telegram_user, telegram_username, now, now))

    def next_job(self):
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE status='pending' ORDER BY created LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE jobs SET status='uploading', updated=? WHERE id=?", (time.time(), row['id']))
                return dict(row)

    def update_job(self, item_id, **fields):
        allowed = {'status', 'drive_id', 'drive_url', 'permission_id', 'error', 'name',
                   'uploaded', 'speed', 'upload_started', 'upload_finished'}
        if not fields or not set(fields) <= allowed:
            raise ValueError('Invalid job fields')
        fields['updated'] = time.time()
        keys = list(fields)
        with self.db() as db:
            db.execute(
                'UPDATE jobs SET ' + ','.join(f'{key}=?' for key in keys) + ' WHERE id=?',
                [fields[k] for k in keys] + [item_id]
            )

    def remember_user(self, telegram_id, username=None, display_name=None):
        now = time.time()
        username = (username or '').lstrip('@').lower() or None
        with self.db() as db:
            db.execute('''INSERT INTO users
                (telegram_id,username,display_name,created,updated)
                VALUES (?,?,?,?,?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                  username=COALESCE(excluded.username,users.username),
                  display_name=COALESCE(excluded.display_name,users.display_name),
                  updated=excluded.updated''',
                (int(telegram_id), username, display_name, now, now))

    def seed_admin(self, telegram_id):
        self.remember_user(telegram_id)
        with self.db() as db:
            db.execute('UPDATE users SET enabled=1,is_admin=1,updated=? WHERE telegram_id=?',
                       (time.time(), int(telegram_id)))

    def user(self, value):
        with self.db() as db:
            if isinstance(value, str) and value.startswith('@'):
                row = db.execute('SELECT * FROM users WHERE username=?',
                                 (value[1:].lower(),)).fetchone()
            else:
                row = db.execute('SELECT * FROM users WHERE telegram_id=?',
                                 (int(value),)).fetchone()
            return dict(row) if row else None

    def users(self):
        with self.db() as db:
            return [dict(row) for row in db.execute(
                'SELECT * FROM users ORDER BY is_admin DESC,created')]

    def set_user_access(self, telegram_id, enabled):
        self.remember_user(telegram_id)
        with self.db() as db:
            db.execute('UPDATE users SET enabled=?,updated=? WHERE telegram_id=?',
                       (1 if enabled else 0, time.time(), int(telegram_id)))

    def set_user_folder(self, telegram_id, folder_id):
        with self.db() as db:
            db.execute('UPDATE users SET drive_folder_id=?,updated=? WHERE telegram_id=?',
                       (folder_id, time.time(), int(telegram_id)))

    def recent_user_jobs(self, telegram_id, limit=8):
        with self.db() as db:
            return [dict(row) for row in db.execute(
                'SELECT * FROM jobs WHERE telegram_user=? ORDER BY created DESC LIMIT ?',
                (int(telegram_id), int(limit)))]

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
