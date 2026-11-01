"""
Data backup / restore for the Telegram bot.

A backup is a .zip containing ONLY the bot's PostgreSQL application data:
    manifest.json   → format version, creation time, row counts
    forexbot.json   → portable JSON snapshot of the application tables

It deliberately does NOT contain: .env / install-time settings (bot token, admin id, API url, ports,
DB credentials ...), website files, downloads, or the API. Those are recreated by the installer.

Backups are NEVER kept on the server. create_backup() builds the zip in a private temporary
folder, the bot sends it to the admin in Telegram and then deletes it (discard_backup).

Restore accepts one of these .zip files (older backups that also contain settings.env / downloads/ are
accepted, but those extra entries are ignored). Every restore first makes a "pre-restore" safety backup,
which is sent to the admin in Telegram and deleted from the server right after.
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import bot_settings as cfg
from db import postgres
from db import snapshot

BACKUP_FORMAT = 1
APP_ID = "forexfactory-bot"
ZIP_DB = "forexbot.json"
ZIP_MANIFEST = "manifest.json"
REQUIRED_TABLES = {"subscribers"}          # minimum for a backup to be considered valid
MAX_RESTORE_BYTES = 20 * 1024 * 1024       # Telegram bot API download limit


class BackupError(Exception):
    """Raised with a human-readable message when a backup can't be created/restored."""


@dataclass
class RestorePlan:
    source: Path
    workdir: Path
    db_file: Path
    manifest: dict = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    def cleanup(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)


# ───────────────  helpers  ────────────────
def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def live_table_counts() -> dict[str, int]:
    from contextlib import closing
    with closing(postgres.connect()) as conn:
        return {table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                for table in postgres.TABLES}


def _check_db(db: Path) -> dict[str, int]:
    try:
        counts = snapshot.validate_snapshot(db)
    except snapshot.SnapshotError as exc:
        raise BackupError(str(exc)) from None
    missing = REQUIRED_TABLES - counts.keys()
    if missing:
        raise BackupError(f"This doesn't look like a bot backup (missing tables: {', '.join(sorted(missing))}).")
    return counts


# ───────────────  create  ────────────────
def create_backup(label: str = "manual") -> Path:
    """Create a data-only backup zip in a private temporary folder and return its path.
    The caller must delete it with discard_backup() as soon as it has been delivered."""
    label = re.sub(r"[^a-z\-]", "", label.lower()) or "manual"
    out_dir = Path(tempfile.mkdtemp(prefix="forexbot-backup-"))
    out_dir.chmod(0o700)
    out = out_dir / f"backup-{_now_stamp()}-{label}.zip"
    try:
        with tempfile.TemporaryDirectory() as td:
            snap = Path(td) / ZIP_DB
            try:
                counts = snapshot.export_snapshot(snap)
            except Exception as exc:
                raise BackupError(f"PostgreSQL backup failed: {exc}") from exc
            manifest = {
                "app": APP_ID,
                "format": BACKUP_FORMAT,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "label": label,
                "tables": counts,
            }
            with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(ZIP_MANIFEST, json.dumps(manifest, indent=2, ensure_ascii=False))
                zf.write(snap, ZIP_DB)
        out.chmod(0o600)
    except Exception:
        discard_backup(out)
        raise
    return out


def discard_backup(path: Path | None) -> None:
    """Delete a backup made by create_backup (the file and its private temporary folder)."""
    if path is None:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass
    try:
        Path(path).parent.rmdir()
    except OSError:
        pass


# ───────────────  restore  ────────────────
def prepare_restore(source: Path) -> RestorePlan:
    """Validate a backup zip and stage it. Does NOT touch live data."""
    if not source.exists():
        raise BackupError("Backup file not found.")
    if source.stat().st_size > MAX_RESTORE_BYTES:
        raise BackupError("Backup is larger than 20 MB, Telegram can't handle it.")

    workdir = Path(tempfile.mkdtemp(prefix="restore-", dir=str(cfg.DATA_DIR)))
    try:
        db_file = workdir / ZIP_DB
        manifest: dict = {}

        if zipfile.is_zipfile(source):
            with zipfile.ZipFile(source) as zf:
                names = set(zf.namelist())
                if len(names) != len(zf.namelist()):
                    raise BackupError("Backup contains duplicate entries.")
                if sum(info.file_size for info in zf.infolist()) > 200 * 1024 * 1024:
                    raise BackupError("Uncompressed backup is larger than 200 MB.")
                bad = zf.testzip()
                if bad:
                    raise BackupError(f"Zip is damaged (bad entry: {bad}).")
                if ZIP_DB not in names:
                    raise BackupError("Zip doesn't contain forexbot.json, this isn't a bot backup (backups made by older SQLite-based versions are not supported).")
                if ZIP_MANIFEST in names:
                    try:
                        manifest = json.loads(zf.read(ZIP_MANIFEST).decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        raise BackupError("manifest.json is unreadable.") from None
                    if manifest.get("app") not in (None, APP_ID):
                        raise BackupError("This backup belongs to a different app.")
                    if int(manifest.get("format", 0)) > BACKUP_FORMAT:
                        raise BackupError("Backup was made by a newer version of the bot, update first.")
                # Extract only the database snapshot, by name (no zip-slip).
                # settings.env / downloads/ in older backups are intentionally ignored.
                with zf.open(ZIP_DB) as src, open(db_file, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        else:
            raise BackupError("Unsupported file. Send a backup .zip file.")

        counts = _check_db(db_file)
        return RestorePlan(source=source, workdir=workdir, db_file=db_file,
                           manifest=manifest, counts=counts)
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        raise


def apply_restore(plan: RestorePlan) -> Path | None:
    """
    Restore the staged backup into the live PostgreSQL database. Settings (.env) and site files are never touched.
    Returns the safety backup path; the caller sends it to the admin and then deletes it with discard_backup().
    """
    safety = create_backup("pre-restore")
    try:
        try:
            snapshot.import_snapshot(plan.db_file, replace=True)
        except Exception as exc:
            raise BackupError(f"PostgreSQL restore failed and was rolled back: {exc}") from exc
    except Exception:
        discard_backup(safety)       # nothing is left behind on the server
        raise

    plan.cleanup()
    return safety
