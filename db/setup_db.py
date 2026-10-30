#!/usr/bin/env python3
"""Check the PostgreSQL connection and create/verify the schema.

Run by scripts/install.sh after the role and database have been provisioned.
Safe to run again at any time.

    python -m db.setup_db            # test connection, create/verify tables
    python -m db.setup_db --show     # only list tables and row counts
"""
from __future__ import annotations

import os
import sys
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
        postgres.initialize()
        print("✓ Tables created/verified")
        return True
    except Exception as exc:
        print(f"✗ Schema setup failed: {exc}", file=sys.stderr)
        return False


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
