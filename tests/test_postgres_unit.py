"""Unit and simulated transfer tests; these do not substitute for a PostgreSQL server."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot_settings as cfg
from bot import backup_manager, database as legacy, migrate_sqlite, postgres


class FakePg:
    """SQLite-backed test double for the transfer code, not a PostgreSQL emulator."""
    def __init__(self, path, fail_table=None):
        self.conn = sqlite3.connect(path)
        self.raw = self
        self.fail_table = fail_table

    def set_session(self, **kwargs):
        pass

    def execute(self, sql, params=None):
        if "pg_advisory_xact_lock" in sql:
            return self.conn.execute("SELECT 1")
        if sql.startswith("LOCK TABLE"):
            self.conn.execute("BEGIN IMMEDIATE")
            return self.conn.execute("SELECT 1")
        if sql.startswith("TRUNCATE TABLE"):
            for name in postgres.TABLES:
                self.conn.execute(f'DELETE FROM "{name}"')
            return self.conn.execute("SELECT 1")
        if sql.startswith("ALTER SEQUENCE"):
            return self.conn.execute("SELECT 1")
        return self.conn.execute(sql, params or ())

    def executemany(self, sql, rows):
        if self.fail_table and f'"{self.fail_table}"' in sql:
            raise RuntimeError("injected import error")
        if '"user_channels_multi"' in sql and '"sort_order"' not in sql:
            cur = self.conn.execute("SELECT COALESCE(MAX(sort_order),0) FROM user_channels_multi")
            start = cur.fetchone()[0]
            for offset, row in enumerate(rows, 1):
                self.conn.execute(sql, row)
                self.conn.execute(
                    "UPDATE user_channels_multi SET sort_order=? WHERE chat_id=? AND channel_id=?",
                    (start + offset, row[0], row[1]))
            return
        return self.conn.executemany(sql, rows)

    def __enter__(self):
        return self

    def __exit__(self, typ, value, tb):
        if typ:
            self.conn.rollback()
        else:
            self.conn.commit()
        return False

    def close(self):
        self.conn.close()


class TransferUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "source.db"
        self.target = self.root / "target.db"
        legacy.initialize(self.source)
        legacy.initialize(self.target)
        with sqlite3.connect(self.target) as conn:
            conn.execute("ALTER TABLE user_channels_multi ADD COLUMN sort_order INTEGER")
            conn.execute("CREATE TABLE forex_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            for table in postgres.TABLES:
                conn.execute(f'DELETE FROM "{table}"')

    def tearDown(self):
        self.tmp.cleanup()

    def test_sql_adapter_and_bytea(self):
        self.assertEqual(postgres._sql("SELECT x FROM y WHERE a=? AND b=?"), "SELECT x FROM y WHERE a=%s AND b=%s")
        self.assertEqual(postgres._row((memoryview(b"logo"), 5, None)), (b"logo", 5, None))
        self.assertIsNone(postgres._row(None))

    def test_import_and_export_preserve_data_and_source(self):
        with sqlite3.connect(self.source) as db:
            db.execute("INSERT INTO subscribers(chat_id, language, digest_on) VALUES (?,?,?)", (9223372036854770000, "fa", 0))
            db.execute("INSERT INTO channel_style_multi VALUES (?, ?, ?, ?, ?)", (2, -1001234567890, "image", "timeline", b"logo\x00"))
            db.execute("INSERT INTO user_channels_multi VALUES (?, ?, ?, ?)", (2, -100, "one", None))
            db.execute("INSERT INTO user_channels_multi VALUES (?, ?, ?, ?)", (2, -200, "two", None))
            db.execute("INSERT INTO user_reminders (id,chat_id,text,kind,hhmm,tz,next_run) VALUES (?,?,?,?,?,?,?)",
                       (45, 2, "سلام", "daily", "08:30", "Asia/Tehran", 5000000000))
        before = self.source.read_bytes()
        with patch.object(postgres, "initialize"), patch.object(postgres, "connect", side_effect=lambda: FakePg(self.target)):
            counts = migrate_sqlite.import_sqlite(self.source)
            self.assertEqual(counts["user_reminders"], 1)
            self.assertEqual(counts["user_channels_multi"], 2)
            snap = self.root / "export.db"
            counts = migrate_sqlite.snapshot_postgres(snap)
        self.assertEqual(before, self.source.read_bytes())
        with sqlite3.connect(snap) as db:
            self.assertEqual(db.execute("SELECT chat_id, language, digest_on FROM subscribers").fetchone(), (9223372036854770000, "fa", 0))
            self.assertEqual(db.execute("SELECT logo FROM channel_style_multi").fetchone()[0], b"logo\x00")
            self.assertEqual(db.execute("SELECT channel_id FROM user_channels_multi ORDER BY sort_order").fetchall(), [(-100,), (-200,)])
            self.assertEqual(db.execute("SELECT id, text, next_run FROM user_reminders").fetchone(), (45, "سلام", 5000000000))

    def test_refuse_nonempty_target(self):
        with sqlite3.connect(self.target) as db:
            db.execute("INSERT INTO users(chat_id) VALUES (77)")
        with patch.object(postgres, "initialize"), patch.object(postgres, "connect", side_effect=lambda: FakePg(self.target)):
            with self.assertRaisesRegex(ValueError, "already contains"):
                migrate_sqlite.import_sqlite(self.source)
        with sqlite3.connect(self.target) as db:
            self.assertEqual(db.execute("SELECT chat_id FROM users").fetchall(), [(77,)])

    def test_restore_rolls_back_on_transfer_failure(self):
        with sqlite3.connect(self.target) as db:
            db.execute("INSERT INTO users(chat_id) VALUES (77)")
        with sqlite3.connect(self.source) as db:
            db.execute("INSERT INTO users(chat_id) VALUES (99)")
        with patch.object(postgres, "initialize"), patch.object(postgres, "connect", side_effect=lambda: FakePg(self.target, "users")):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                migrate_sqlite.import_sqlite(self.source, replace=True)
        with sqlite3.connect(self.target) as db:
            self.assertEqual(db.execute("SELECT chat_id FROM users").fetchall(), [(77,)])

    def test_corrupt_source_rejected_before_target(self):
        self.source.write_bytes(b"not a database")
        with patch.object(postgres, "connect") as connect:
            with self.assertRaises(ValueError):
                migrate_sqlite.import_sqlite(self.source)
            connect.assert_not_called()

    def test_backup_and_restore_with_simulated_target(self):
        with sqlite3.connect(self.target) as db:
            db.execute("INSERT INTO users(chat_id) VALUES (7)")
        settings = self.root / "settings.env"
        settings.write_text("TELEGRAM_BOT_TOKEN=secret\nDB_PASSWORD=example\n")
        backup_dir = self.root / "backups"
        with patch.object(postgres, "initialize"), \
             patch.object(postgres, "connect", side_effect=lambda: FakePg(self.target)), \
             patch.object(cfg, "DATA_DIR", self.root), patch.object(cfg, "BACKUP_DIR", backup_dir), \
             patch.object(cfg, "ENV_FILE", settings), patch.object(cfg, "DOWNLOADS_DIR", self.root / "downloads"):
            archive = backup_manager.create_backup("manual")
            self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
            self.assertEqual(backup_dir.stat().st_mode & 0o777, 0o700)
            plan = backup_manager.prepare_restore(archive)
            self.assertEqual(plan.counts["users"], 1)
            with sqlite3.connect(self.target) as db:
                db.execute("DELETE FROM users")
                db.execute("INSERT INTO users(chat_id) VALUES (8)")
            safety, _ = backup_manager.apply_restore(plan)
            self.assertTrue(safety.exists())
            with sqlite3.connect(self.target) as db:
                self.assertEqual(db.execute("SELECT chat_id FROM users").fetchall(), [(7,)])
            safety_plan = backup_manager.prepare_restore(safety)
            with sqlite3.connect(safety_plan.db_file) as db:
                self.assertEqual(db.execute("SELECT chat_id FROM users").fetchall(), [(8,)])
            safety_plan.cleanup()

    def test_restore_settings_keep_current_database_target(self):
        plan = backup_manager.RestorePlan(
            source=self.source, workdir=self.root / "plan", db_file=self.source,
            env_values={"DB_HOST": "other-host", "DB_PASSWORD": "old-secret",
                        "TELEGRAM_BOT_TOKEN": "restored-token"})
        plan.workdir.mkdir()
        current = {"DB_HOST": "current-host", "DB_PASSWORD": "current-secret", "ADMIN_CHAT_ID": "42"}
        with patch.object(postgres, "initialize"), \
             patch.object(postgres, "connect", side_effect=lambda: FakePg(self.target)), \
             patch.object(backup_manager, "create_backup", return_value=self.root / "safety.zip"), \
             patch.object(cfg, "read_env", return_value=current), \
             patch.object(cfg, "write_env") as write, patch.object(cfg, "reload"), \
             patch.object(cfg, "DOWNLOADS_DIR", self.root / "downloads"):
            backup_manager.apply_restore(plan, include_settings=True)
            written = write.call_args.args[0]
            self.assertEqual(written["DB_HOST"], "current-host")
            self.assertEqual(written["DB_PASSWORD"], "current-secret")
            self.assertEqual(written["ADMIN_CHAT_ID"], "42")
            self.assertEqual(written["TELEGRAM_BOT_TOKEN"], "restored-token")


if __name__ == "__main__":
    unittest.main()
