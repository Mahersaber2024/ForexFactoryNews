import sqlite3
import sys
import tempfile
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

try:
    import psycopg2
except ModuleNotFoundError:
    # Legacy-only tests remain runnable without the optional PostgreSQL driver.
    psycopg2 = types.ModuleType("psycopg2")
    psycopg2.Error = type("Error", (Exception,), {})
    psycopg2.connect = None
    psycopg2._test_stub = True
    sys.modules["psycopg2"] = psycopg2

from bot import backup_manager, database


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "bot.db"

    def tearDown(self):
        self.temp.cleanup()

    def test_fresh_database_and_repeat_initialization(self):
        database.initialize(self.path)
        database.initialize(self.path)
        with database.connect(self.path) as conn:
            self.assertEqual(database.schema_version(conn), 1)
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            for table in ("subscribers", "user_currencies", "users", "bot_config", "api_servers",
                          "user_channels", "user_channels_multi", "channel_style", "channel_style_multi",
                          "date_daily", "date_dest", "user_reminders", "reminder_tz",
                          "pre_news_sent", "pre_news_deliveries"):
                self.assertIsNotNone(conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())

    def test_upgrade_legacy_preserves_rows_and_channel_migration(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE subscribers (chat_id INTEGER PRIMARY KEY, digest_time TEXT, joined_at TEXT)")
            conn.execute("INSERT INTO subscribers VALUES (1, '08:15', '2020-01-01')")
            conn.execute("CREATE TABLE users (chat_id INTEGER PRIMARY KEY, joined_at TEXT)")
            conn.execute("INSERT INTO users VALUES (1, '2020-01-01')")
            conn.execute("CREATE TABLE user_currencies (chat_id INTEGER PRIMARY KEY, currencies TEXT)")
            conn.execute("INSERT INTO user_currencies VALUES (1, 'EUR,USD')")
            conn.execute("CREATE TABLE user_channels (chat_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, title TEXT, username TEXT)")
            conn.execute("INSERT INTO user_channels VALUES (1, -100, 'News', 'news')")
            conn.execute("CREATE TABLE date_daily (chat_id INTEGER PRIMARY KEY, is_on INTEGER, send_time TEXT, kinds TEXT, last_sent TEXT)")
            conn.execute("INSERT INTO date_daily VALUES (1, 1, '09:00', 'jalali', NULL)")
        database.initialize(self.path)
        with database.connect(self.path) as conn:
            self.assertEqual(conn.execute(
                "SELECT digest_time, language, digest_on, pre_news_on FROM subscribers WHERE chat_id=1"
            ).fetchone(), ("08:15", "en", 1, 0))
            self.assertEqual(conn.execute(
                "SELECT currencies, impacts FROM user_currencies WHERE chat_id=1"
            ).fetchone(), ("EUR,USD", "High"))
            self.assertEqual(conn.execute("SELECT * FROM user_channels_multi").fetchone(),
                             (1, -100, "News", "news"))
            self.assertEqual(conn.execute("SELECT is_on, to_channel FROM date_daily").fetchone(), (1, 0))
        database.initialize(self.path)
        with database.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM user_channels_multi").fetchone()[0], 1)

    def test_completed_channel_migration_does_not_resurrect_deleted_channel(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE user_channels (chat_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, title TEXT, username TEXT)")
            conn.execute("INSERT INTO user_channels VALUES (1, -100, 'Old', NULL)")
            conn.execute("CREATE TABLE user_channels_multi (chat_id INTEGER, channel_id INTEGER, title TEXT, username TEXT, PRIMARY KEY(chat_id, channel_id))")
            conn.execute("CREATE TABLE channel_migrations (name TEXT PRIMARY KEY)")
            conn.execute("INSERT INTO channel_migrations VALUES ('multi_v1')")
        database.initialize(self.path)
        with database.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM user_channels_multi").fetchone()[0], 0)

    def test_failed_upgrade_rolls_back(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE subscribers (chat_id INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO subscribers VALUES (1)")
        def broken(conn):
            conn.execute("CREATE TABLE should_rollback (id INTEGER)")
            raise RuntimeError("failure")
        with patch.dict(database.MIGRATIONS, {1: broken}):
            with self.assertRaises(RuntimeError):
                database.initialize(self.path)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT * FROM subscribers").fetchall(), [(1,)])
            self.assertIsNone(conn.execute(
                "SELECT name FROM sqlite_master WHERE name IN ('should_rollback', 'forex_schema_migrations')"
            ).fetchone())

    def test_future_version_rejected_on_initialize_and_backup_validation(self):
        database.initialize(self.path)
        with database.connect(self.path) as conn:
            conn.execute("INSERT INTO forex_schema_migrations(version) VALUES (99)")
        with self.assertRaises(RuntimeError):
            database.initialize(self.path)
        with self.assertRaises(backup_manager.BackupError):
            backup_manager._check_db(self.path)

    def test_snapshot_and_restore_legacy_database(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE subscribers (chat_id INTEGER PRIMARY KEY, digest_time TEXT, joined_at TEXT)")
            conn.execute("INSERT INTO subscribers VALUES (7, '07:00', '2020-01-01')")
        snapshot = self.path.with_name("snapshot.db")
        backup_manager._sqlite_copy(self.path, snapshot)
        backup_manager._check_db(snapshot)
        restored = self.path.with_name("restored.db")
        backup_manager._sqlite_copy(snapshot, restored)
        database.initialize(restored)
        with database.connect(restored) as conn:
            self.assertEqual(conn.execute("SELECT chat_id, digest_on FROM subscribers").fetchone(), (7, 1))

    def test_concurrent_initialization(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: database.initialize(self.path), range(4)))
        with database.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM forex_schema_migrations").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
