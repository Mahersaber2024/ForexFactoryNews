"""Import a ForexFactoryNews SQLite backup into PostgreSQL, without touching the source."""
from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

import bot_settings as cfg
from bot import database as legacy
from bot import postgres as pg


def snapshot_postgres(destination: Path) -> dict[str, int]:
    """Export all application tables from one consistent PostgreSQL snapshot."""
    legacy.initialize(destination)
    counts = {}
    with closing(pg.connect()) as src, closing(sqlite3.connect(destination)) as dst:
        src.raw.set_session(isolation_level="REPEATABLE READ", readonly=True)
        dst.execute("ALTER TABLE user_channels_multi ADD COLUMN sort_order INTEGER")
        for table, columns in pg.TABLES.items():
            dst.execute(f'DELETE FROM "{table}"')
            fields = ", ".join(f'"{col}"' for col in columns)
            ordering = " ORDER BY sort_order" if table == "user_channels_multi" else ""
            cursor = src.execute(f'SELECT {fields} FROM "{table}"{ordering}')
            placeholders = ", ".join("?" for _ in columns)
            count = 0
            while True:
                batch = cursor.fetchmany(1000)
                if not batch:
                    break
                dst.executemany(f'INSERT INTO "{table}" ({fields}) VALUES ({placeholders})', batch)
                count += len(batch)
            counts[table] = count
        dst.commit()
    return counts


def _copy_sqlite(source: Path, destination: Path):
    # URI mode=ro prevents a typo from creating an empty database.
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)


def _validate_source(source: Path):
    if not source.is_file():
        raise ValueError("SQLite source file does not exist")
    with source.open("rb") as f:
        if f.read(16) != b"SQLite format 3\x00":
            raise ValueError("Source is not a SQLite database")
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite integrity check failed")
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='subscribers'").fetchone():
            raise ValueError("Not a ForexFactoryNews database: subscribers table is missing")
        if legacy.schema_version(db) > legacy.SCHEMA_VERSION:
            raise ValueError("SQLite database was created by a newer unsupported version")


def import_sqlite(source: Path, *, replace: bool = False) -> dict[str, int]:
    """Import in one PostgreSQL transaction. replace=False refuses any nonempty target."""
    source = Path(source)
    _validate_source(source)
    with tempfile.TemporaryDirectory(prefix="forex-migration-") as td:
        normalized = Path(td) / "normalized.db"
        _copy_sqlite(source, normalized)
        # Upgrade only the private copy. This also handles old single-channel records.
        legacy.initialize(normalized)
        pg.initialize(allow_legacy=True)
        with closing(sqlite3.connect(normalized)) as old, closing(pg.connect()) as conn, conn:
            conn.execute("SELECT pg_advisory_xact_lock(694711018)")
            tables = ", ".join(f'public."{name}"' for name in pg.TABLES)
            conn.execute(f"LOCK TABLE {tables} IN ACCESS EXCLUSIVE MODE")
            if not replace:
                nonempty = [table for table in pg.TABLES
                            if conn.execute(f'SELECT EXISTS (SELECT 1 FROM "{table}")').fetchone()[0]]
                if nonempty:
                    raise ValueError("PostgreSQL already contains bot data; refusing to overwrite: " + ", ".join(nonempty))
            else:
                conn.execute(f"TRUNCATE TABLE {tables}")
            counts = {}
            for table, allowed in pg.TABLES.items():
                available = {r[1] for r in old.execute(f'PRAGMA table_info("{table}")')}
                columns = [c for c in allowed if c in available]
                if not columns:
                    raise ValueError(f"No compatible columns in {table}")
                fields = ", ".join(f'"{c}"' for c in columns)
                order = " ORDER BY rowid" if table == "user_channels_multi" and "sort_order" not in columns else ""
                rows = old.execute(f'SELECT {fields} FROM "{table}"{order}')
                count = 0
                while True:
                    batch = rows.fetchmany(1000)
                    if not batch:
                        break
                    placeholders = ", ".join("?" for _ in columns)
                    conn.executemany(f'INSERT INTO "{table}" ({fields}) VALUES ({placeholders})', batch)
                    count += len(batch)
                counts[table] = count
            # ALTER SEQUENCE is transactional, unlike setval(). Prevent collisions after import.
            next_id = int(conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM user_reminders").fetchone()[0])
            next_sort = int(conn.execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM user_channels_multi").fetchone()[0])
            conn.execute(f"ALTER SEQUENCE public.user_reminders_id_seq RESTART WITH {max(1, next_id)}")
            conn.execute(f"ALTER SEQUENCE public.user_channels_multi_sort_order_seq RESTART WITH {max(1, next_sort)}")
            conn.execute("""INSERT INTO forex_meta(key, value) VALUES ('legacy_import_complete', '1')
                            ON CONFLICT(key) DO UPDATE SET value=excluded.value""")
            return counts


def main():
    parser = argparse.ArgumentParser(description="Import an old SQLite database into an EMPTY PostgreSQL target.")
    parser.add_argument("source", nargs="?", type=Path, default=cfg.DB_PATH)
    args = parser.parse_args()
    try:
        counts = import_sqlite(args.source)
    except Exception as exc:
        print(f"Migration failed, no source data was modified: {exc}", file=sys.stderr)
        return 1
    print("Migration committed. Source SQLite file was left unchanged.")
    for table, count in counts.items():
        print(f"  {table}: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
