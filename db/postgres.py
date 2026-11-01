"""PostgreSQL access and transactional schema setup for ForexFactoryNews."""
from __future__ import annotations

import os
import threading
import time
from contextlib import closing
import psycopg2
from psycopg2 import pool as _pgpool
import bot_settings as cfg

SCHEMA_VERSION = 1
Error = psycopg2.Error

# Only tables belonging to this application are ever exported or replaced.
TABLES = {
    "subscribers": ("chat_id", "digest_time", "joined_at", "language", "digest_on", "pre_news_on"),
    "user_currencies": ("chat_id", "currencies", "impacts"),
    "users": ("chat_id", "joined_at", "menu_kb", "news_menu_message_id"),
    "bot_config": ("key", "value"),
    "api_servers": ("name", "url"),
    "user_channels": ("chat_id", "channel_id", "title", "username"),
    "user_channels_multi": ("chat_id", "channel_id", "title", "username", "sort_order"),
    "channel_migrations": ("name",),
    "channel_style": ("chat_id", "mode", "theme", "logo"),
    "channel_style_multi": ("chat_id", "channel_id", "mode", "theme", "logo"),
    "date_daily": ("chat_id", "is_on", "send_time", "kinds", "last_sent", "to_channel"),
    "date_dest": ("chat_id", "dest_id"),
    "user_reminders": ("id", "chat_id", "text", "kind", "hhmm", "on_date", "weekday", "tz", "next_run", "created_at"),
    "reminder_tz": ("chat_id", "tz"),
    "pre_news_sent": ("chat_id", "event_key", "sent_date"),
    "pre_news_deliveries": ("chat_id", "event_key", "target_id", "sent_date"),
}


def _sql(statement: str) -> str:
    # Application queries use qmark placeholders, never literal '?' in SQL.
    return statement.replace("?", "%s")


def _row(row):
    if row is None:
        return None
    return tuple(bytes(value) if isinstance(value, memoryview) else value for value in row)


class Cursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql, params=None):
        self._cursor.execute(_sql(sql), params)
        return self

    def executemany(self, sql, params):
        self._cursor.executemany(_sql(sql), params)
        return self

    def fetchone(self):
        return _row(self._cursor.fetchone())

    def fetchall(self):
        return [_row(row) for row in self._cursor.fetchall()]

    def fetchmany(self, size=1000):
        return [_row(row) for row in self._cursor.fetchmany(size)]

    def __iter__(self):
        return (_row(row) for row in self._cursor)

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def close(self):
        self._cursor.close()


class Connection:
    def __init__(self, raw, pool=None):
        self.raw = raw
        self._pool = pool            # set for pooled connections: close() gives it back instead of closing it

    def cursor(self):
        return Cursor(self.raw.cursor())

    def execute(self, sql, params=None):
        return self.cursor().execute(sql, params)

    def executemany(self, sql, params):
        return self.cursor().executemany(sql, params)

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        if self._pool is not None:
            pool, self._pool = self._pool, None
            pool.give_back(self.raw)
        elif self.raw is not None:
            self.raw.close()

    def __del__(self):               # a caller forgot close(): never leak a pool slot
        try:
            if self._pool is not None:
                self.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, typ, value, tb):
        if typ is None:
            self.commit()
        else:
            self.rollback()
        return False


# ───────────────  connection pool  ────────────────
# Opening a PostgreSQL connection costs a TCP + login round trip, and the server only allows ~100 of them. The bot
# used to open a new one for every tiny query. Now a small pool is shared by all threads (DB_POOL_MAX, default 40).
POOL_MIN = 2
POOL_MAX = max(POOL_MIN, int(os.environ.get("DB_POOL_MAX", "40")))
POOL_WAIT = 30.0                     # seconds to wait for a free connection before giving up
IDLE_CHECK = 30.0                    # a connection idle longer than this is tested before it is handed out


def _kwargs() -> dict:
    return dict(
        host=cfg.DB_HOST, port=cfg.DB_PORT, dbname=cfg.DB_NAME, user=cfg.DB_USER,
        password=cfg.DB_PASSWORD, sslmode=cfg.DB_SSLMODE, connect_timeout=10,
        application_name="forexfactory-news", options="-c search_path=public",
        keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
    )


class _Pool:
    def __init__(self, kwargs: dict):
        self.kwargs = kwargs
        self._pool = _pgpool.ThreadedConnectionPool(POOL_MIN, POOL_MAX, **kwargs)
        self._slots = threading.BoundedSemaphore(POOL_MAX)     # getconn() raises when full; this makes callers wait
        self._idle_since: dict[int, float] = {}

    def take(self) -> Connection:
        if not self._slots.acquire(timeout=POOL_WAIT):
            raise psycopg2.OperationalError("PostgreSQL connection pool is exhausted (all connections are busy)")
        try:
            while True:
                raw = self._pool.getconn()
                if raw.closed:
                    self._pool.putconn(raw, close=True)
                    continue
                idle = time.monotonic() - self._idle_since.get(id(raw), time.monotonic())
                if idle > IDLE_CHECK:                       # the server may have dropped it while it sat in the pool
                    try:
                        with raw.cursor() as cur:
                            cur.execute("SELECT 1")
                        raw.rollback()
                    except Exception:
                        self._pool.putconn(raw, close=True)
                        continue
                return Connection(raw, self)
        except BaseException:
            self._slots.release()
            raise

    def give_back(self, raw) -> None:
        try:
            if raw.closed:
                self._pool.putconn(raw, close=True)
            else:
                try:
                    raw.rollback()                          # never hand out a half-finished transaction
                except Exception:
                    self._pool.putconn(raw, close=True)
                    return
                self._idle_since[id(raw)] = time.monotonic()
                self._pool.putconn(raw)
        finally:
            self._slots.release()


_pool_lock = threading.Lock()
_pool: _Pool | None = None


def _get_pool() -> _Pool:
    global _pool
    kwargs = _kwargs()
    if _pool is None or _pool.kwargs != kwargs:      # first use, or the settings changed
        with _pool_lock:
            if _pool is None or _pool.kwargs != kwargs:
                old, _pool = _pool, _Pool(kwargs)
                if old is not None:
                    try:
                        old._pool.closeall()
                    except Exception:
                        pass
    return _pool


def connect(pooled: bool = True) -> Connection:
    """A database connection. Pooled by default: close() hands it back to the pool (always close it, `with closing(...)`).
    pooled=False gives a private connection for work that changes session settings (backup/restore)."""
    if pooled:
        return _get_pool().take()
    return Connection(psycopg2.connect(**_kwargs()))


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS subscribers (
        chat_id BIGINT PRIMARY KEY, digest_time TEXT DEFAULT '07:00',
        joined_at TEXT DEFAULT to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS'),
        language TEXT DEFAULT 'en', digest_on INTEGER NOT NULL DEFAULT 1,
        pre_news_on INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS user_currencies (
        chat_id BIGINT PRIMARY KEY, currencies TEXT DEFAULT 'USD', impacts TEXT DEFAULT 'High')""",
    """CREATE TABLE IF NOT EXISTS users (
        chat_id BIGINT PRIMARY KEY, joined_at TEXT DEFAULT to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS'),
        menu_kb INTEGER NOT NULL DEFAULT 0, news_menu_message_id BIGINT)""",
    "CREATE TABLE IF NOT EXISTS bot_config (key TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE IF NOT EXISTS api_servers (name TEXT PRIMARY KEY, url TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS user_channels (
        chat_id BIGINT PRIMARY KEY, channel_id BIGINT NOT NULL, title TEXT, username TEXT)""",
    """CREATE TABLE IF NOT EXISTS user_channels_multi (
        chat_id BIGINT NOT NULL, channel_id BIGINT NOT NULL, title TEXT, username TEXT,
        sort_order BIGSERIAL NOT NULL, PRIMARY KEY(chat_id, channel_id))""",
    "CREATE TABLE IF NOT EXISTS channel_migrations (name TEXT PRIMARY KEY)",
    """CREATE TABLE IF NOT EXISTS channel_style (
        chat_id BIGINT PRIMARY KEY, mode TEXT NOT NULL DEFAULT 'text',
        theme TEXT NOT NULL DEFAULT 'aurora', logo BYTEA)""",
    """CREATE TABLE IF NOT EXISTS channel_style_multi (
        chat_id BIGINT NOT NULL, channel_id BIGINT NOT NULL,
        mode TEXT NOT NULL DEFAULT 'text', theme TEXT NOT NULL DEFAULT 'timeline', logo BYTEA,
        PRIMARY KEY(chat_id, channel_id))""",
    """CREATE TABLE IF NOT EXISTS date_daily (
        chat_id BIGINT PRIMARY KEY, is_on INTEGER NOT NULL DEFAULT 0,
        send_time TEXT NOT NULL DEFAULT '07:00',
        kinds TEXT NOT NULL DEFAULT 'jalali,gregorian', last_sent TEXT,
        to_channel INTEGER NOT NULL DEFAULT 0)""",
    "CREATE TABLE IF NOT EXISTS date_dest (chat_id BIGINT NOT NULL, dest_id BIGINT NOT NULL, PRIMARY KEY(chat_id, dest_id))",
    """CREATE TABLE IF NOT EXISTS user_reminders (
        id BIGSERIAL PRIMARY KEY, chat_id BIGINT NOT NULL, text TEXT NOT NULL,
        kind TEXT NOT NULL, hhmm TEXT NOT NULL, on_date TEXT, weekday INTEGER,
        tz TEXT NOT NULL, next_run BIGINT,
        created_at TEXT DEFAULT to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS'))""",
    "CREATE TABLE IF NOT EXISTS reminder_tz (chat_id BIGINT PRIMARY KEY, tz TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS pre_news_sent (chat_id BIGINT NOT NULL, event_key TEXT NOT NULL, sent_date TEXT NOT NULL, PRIMARY KEY(chat_id, event_key))",
    """CREATE TABLE IF NOT EXISTS pre_news_deliveries (
        chat_id BIGINT NOT NULL, event_key TEXT NOT NULL, target_id BIGINT NOT NULL,
        sent_date TEXT NOT NULL, PRIMARY KEY(chat_id, event_key, target_id))""",
    "CREATE INDEX IF NOT EXISTS idx_user_reminders_next ON user_reminders(next_run)",
    "CREATE INDEX IF NOT EXISTS idx_user_reminders_chat ON user_reminders(chat_id)",
    "CREATE INDEX IF NOT EXISTS idx_pre_news_sent_date ON pre_news_sent(sent_date)",
    "CREATE INDEX IF NOT EXISTS idx_pre_news_deliveries_date ON pre_news_deliveries(sent_date)",
    "CREATE INDEX IF NOT EXISTS idx_pre_news_deliveries_target ON pre_news_deliveries(chat_id, target_id)",
)


def initialize():
    """Create the schema under an advisory lock."""
    with closing(connect()) as conn, conn:
        conn.execute("SELECT pg_advisory_xact_lock(694711018)")
        existing = {row[0] for row in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE'").fetchall()}
        if "forex_schema_migrations" not in existing and existing:
            raise RuntimeError(
                "Refusing to initialize a nonempty PostgreSQL database. "
                "Create a dedicated empty database for ForexFactoryNews."
            )
        conn.execute("""CREATE TABLE IF NOT EXISTS forex_schema_migrations (
            version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        version = conn.execute("SELECT COALESCE(MAX(version), 0) FROM forex_schema_migrations").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"PostgreSQL schema {version} is newer than this bot supports ({SCHEMA_VERSION})")
        if version < 1:
            for statement in SCHEMA:
                conn.execute(statement)
            conn.execute("INSERT INTO forex_schema_migrations(version) VALUES (1)")
        present = {}
        for table, column in conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema='public'"
        ):
            present.setdefault(table, set()).add(column)
        missing = [f"{table}.{col}" for table, cols in TABLES.items() for col in cols
                   if col not in present.get(table, set())]
        if missing:
            raise RuntimeError("PostgreSQL schema is incomplete: " + ", ".join(missing))
