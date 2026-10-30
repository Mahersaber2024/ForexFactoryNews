"""Legacy SQLite backup interchange only. Live bot data is stored in PostgreSQL."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import bot_settings as cfg

SCHEMA_VERSION = 1


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Return a short-lived connection with a consistent lock timeout."""
    conn = sqlite3.connect(str(path if path is not None else cfg.DB_PATH), timeout=10)
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='forex_schema_migrations'"
    ).fetchone()
    if not exists:
        return 0
    return int(conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM forex_schema_migrations"
    ).fetchone()[0])


def _add_column(conn: sqlite3.Connection, table: str, name: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
    if name not in columns:
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {definition}')


def _migration_1(conn: sqlite3.Connection) -> None:
    statements = (
        """CREATE TABLE IF NOT EXISTS subscribers (
            chat_id INTEGER PRIMARY KEY, digest_time TEXT DEFAULT '07:00',
            joined_at TEXT DEFAULT CURRENT_TIMESTAMP, language TEXT DEFAULT 'en',
            digest_on INTEGER NOT NULL DEFAULT 1, pre_news_on INTEGER NOT NULL DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS user_currencies (
            chat_id INTEGER PRIMARY KEY, currencies TEXT DEFAULT 'USD', impacts TEXT DEFAULT 'High')""",
        """CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY, joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
            menu_kb INTEGER NOT NULL DEFAULT 0, news_menu_message_id INTEGER)""",
        "CREATE TABLE IF NOT EXISTS bot_config (key TEXT PRIMARY KEY, value TEXT)",
        "CREATE TABLE IF NOT EXISTS api_servers (name TEXT PRIMARY KEY, url TEXT NOT NULL)",
        """CREATE TABLE IF NOT EXISTS user_channels (
            chat_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, title TEXT, username TEXT)""",
        """CREATE TABLE IF NOT EXISTS user_channels_multi (
            chat_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, title TEXT, username TEXT,
            PRIMARY KEY(chat_id, channel_id))""",
        "CREATE TABLE IF NOT EXISTS channel_migrations (name TEXT PRIMARY KEY)",
        """CREATE TABLE IF NOT EXISTS channel_style (
            chat_id INTEGER PRIMARY KEY, mode TEXT NOT NULL DEFAULT 'text',
            theme TEXT NOT NULL DEFAULT 'aurora', logo BLOB)""",
        """CREATE TABLE IF NOT EXISTS channel_style_multi (
            chat_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
            mode TEXT NOT NULL DEFAULT 'text', theme TEXT NOT NULL DEFAULT 'timeline', logo BLOB,
            PRIMARY KEY(chat_id, channel_id))""",
        """CREATE TABLE IF NOT EXISTS date_daily (
            chat_id INTEGER PRIMARY KEY, is_on INTEGER NOT NULL DEFAULT 0,
            send_time TEXT NOT NULL DEFAULT '07:00',
            kinds TEXT NOT NULL DEFAULT 'jalali,gregorian', last_sent TEXT,
            to_channel INTEGER NOT NULL DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS date_dest (
            chat_id INTEGER NOT NULL, dest_id INTEGER NOT NULL, PRIMARY KEY(chat_id, dest_id))""",
        """CREATE TABLE IF NOT EXISTS user_reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, text TEXT NOT NULL,
            kind TEXT NOT NULL, hhmm TEXT NOT NULL, on_date TEXT, weekday INTEGER,
            tz TEXT NOT NULL, next_run INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
        "CREATE TABLE IF NOT EXISTS reminder_tz (chat_id INTEGER PRIMARY KEY, tz TEXT NOT NULL)",
        """CREATE TABLE IF NOT EXISTS pre_news_sent (
            chat_id INTEGER NOT NULL, event_key TEXT NOT NULL, sent_date TEXT NOT NULL,
            PRIMARY KEY(chat_id, event_key))""",
        """CREATE TABLE IF NOT EXISTS pre_news_deliveries (
            chat_id INTEGER NOT NULL, event_key TEXT NOT NULL, target_id INTEGER NOT NULL,
            sent_date TEXT NOT NULL, PRIMARY KEY(chat_id, event_key, target_id))""",
        "CREATE INDEX IF NOT EXISTS idx_user_reminders_next ON user_reminders(next_run)",
        "CREATE INDEX IF NOT EXISTS idx_user_reminders_chat ON user_reminders(chat_id)",
        "CREATE INDEX IF NOT EXISTS idx_pre_news_sent_date ON pre_news_sent(sent_date)",
        "CREATE INDEX IF NOT EXISTS idx_pre_news_deliveries_date ON pre_news_deliveries(sent_date)",
        "CREATE INDEX IF NOT EXISTS idx_pre_news_deliveries_target ON pre_news_deliveries(chat_id, target_id)",
    )
    for statement in statements:
        conn.execute(statement)
    for table, name, definition in (
        ("subscribers", "language", "TEXT DEFAULT 'en'"),
        ("subscribers", "digest_on", "INTEGER NOT NULL DEFAULT 1"),
        ("subscribers", "pre_news_on", "INTEGER NOT NULL DEFAULT 0"),
        ("users", "menu_kb", "INTEGER NOT NULL DEFAULT 0"),
        ("users", "news_menu_message_id", "INTEGER"),
        ("user_currencies", "impacts", "TEXT DEFAULT 'High'"),
        ("date_daily", "to_channel", "INTEGER NOT NULL DEFAULT 0"),
    ):
        _add_column(conn, table, name, definition)
    if not conn.execute("SELECT 1 FROM channel_migrations WHERE name = 'multi_v1'").fetchone():
        conn.execute("""INSERT OR IGNORE INTO user_channels_multi (chat_id, channel_id, title, username)
                        SELECT chat_id, channel_id, title, username FROM user_channels""")
        conn.execute("INSERT INTO channel_migrations(name) VALUES ('multi_v1')")


MIGRATIONS = {1: _migration_1}


def initialize(path: str | Path | None = None) -> None:
    """Upgrade a new or legacy DB atomically; refuse DBs from newer software."""
    with closing(connect(path)) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            version = schema_version(conn)
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema version {version} is newer than supported version {SCHEMA_VERSION}"
                )
            conn.execute("""CREATE TABLE IF NOT EXISTS forex_schema_migrations (
                version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            for number in range(version + 1, SCHEMA_VERSION + 1):
                MIGRATIONS[number](conn)
                conn.execute("INSERT INTO forex_schema_migrations(version) VALUES (?)", (number,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
