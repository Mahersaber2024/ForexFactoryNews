#!/usr/bin/env python3
"""Check the PostgreSQL connection, create the schema and import an old SQLite file.

Run by scripts/install.sh after the role and database have been provisioned.
Safe to run again at any time.

    python -m db.setup_db            # test connection, create/verify tables, import legacy data
    python -m db.setup_db --show     # only list tables and row counts
    python -m db.setup_db --no-legacy  # skip the old SQLite import
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from contextlib import closing
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_settings as cfg
from db import postgres


def test_connection() -> bool:
    try:
        with closing(postgres.connect()) as conn:
            version = conn.execute("SELECT version()").fetchone()[0]
        print(f"✓ Connected to PostgreSQL: {version[:60]}")
        return True
    except Exception as exc:
        print(f"✗ Connection failed: {exc}", file=sys.stderr)
        print("\nCheck:", file=sys.stderr)
        print("  1. PostgreSQL is installed and running: systemctl status postgresql", file=sys.stderr)
        print("  2. DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASSWORD are right: python bot_settings.py show", file=sys.stderr)
        print("  3. The role and database exist (install.sh creates them)", file=sys.stderr)
        return False


def create_tables() -> bool:
    try:
        postgres.initialize(allow_legacy=True)
        print("✓ Tables created/verified")
        return True
    except Exception as exc:
        print(f"✗ Schema setup failed: {exc}", file=sys.stderr)
        return False


def legacy_pending() -> bool:
    if not cfg.DB_PATH.exists():
        return False
    with closing(postgres.connect()) as conn:
        row = conn.execute("SELECT value FROM forex_meta WHERE key = 'legacy_import_complete'").fetchone()
    return row is None


def import_legacy() -> bool:
    if not legacy_pending():
        return True
    from db import migrate_sqlite
    cfg.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    copy = cfg.BACKUP_DIR / f"forexbot-before-postgres-{time.strftime('%Y%m%d-%H%M%S')}.db"
    shutil.copy2(cfg.DB_PATH, copy)
    print(f"✓ Copy of the old database saved: {copy}")
    try:
        counts = migrate_sqlite.import_sqlite(cfg.DB_PATH)
    except Exception as exc:
        print(f"✗ Legacy import failed (nothing was changed in the old file): {exc}", file=sys.stderr)
        return False
    print("✓ Old SQLite data imported:")
    for table, count in counts.items():
        print(f"    {table}: {count}")
    return True


def show_tables() -> None:
    with closing(postgres.connect()) as conn:
        names = [r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name").fetchall()]
        if not names:
            print("No tables found.")
            return
        print("\nTables:")
        for name in names:
            count = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            print(f"  • {name} ({count} rows)")


def main(argv: list[str] | None = None) -> int:
    args = set(sys.argv[1:] if argv is None else argv)
    print(f"Database: {cfg.DB_NAME} @ {cfg.DB_HOST}:{cfg.DB_PORT}  user: {cfg.DB_USER}")
    if not test_connection():
        return 1
    if "--show" in args:
        show_tables()
        return 0
    if not create_tables():
        return 1
    if "--no-legacy" not in args and not import_legacy():
        return 1
    try:
        postgres.initialize()
    except Exception as exc:
        print(f"✗ Final check failed: {exc}", file=sys.stderr)
        return 1
    show_tables()
    print("\n✓ Database setup completed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
