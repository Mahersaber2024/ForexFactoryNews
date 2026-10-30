"""
Full backup / restore for the Telegram bot.

A backup is a .zip containing:
    manifest.json   → format version, creation time, row counts
    forexbot.db     → consistent snapshot of the SQLite DB (via sqlite backup API)
    settings.env    → copy of .env (bot token, admin id, API url, ...)

Restore accepts either one of these .zip files or a raw forexbot.db file
(e.g. from an older version). Every restore first saves a "pre-restore"
safety backup, so a bad restore can always be undone from the admin panel.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import bot_settings as cfg

BACKUP_FORMAT = 1
APP_ID = "forexfactory-bot"
ZIP_DB = "forexbot.db"
ZIP_ENV = "settings.env"
ZIP_MANIFEST = "manifest.json"
ZIP_BUILDS = "downloads/"                  # files uploaded to the site from the bot (EA builds, PDFs, ...)
BUILD_EXT = (".ex5", ".ex4")             # kept for compatibility
# never restore files the web server would run/render (same list as ea_admin)
BLOCKED_EXT = (".html", ".htm", ".xhtml", ".svg", ".js", ".mjs", ".php", ".phtml", ".cgi", ".py", ".sh", ".htaccess")
REQUIRED_TABLES = {"subscribers"}          # minimum for a backup to be considered valid
MAX_RESTORE_BYTES = 20 * 1024 * 1024       # Telegram bot API download limit
SQLITE_MAGIC = b"SQLite format 3\x00"
NAME_RE = re.compile(r"^backup-\d{8}-\d{6}-[a-z\-]+\.zip$")


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


def _sqlite_copy(src: Path, dst: Path) -> None:
    """Copy a SQLite DB page-by-page (safe even while the bot is using it)."""
    s = sqlite3.connect(str(src), timeout=30)
    d = sqlite3.connect(str(dst), timeout=30)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def _table_counts(db: Path) -> dict[str, int]:
    conn = sqlite3.connect(str(db))
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        tables = [r[0] for r in cur.fetchall()]
        return {t: cur.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    finally:
        conn.close()


def _check_db(db: Path) -> dict[str, int]:
    with open(db, "rb") as fh:
        if fh.read(16) != SQLITE_MAGIC:
            raise BackupError("The database inside the backup is not a valid SQLite file.")
    conn = sqlite3.connect(str(db))
    try:
        res = conn.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.DatabaseError as e:
        raise BackupError(f"Database is corrupted: {e}") from None
    finally:
        conn.close()
    if not res or res[0] != "ok":
        raise BackupError(f"Database integrity check failed: {res[0] if res else 'unknown'}")
    counts = _table_counts(db)
    missing = REQUIRED_TABLES - counts.keys()
    if missing:
        raise BackupError(f"This doesn't look like a bot backup (missing tables: {', '.join(sorted(missing))}).")
    return counts


def _parse_env_text(text: str) -> dict[str, str]:
    tmp = Path(tempfile.mkstemp(suffix=".env")[1])
    try:
        tmp.write_text(text, encoding="utf-8")
        return cfg.read_env(tmp)
    finally:
        tmp.unlink(missing_ok=True)


# ───────────────  create  ────────────────
def create_backup(label: str = "manual") -> Path:
    """Create a full backup zip in data/backups and return its path."""
    label = re.sub(r"[^a-z\-]", "", label.lower()) or "manual"
    cfg.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if not cfg.DB_PATH.exists():
        raise BackupError("No database yet, nothing to back up.")

    out = cfg.BACKUP_DIR / f"backup-{_now_stamp()}-{label}.zip"
    n = 1
    while out.exists():                      # two backups in the same second
        out = cfg.BACKUP_DIR / f"backup-{_now_stamp()}-{label}{'-' * n}.zip"
        n += 1

    with tempfile.TemporaryDirectory() as td:
        snap = Path(td) / ZIP_DB
        _sqlite_copy(cfg.DB_PATH, snap)
        counts = _table_counts(snap)
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
        tmp_zip = out.with_suffix(".part")
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(ZIP_MANIFEST, json.dumps(manifest, indent=2, ensure_ascii=False))
            zf.write(snap, ZIP_DB)
            if cfg.ENV_FILE.exists():
                zf.write(cfg.ENV_FILE, ZIP_ENV)
            for f in builds:
                zf.write(f, ZIP_BUILDS + f.name)
        tmp_zip.replace(out)

    prune_backups()
    return out


def _build_files() -> list[Path]:
    """Every file in the site's downloads folder (managed from /admin → Downloads)."""
    d = cfg.DOWNLOADS_DIR
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.is_file() and not p.name.startswith("."))


def list_backups() -> list[Path]:
    if not cfg.BACKUP_DIR.exists():
        return []
    files = [p for p in cfg.BACKUP_DIR.glob("backup-*.zip") if p.is_file()]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def prune_backups(keep: int | None = None) -> None:
    keep = keep or cfg.BACKUP_KEEP
    for old in list_backups()[keep:]:
        old.unlink(missing_ok=True)


def resolve_backup_name(name: str) -> Path | None:
    """Safely map a button payload back to a file in BACKUP_DIR (no path traversal)."""
    if not NAME_RE.match(name):
        return None
    p = (cfg.BACKUP_DIR / name).resolve()
    if p.parent != cfg.BACKUP_DIR.resolve() or not p.is_file():
        return None
    return p


def describe(path: Path) -> str:
    size_kb = path.stat().st_size / 1024
    return f"{path.name} ({size_kb:.1f} KB)"


# ───────────────  restore  ────────────────
def prepare_restore(source: Path) -> RestorePlan:
    """Validate a backup file (zip or raw .db) and stage it. Does NOT touch live data."""
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

        with open(source, "rb") as fh:
            head = fh.read(16)

        if head == SQLITE_MAGIC:
            # Raw forexbot.db (older versions / manual copy)
            shutil.copyfile(source, db_file)
            manifest = {"app": APP_ID, "format": 0, "label": "raw-db"}
        elif zipfile.is_zipfile(source):
            with zipfile.ZipFile(source) as zf:
                bad = zf.testzip()
                if bad:
                    raise BackupError(f"Zip is damaged (bad entry: {bad}).")
                names = set(zf.namelist())
                if ZIP_DB not in names:
                    raise BackupError("Zip doesn't contain forexbot.db, this isn't a bot backup.")
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
            raise BackupError("Unsupported file. Send a backup .zip or a forexbot.db file.")

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
    Returns (safety_backup_path, settings_changed).
    """
    safety = None
    if cfg.DB_PATH.exists():
        safety = create_backup("pre-restore")

    cfg.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Copy INTO the live DB with the sqlite backup API: atomic from SQLite's
    # point of view and safe while other connections exist.
    _sqlite_copy(plan.db_file, cfg.DB_PATH)

    # Verify what we just wrote
    _check_db(cfg.DB_PATH)

    settings_changed = False
    if include_settings and plan.env_values:
        current = cfg.read_env()
        new_values = dict(plan.env_values)
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

    plan.cleanup()
    return safety, settings_changed
