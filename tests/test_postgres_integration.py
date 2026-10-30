"""Real PostgreSQL tests. Use ONLY an explicitly designated disposable test database."""
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import psycopg2
import bot_settings as cfg
from db import database as legacy, migrate_sqlite, postgres


ENABLED = (os.environ.get("FOREX_RUN_PG_TESTS") == "1"
           and cfg.DB_NAME.startswith("forex_test_")
           and not getattr(psycopg2, "_test_stub", False))


@unittest.skipUnless(ENABLED, "requires psycopg2 and an explicitly enabled forex_test_* PostgreSQL database")
class PostgreSQLIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.source = Path(self.tmp.name) / "legacy.db"
        legacy.initialize(self.source)
        postgres.initialize(allow_legacy=True)
        with closing(postgres.connect()) as conn, conn:
            tables = ", ".join(f'public."{t}"' for t in postgres.TABLES)
            conn.execute(f"TRUNCATE TABLE {tables} RESTART IDENTITY")
            conn.execute("DELETE FROM forex_meta")

    def tearDown(self):
        self.tmp.cleanup()

    def test_import_roundtrip_sequences_and_binary(self):
        with sqlite3.connect(self.source) as src:
            src.execute("INSERT INTO subscribers(chat_id,language,digest_on) VALUES (?,?,?)",
                        (9223372036854770000, "fa", 0))
            src.execute("INSERT INTO user_channels_multi VALUES (?,?,?,?)", (1, -100, "first", None))
            src.execute("INSERT INTO user_channels_multi VALUES (?,?,?,?)", (1, -200, "second", None))
            src.execute("INSERT INTO channel_style_multi VALUES (?,?,?,?,?)", (1, -100, "image", "timeline", b"\x00logo"))
            src.execute("INSERT INTO user_reminders (id,chat_id,text,kind,hhmm,tz,next_run) VALUES (?,?,?,?,?,?,?)",
                        (45, 1, "تست", "daily", "08:30", "Asia/Tehran", 5000000000))
        migrate_sqlite.import_sqlite(self.source)
        with closing(postgres.connect()) as conn, conn:
            self.assertEqual(conn.execute("SELECT language FROM subscribers WHERE chat_id=?", (9223372036854770000,)).fetchone(), ("fa",))
            self.assertEqual(conn.execute("SELECT logo FROM channel_style_multi").fetchone(), (b"\x00logo",))
            self.assertEqual(conn.execute("SELECT channel_id FROM user_channels_multi ORDER BY sort_order").fetchall(), [(-100,), (-200,)])
            self.assertEqual(conn.execute(
                "INSERT INTO user_reminders(chat_id,text,kind,hhmm,tz) VALUES (?,?,?,?,?) RETURNING id",
                (1, "next", "once", "08:00", "UTC")).fetchone()[0], 46)
            conn.execute("INSERT INTO user_channels_multi(chat_id,channel_id) VALUES (?,?)", (1, -300))
            self.assertEqual(conn.execute("SELECT channel_id FROM user_channels_multi ORDER BY sort_order").fetchall(),
                             [(-100,), (-200,), (-300,)])
        snap = Path(self.tmp.name) / "snapshot.db"
        migrate_sqlite.snapshot_postgres(snap)
        migrate_sqlite.import_sqlite(snap, replace=True)
        with closing(postgres.connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM user_reminders").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT logo FROM channel_style_multi").fetchone(), (b"\x00logo",))

    def test_nonempty_target_never_overwritten_by_first_import(self):
        with closing(postgres.connect()) as conn, conn:
            conn.execute("INSERT INTO users(chat_id) VALUES (?)", (99,))
        with self.assertRaises(ValueError):
            migrate_sqlite.import_sqlite(self.source)
        with closing(postgres.connect()) as conn:
            self.assertEqual(conn.execute("SELECT chat_id FROM users").fetchall(), [(99,)])

    def test_failed_restore_rolls_back_all_tables(self):
        with sqlite3.connect(self.source) as src:
            # SQLite's dynamic typing accepts a text id; PostgreSQL BIGINT must reject it.
            src.execute("INSERT INTO user_reminders(chat_id,text,kind,hhmm,tz,next_run) VALUES (?,?,?,?,?,?)",
                        (5, "test", "once", "08:00", "UTC", "not-an-integer"))
        with closing(postgres.connect()) as conn, conn:
            conn.execute("INSERT INTO users(chat_id) VALUES (?)", (88,))
        with self.assertRaises(psycopg2.Error):
            migrate_sqlite.import_sqlite(self.source, replace=True)
        with closing(postgres.connect()) as conn:
            self.assertEqual(conn.execute("SELECT chat_id FROM users").fetchall(), [(88,)])

    def test_conflict_queries_and_defaults(self):
        with closing(postgres.connect()) as conn, conn:
            conn.execute("INSERT INTO subscribers(chat_id,language,digest_on) VALUES (?,?,?)", (1, "fa", 0))
            conn.execute("""INSERT INTO subscribers(chat_id,language,digest_on) VALUES (?,?,?)
                ON CONFLICT(chat_id) DO UPDATE SET digest_on=excluded.digest_on,
                pre_news_on=CASE WHEN excluded.digest_on=1 THEN 0 ELSE subscribers.pre_news_on END""", (1, "fa", 1))
            self.assertEqual(conn.execute("SELECT digest_on,pre_news_on FROM subscribers").fetchone(), (1, 0))
            conn.execute("INSERT INTO date_daily(chat_id) VALUES (?) ON CONFLICT DO NOTHING", (1,))
            self.assertEqual(conn.execute("SELECT send_time FROM date_daily").fetchone(), ("07:00",))


if __name__ == "__main__":
    unittest.main()
