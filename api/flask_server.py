import hmac
import json
import logging
import os
import pathlib
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

# Make the project root importable (for bot_settings / api package)
ROOT_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from flask import Flask, jsonify, request
import pytz

import bot_settings as cfg
from api.forexFactoryScrapper import getRecords, getURL, get_start_of_week, ALL_IMPACTS

HOST = cfg.FLASK_HOST
PORT = cfg.FLASK_PORT
DEBUG = cfg.FLASK_DEBUG

# Configure logging to display information in the console
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

NY_TZ = pytz.timezone("America/New_York")
SCHEDULE_TZ = pytz.timezone("Asia/Tehran")      # fixed refresh times are given in Tehran time (like the bot)

def ny_now():
    return datetime.now(NY_TZ)

# ───────────────  Cache  ────────────────
# The cache lives in memory AND on disk (data/api_cache.json), so a restart no longer throws it away.
# Requests are always answered from the cache (even when it is a little old, so they are instant);
# a background scheduler keeps it fresh. The refresh schedule is editable from the bot's admin panel.
KEYS = ("today", "tomorrow", "weekly", "yesterday")
SCHEDULED_KEYS = ("today", "tomorrow", "weekly")   # yesterday is only re-read when the day changes
CACHE_FILE = cfg.DATA_DIR / "api_cache.json"
SETTINGS_FILE = cfg.DATA_DIR / "api_cache_settings.json"
DEFAULT_SETTINGS = {"interval_minutes": 5, "times": []}   # 5 min = the old behaviour
RETRY_AFTER = 60          # seconds to wait after a failed refresh before trying again
TICK = 20                 # how often the scheduler looks at the cache

cache: dict[str, dict | None] = {k: None for k in KEYS}   # {"data": [...], "last_update": iso-utc, "for": "YYYY-MM-DD"}
meta: dict[str, dict] = {k: {"last_attempt": 0.0, "last_error": None} for k in KEYS}
_locks = {k: threading.Lock() for k in KEYS}
_file_lock = threading.Lock()

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)

# ---- disk ----
def _atomic_write(path: pathlib.Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)

def _save_cache() -> None:
    with _file_lock:
        try:
            _atomic_write(CACHE_FILE, cache)
        except OSError as e:
            logging.warning(f"Could not save the cache to disk: {e}")

def _load_cache() -> None:
    try:
        saved = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for key in KEYS:
        entry = saved.get(key) if isinstance(saved, dict) else None
        if isinstance(entry, dict) and isinstance(entry.get("data"), list) and entry.get("for") and entry.get("last_update"):
            cache[key] = entry
    logging.info("Cache restored from disk: " + ", ".join(f"{k}={'yes' if cache[k] else 'no'}" for k in KEYS))

# ---- settings ----
def _clean_times(values) -> list[str]:
    out = set()
    for v in values or []:
        try:
            h, m = str(v).strip().split(":")
            h, m = int(h), int(m)
        except ValueError:
            raise ValueError(f"bad time: {v!r}")
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError(f"bad time: {v!r}")
        out.add(f"{h:02d}:{m:02d}")
    if len(out) > 24:
        raise ValueError("at most 24 times")
    return sorted(out)

def load_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        settings["interval_minutes"] = max(0, min(1440, int(saved.get("interval_minutes", settings["interval_minutes"]))))
        settings["times"] = _clean_times(saved.get("times", []))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return settings

def save_settings(settings: dict) -> None:
    _atomic_write(SETTINGS_FILE, settings)

# ---- what each key should contain right now ----
def _target(key: str):
    now = ny_now()
    if key == "today":
        return now.date()
    if key == "tomorrow":
        return (now + timedelta(days=1)).date()
    if key == "yesterday":
        return (now - timedelta(days=1)).date()
    return get_start_of_week(now).date()      # weekly: the Monday of this week

def _fetch(key: str):
    now = ny_now()
    if key == "weekly":
        base = get_start_of_week(now)
        url = getURL(base, "week")
    else:
        base = now + timedelta(days={"today": 0, "tomorrow": 1, "yesterday": -1}[key])
        url = getURL(base, "day")
    return getRecords(url, base, ALL_IMPACTS), _target(key)

def _is_current(key: str) -> bool:
    entry = cache.get(key)
    return bool(entry) and entry["for"] == _target(key).isoformat()

def refresh(key: str, force: bool = True) -> bool:
    """Scrape ForexFactory and store the result. force=False: skip if somebody else just refreshed it."""
    with _locks[key]:
        if not force and _is_current(key):
            return True
        meta[key]["last_attempt"] = time.time()
        try:
            records, target = _fetch(key)
        except Exception as e:     # network, Cloudflare, page layout ... the old cache stays in place
            meta[key]["last_error"] = f"{type(e).__name__}: {e}"[:200]
            logging.warning(f"Refresh of '{key}' failed: {meta[key]['last_error']}")
            return False
        cache[key] = {"data": records, "last_update": _utcnow().isoformat(), "for": target.isoformat()}
        meta[key]["last_error"] = None
    _save_cache()
    logging.info(f"Refreshed '{key}': {len(records)} records")
    return True

def get_cached(key: str):
    """Instant answer from the cache. Only when nothing usable exists (first run, or a new day whose
    data was not fetched yet) does the request wait for one scrape."""
    if _is_current(key):
        return cache[key]["data"]
    refresh(key, force=False)
    return cache[key]["data"] if _is_current(key) else None

# ---- schedule ----
def _scheduled_moments(times: list[str], around: datetime) -> list[datetime]:
    """Every fixed refresh time from yesterday to tomorrow (Tehran), as aware datetimes."""
    local = around.astimezone(SCHEDULE_TZ)
    moments = []
    for day_shift in (-1, 0, 1):
        day = (local + timedelta(days=day_shift)).date()
        for t in times:
            h, m = map(int, t.split(":"))
            moments.append(SCHEDULE_TZ.localize(datetime(day.year, day.month, day.day, h, m)))
    return sorted(moments)

def _is_due(key: str, settings: dict, now: datetime) -> bool:
    if not _is_current(key):                 # missing, or the day/week rolled over
        return True
    if key not in SCHEDULED_KEYS:
        return False
    last = _parse_iso(cache[key]["last_update"])
    interval = settings["interval_minutes"]
    if interval and now - last >= timedelta(minutes=interval):
        return True
    passed = [t for t in _scheduled_moments(settings["times"], now) if t <= now]
    return bool(passed) and last < passed[-1]

def _next_refresh(settings: dict, now: datetime):
    candidates = []
    entry = cache.get("today")
    if settings["interval_minutes"] and entry:
        candidates.append(_parse_iso(entry["last_update"]) + timedelta(minutes=settings["interval_minutes"]))
    candidates += [t for t in _scheduled_moments(settings["times"], now) if t > now]
    if not candidates:
        return None
    nxt = min(candidates)
    return max(nxt, now)

def _scheduler() -> None:
    time.sleep(2)
    while True:
        try:
            settings = load_settings()           # re-read every tick: changes from the admin panel apply at once
            for key in KEYS:
                now = _utcnow()
                recently_failed = meta[key]["last_error"] and time.time() - meta[key]["last_attempt"] < RETRY_AFTER
                if not recently_failed and _is_due(key, settings, now):
                    refresh(key)
                    time.sleep(2)                # be gentle with the website
        except Exception:
            logging.exception("Cache scheduler error")
        time.sleep(TICK)

_started = False
def start_background() -> None:
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_scheduler, name="cache-scheduler", daemon=True).start()

_load_cache()     # restore what the last run knew, before the first request arrives

# ───────────────  Admin endpoints (used by the bot's admin panel)  ────────────────
def _admin_allowed() -> bool:
    token = getattr(cfg, "CACHE_ADMIN_TOKEN", "")
    if token:
        return hmac.compare_digest(request.headers.get("X-Admin-Token", ""), token)
    # No token configured: only the same machine, and never through a reverse proxy
    proxied = request.headers.get("X-Forwarded-For") or request.headers.get("X-Real-IP")
    return request.remote_addr in ("127.0.0.1", "::1") and not proxied

def _iso(dt):
    return dt.isoformat() if dt else None

@app.route("/api/forex/cache", methods=["GET"])
def cache_status():
    settings = load_settings()
    now = _utcnow()
    entries = {}
    for key in KEYS:
        entry = cache[key]
        entries[key] = {
            "for": entry["for"] if entry else None,
            "last_update": entry["last_update"] if entry else None,
            "age_seconds": int((now - _parse_iso(entry["last_update"])).total_seconds()) if entry else None,
            "records": len(entry["data"]) if entry else 0,
            "current": _is_current(key),
            "last_error": meta[key]["last_error"],
        }
    return jsonify({
        "settings": {**settings, "tz": SCHEDULE_TZ.zone},
        "now": now.isoformat(),
        "next_refresh": _iso(_next_refresh(settings, now)),
        "entries": entries,
    }), 200

@app.route("/api/forex/cache/settings", methods=["POST"])
def cache_set_settings():
    if not _admin_allowed():
        return jsonify({"error": "forbidden"}), 403
    body = request.get_json(silent=True) or {}
    settings = load_settings()
    try:
        if "interval_minutes" in body:
            iv = int(body["interval_minutes"])
            if not 0 <= iv <= 1440:
                raise ValueError("interval_minutes must be 0-1440 (0 = off)")
            settings["interval_minutes"] = iv
        if "times" in body:
            settings["times"] = _clean_times(body["times"])
    except (ValueError, TypeError) as e:
        return jsonify({"error": str(e)}), 400
    save_settings(settings)
    logging.info(f"Cache settings changed: {settings}")
    return jsonify({"settings": {**settings, "tz": SCHEDULE_TZ.zone}}), 200

@app.route("/api/forex/cache/refresh", methods=["POST"])
def cache_refresh_now():
    if not _admin_allowed():
        return jsonify({"error": "forbidden"}), 403
    def run():
        for key in KEYS:
            refresh(key)
    threading.Thread(target=run, name="cache-manual-refresh", daemon=True).start()
    return jsonify({"started": True}), 202

# ───────────────  Public data endpoints  ────────────────
def _wanted_impacts() -> set[str]:
    """?impact=high,medium,low (or all). No parameter = High only, so the EA keeps getting exactly what it always got."""
    raw = (request.args.get("impact") or "high").lower()
    if raw == "all":
        return set(ALL_IMPACTS)
    wanted = {lvl for lvl in ALL_IMPACTS if lvl.lower() in {p.strip() for p in raw.split(",")}}
    return wanted or {"High"}

def _respond(records):
    wanted = _wanted_impacts()
    return jsonify([r for r in (records or []) if r.get("Impact") in wanted]), 200

def _serve(key: str):
    records = get_cached(key)
    if records is None:
        return jsonify({"error": "data is not available yet, try again shortly"}), 503
    return _respond(records)

@app.route('/api/forex/today', methods=['GET'])
def today_data():
    return _serve("today")

@app.route('/api/forex/weekly', methods=['GET'])
def weekly_data():
    return _serve("weekly")

@app.route('/api/forex/tomorrow', methods=['GET'])
def tomorrow_data():
    return _serve("tomorrow")

@app.route('/api/forex/yesterday', methods=['GET'])
def yesterday_data():
    return _serve("yesterday")

if __name__ == '__main__':
    if not DEBUG or os.environ.get("WERKZEUG_RUN_MAIN") == "true":   # the debug reloader starts a second process
        start_background()
    app.run(host=HOST, port=PORT, debug=DEBUG)
