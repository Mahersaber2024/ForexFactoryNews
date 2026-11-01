"""
Full backup / restore for the Telegram bot.

A backup is a .zip containing:
    manifest.json   → format version, creation time, row counts
    forexbot.json   → portable JSON snapshot of PostgreSQL application data
    settings.env    → copy of .env (bot token, admin id, API url, ...)

Backups are NEVER kept on the server. create_backup() builds the zip in a private temporary
folder, the bot sends it to the admin in Telegram and then deletes it (discard_backup).

Restore accepts one of these .zip files. Every restore first makes a "pre-restore" safety backup,
which is sent to the admin in Telegram and deleted from the server right after.
"""
from __future__ import annotations

import json
import os
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
ZIP_ENV = "settings.env"
ZIP_MANIFEST = "manifest.json"
ZIP_BUILDS = "downloads/"                  # files uploaded to the site from the bot (EA builds, PDFs, ...)
BUILD_EXT = (".ex5", ".ex4")             # kept for compatibility
# never restore files the web server would run/render (same list as ea_admin)
BLOCKED_EXT = (".html", ".htm", ".xhtml", ".svg", ".js", ".mjs", ".php", ".phtml", ".cgi", ".py", ".sh", ".htaccess")
REQUIRED_TABLES = {"subscribers"}          # minimum for a backup to be considered valid
MAX_RESTORE_BYTES = 20 * 1024 * 1024       # Telegram bot API download limit


class BackupError(Exception):
    """Raised with a human-readable message when a backup can't be created/restored."""


@dataclass
class RestorePlan:
    source: Path
    workdir: Path
    db_file: Path
    env_values: dict[str, str] | None
    builds_dir: Path | None = None
    manifest: dict = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def has_settings(self) -> bool:
        return bool(self.env_values)

    @property
    def builds(self) -> list[Path]:
        return sorted(self.builds_dir.iterdir()) if self.builds_dir else []

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


def _parse_env_text(text: str) -> dict[str, str]:
    fd, name = tempfile.mkstemp(suffix=".env")
    os.close(fd)
    tmp = Path(name)
    try:
        tmp.write_text(text, encoding="utf-8")
        return cfg.read_env(tmp)
    finally:
        tmp.unlink(missing_ok=True)


# ───────────────  create  ────────────────
def create_backup(label: str = "manual") -> Path:
    """Create a full backup zip in a private temporary folder and return its path.
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
            builds = _build_files()
            manifest = {
                "app": APP_ID,
                "format": BACKUP_FORMAT,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "label": label,
                "tables": counts,
                "includes_settings": cfg.ENV_FILE.exists(),
                "ea_builds": [b.name for b in builds],
            }
            with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(ZIP_MANIFEST, json.dumps(manifest, indent=2, ensure_ascii=False))
                zf.write(snap, ZIP_DB)
                if cfg.ENV_FILE.exists():
                    zf.write(cfg.ENV_FILE, ZIP_ENV)
                for f in builds:
                    zf.write(f, ZIP_BUILDS + f.name)
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


def _build_files() -> list[Path]:
    """Every file in the site's downloads folder (managed from /admin → Downloads)."""
    d = cfg.DOWNLOADS_DIR
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.is_file() and not p.name.startswith("."))


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
        env_values: dict[str, str] | None = None
        builds_dir: Path | None = None
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
                # Extract only the known members by name (no zip-slip)
                with zf.open(ZIP_DB) as src, open(db_file, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                if ZIP_ENV in names:
                    env_values = _parse_env_text(zf.read(ZIP_ENV).decode("utf-8", errors="replace")) or None
                for n in names:
                    if not n.startswith(ZIP_BUILDS):
                        continue
                    fname = n[len(ZIP_BUILDS):]
                    # flat file names only (no folders / path tricks)
                    if not fname or "/" in fname or "\\" in fname or fname.startswith(".") \
                            or fname.lower().endswith(BLOCKED_EXT):
                        continue
                    if builds_dir is None:
                        builds_dir = workdir / "downloads"
                        builds_dir.mkdir()
                    with zf.open(n) as src, open(builds_dir / fname, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        else:
            raise BackupError("Unsupported file. Send a backup .zip file.")

        counts = _check_db(db_file)
        return RestorePlan(source=source, workdir=workdir, db_file=db_file, env_values=env_values,
                           builds_dir=builds_dir, manifest=manifest, counts=counts)
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        raise


def apply_restore(plan: RestorePlan, include_settings: bool = False) -> tuple[Path | None, bool]:
    """
    Restore the staged backup into the live DB (and optionally .env).
    Download files in the backup are copied back into the downloads folder (existing files are kept).
    Returns (safety_backup_path, settings_changed). The caller sends the safety file to the admin and
    then deletes it with discard_backup().
    """
    safety = create_backup("pre-restore")
    try:
        try:
            snapshot.import_snapshot(plan.db_file, replace=True)
        except Exception as exc:
            raise BackupError(f"PostgreSQL restore failed and was rolled back: {exc}") from exc

        settings_changed = False
        if include_settings and plan.env_values:
            current = cfg.read_env()
            new_values = dict(plan.env_values)
            # Keep the current PostgreSQL target; an old backup may have no DB credentials.
            for key in ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_SSLMODE"):
                if key in current:
                    new_values[key] = current[key]
                else:
                    new_values.pop(key, None)
            # Never wipe a working token / admin with an empty value from the backup
            for key in ("TELEGRAM_BOT_TOKEN", "ADMIN_CHAT_ID"):
                if not new_values.get(key) and current.get(key):
                    new_values[key] = current[key]
            settings_changed = new_values != current
            cfg.write_env(new_values)
            cfg.reload()

        if plan.builds and cfg.DOWNLOADS_DIR.parent.is_dir():
            cfg.DOWNLOADS_DIR.mkdir(exist_ok=True)
            for b in plan.builds:
                dest = cfg.DOWNLOADS_DIR / b.name
                shutil.copyfile(b, dest)
                dest.chmod(0o644)
    except Exception:
        discard_backup(safety)       # nothing is left behind on the server
        raise

    plan.cleanup()
    return safety, settings_changed
