"""Portable JSON snapshot of the PostgreSQL application data (used by bot backups)."""
from __future__ import annotations

import base64
import json
from contextlib import closing
from pathlib import Path

from db import postgres as pg

SNAPSHOT_VERSION = 1
BYTES_KEY = "$b64"


class SnapshotError(ValueError):
    """The snapshot file is unreadable or is not a ForexFactoryNews snapshot."""


def _encode(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {BYTES_KEY: base64.b64encode(bytes(value)).decode("ascii")}
    return value


def _decode(value):
    if isinstance(value, dict) and set(value) == {BYTES_KEY}:
        return base64.b64decode(value[BYTES_KEY])
    return value


def export_snapshot(destination: Path) -> dict[str, int]:
    """Write all application tables from one consistent PostgreSQL snapshot."""
    tables: dict[str, dict] = {}
    counts: dict[str, int] = {}
    with closing(pg.connect(pooled=False)) as src:
        src.raw.set_session(isolation_level="REPEATABLE READ", readonly=True)
        for table, columns in pg.TABLES.items():
            fields = ", ".join(f'"{col}"' for col in columns)
            ordering = " ORDER BY sort_order" if table == "user_channels_multi" else ""
            rows = src.execute(f'SELECT {fields} FROM "{table}"{ordering}').fetchall()
            tables[table] = {"columns": list(columns), "rows": [[_encode(v) for v in row] for row in rows]}
            counts[table] = len(rows)
    document = {"app": "forexfactory-bot", "version": SNAPSHOT_VERSION, "tables": tables}
    Path(destination).write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return counts


def _load(source: Path) -> dict:
    try:
        document = json.loads(Path(source).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SnapshotError(f"Snapshot is unreadable: {exc}") from None
    if not isinstance(document, dict) or not isinstance(document.get("tables"), dict):
        raise SnapshotError("This is not a ForexFactoryNews snapshot.")
    if int(document.get("version", 0)) > SNAPSHOT_VERSION:
        raise SnapshotError("Snapshot was made by a newer version of the bot, update first.")
    if "subscribers" not in document["tables"]:
        raise SnapshotError("Not a ForexFactoryNews snapshot: subscribers table is missing.")
    for name, table in document["tables"].items():
        if name in pg.TABLES and not (isinstance(table, dict) and isinstance(table.get("columns"), list)
                                      and isinstance(table.get("rows"), list)):
            raise SnapshotError(f"Table {name} is malformed.")
    return document


def validate_snapshot(source: Path) -> dict[str, int]:
    """Check a snapshot file without touching PostgreSQL; returns row counts per table."""
    document = _load(source)
    return {name: len(document["tables"][name]["rows"]) for name in pg.TABLES if name in document["tables"]}


def import_snapshot(source: Path, *, replace: bool = False) -> dict[str, int]:
    """Import in one PostgreSQL transaction. replace=False refuses any nonempty target."""
    document = _load(source)
    pg.initialize()
    with closing(pg.connect(pooled=False)) as conn, conn:
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
            data = document["tables"].get(table)
            if data is None:
                counts[table] = 0
                continue
            index = {col: i for i, col in enumerate(data["columns"])}
            columns = [c for c in allowed if c in index]
            if not columns:
                raise ValueError(f"No compatible columns in {table}")
            fields = ", ".join(f'"{c}"' for c in columns)
            placeholders = ", ".join("?" for _ in columns)
            rows = [[_decode(row[index[c]]) for c in columns] for row in data["rows"]]
            for start in range(0, len(rows), 1000):
                conn.executemany(f'INSERT INTO "{table}" ({fields}) VALUES ({placeholders})', rows[start:start + 1000])
            counts[table] = len(rows)
        # ALTER SEQUENCE is transactional, unlike setval(). Prevent collisions after import.
        next_id = int(conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM user_reminders").fetchone()[0])
        next_sort = int(conn.execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM user_channels_multi").fetchone()[0])
        conn.execute(f"ALTER SEQUENCE public.user_reminders_id_seq RESTART WITH {max(1, next_id)}")
        conn.execute(f"ALTER SEQUENCE public.user_channels_multi_sort_order_seq RESTART WITH {max(1, next_sort)}")
        return counts
