"""
calendar_image.py: the economic calendar as a picture, in 7 numbered designs that differ in LAYOUT (not just colour).

    render_calendar(records, style="timeline", title=..., logo=<path|bytes|PIL>)  -> PNG bytes
    render_sheet(logo)                                                            -> ONE PNG: all 8 designs, numbered

    1 Timeline   glass cards on a glowing time axis
    2 Bento      soft tiles, big date hero
    3 Soft Ledger  the Ledger table in a tinted (not white) theme; key stays "editorial" so saved channel settings keep working
    4 HUD        futuristic terminal with LED impact meters
    5 Ticket     perforated boarding-pass tickets with a stamped result
    6 Spotlight  the key event as a giant hero, the rest in a list
    7 Ledger     the whole day in one compact table: impact counts, a 24h pulse strip, tinted high-impact rows

Every design is generated from the main colour of the user's logo. Drawn at 2x and scaled down.
Needs only Pillow. Fonts live in bot/fonts/ (Carlito, Liberation Serif, DejaVu Sans / Sans Mono; Persian: Vazirmatn, fetched by bot/font_setup.py) with system fallbacks.
Persian text needs a real Persian font (Vazirmatn in bot/fonts/ or `apt install fonts-vazirmatn`/`fonts-noto-core`); DejaVu is the last resort and looks rough.
"""
from __future__ import annotations

import colorsys
import contextvars
import functools
import io
import math
import random
import re
from pathlib import Path

from PIL import features, Image, ImageChops, ImageDraw, ImageFilter, ImageFont
from bot import image_locale

BOT_NAME = "@MarketingToolssBot"
HERE = Path(__file__).parent
DEFAULT_LOGO = HERE / "logo.jpg"
DEFAULT_BRAND = (53, 85, 138)
SS = 2
W = 1080

STYLES = [("timeline", "Timeline"), ("bento", "Bento"), ("editorial", "Soft Ledger"),
          ("hud", "HUD"), ("ticket", "Ticket"), ("spotlight", "Spotlight"),
          ("aurora_light", "Aurora Light"), ("aurora", "Aurora Dark")]
# Designs built for MANY rows get more events per picture (the rest use 12).
ROWS_PER_STYLE = {"ledger": 20, "editorial": 20}


def rows_per_image(style: str, default: int = 12) -> int:
    return ROWS_PER_STYLE.get(style, default)
STYLE_KEYS = [k for k, _ in STYLES]
STYLE_NAMES = dict(STYLES)

# Which clock the news rows are in and which language the labels speak. ContextVars so parallel renders never mix.
_TZ = contextvars.ContextVar("calendar_tz", default="ny")          # legacy key ("ny"/"tehran") or a zone name
_LANG = contextvars.ContextVar("calendar_lang", default="en")      # "fa" only when Persian can really be drawn
_ZT = contextvars.ContextVar("calendar_zone_text", default=None)   # {sub, caps, mono, plain} for this render
_CH = contextvars.ContextVar("calendar_channel", default="")        # "@mychannel" drawn in the header ("" = nothing)
TZ_TEXT = {"ny": dict(sub="New York time (ET)", caps="NEW YORK TIME (ET)", mono="T+0 NEW YORK (ET)", plain="New York time"),
           "tehran": dict(sub="Tehran time", caps="TEHRAN TIME", mono="T+0 TEHRAN (IRST)", plain="Tehran time")}
_LEGACY_ZONES = {"ny": "America/New_York", "tehran": "Asia/Tehran"}


def _zone_text(tz_key: str, lang: str) -> dict:
    """The four captions that say which clock the times are on, for this zone and language."""
    try:
        from bot import tz as tzmod
        zone_name = _LEGACY_ZONES.get(tz_key, tz_key)
        if lang in image_locale.SUPPORTED:
            return image_locale.zone(lang, tzmod.city(zone_name, lang), tzmod.abbr(zone_name))
        return tzmod.zone_text(zone_name, lang)
    except Exception:
        return TZ_TEXT.get(tz_key, TZ_TEXT["ny"])


def _tzt(key: str) -> str:
    zt = _ZT.get() or TZ_TEXT["ny"]
    return zt[key]


def _is_fa() -> bool:
    return _LANG.get() == "fa"


_FA_DIG = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_HAS_FA = re.compile("[\u0600-\u06FF]")


def _n(x) -> str:
    """Digits of OUR labels (counts, day numbers) in Persian digits for Persian pictures. News values are never passed here."""
    return str(x).translate(_FA_DIG) if _is_fa() else str(x)


def _bn(bot: str) -> str:
    """The bot's name without the leading @ ('@MarketingToolssBot' -> 'MarketingToolssBot')."""
    return (bot or "").lstrip("@")


def _footer(bot: str) -> str:
    """Bottom line of every picture. FIXED and always English (never follows the channel language or impact filter)."""
    return f"powered by: {_bn(bot)} | ForexFactory"


_IMP_FA = {"high": "بالا", "medium": "متوسط", "low": "کم"}
_FA_EXACT = {
    "ECONOMIC CALENDAR": "تقویم اقتصادی", "ECONOMIC CALENDAR  ·  DAILY PASS": "تقویم اقتصادی  ·  بلیت روزانه",
    "// ECON.CALENDAR  ·  LIVE": "// تقویم اقتصادی  ·  زنده", "TODAY": "امروز",
    "FORECAST": "پیش‌بینی", "PREVIOUS": "قبلی", "ACTUAL": "واقعی", "FCST": "پیش‌بینی", "PREV": "قبلی", "ACT": "واقعی",
    "TIME": "ساعت", "CUR": "ارز", "EVENT": "خبر", "EVENTS": "خبر", "IMPACT": "اهمیت", "SPOTLIGHT": "خبر ویژه",
    "ADMIT": "ورود", "Source": "منبع", "F": "پیش‌بینی", "P": "قبلی",
    "Low": "کم", "Medium": "متوسط", "High": "بالا",
    "DAY PULSE": "نبض روز", "SPEECH": "سخنرانی", "CONFERENCE": "کنفرانس", "Forex Economic News": "اخبار اقتصادی فارکس",
}


def _fa(s: str) -> str:
    """English label -> Persian label (only OUR fixed texts; event names, currencies and values pass through)."""
    if s in _FA_EXACT:
        return _FA_EXACT[s]
    m = re.fullmatch(r"(?i)(high|medium|low) impact", s)
    if m:
        return "اهمیت " + _IMP_FA[m.group(1).lower()]
    m = re.fullmatch(r"(\d+) (?:events?|EVENTS?)", s)
    if m:
        return f"{_n(m.group(1))} خبر"
    m = re.fullmatch(r"\[ (\d+) EVENTS? \]", s)
    if m:
        return f"[ {_n(m.group(1))} خبر ]"
    m = re.fullmatch(r"(\d+) high impact", s)
    if m:
        return f"{_n(m.group(1))} با اهمیت بالا"
    m = re.fullmatch(r"NO\. (\d+)", s)
    if m:
        return f"شماره {_n(m.group(1))}"
    if re.fullmatch(r"(?i)(> )?powered by: .+ \| forexfactory", s):
        return s
    m = re.fullmatch(r"by (.+)", s)
    if m:
        return f"توسط \u200e{m.group(1)}\u200e"
    m = re.fullmatch(r"Source: ForexFactory  ·  (.*)  \|  by (.+)", s)
    if m:
        return f"منبع: ForexFactory  ·  {_fa(m.group(1))}  |  توسط \u200e{m.group(2)}\u200e"
    m = re.fullmatch(r"> (.*)  \|  BY (.+)", s)
    if m:
        return f"> {_fa(m.group(1))}  |  توسط \u200e{m.group(2)}\u200e"
    m = re.fullmatch(r"(.*)  ·  SRC FOREXFACTORY", s)
    if m:
        return f"{m.group(1)}  ·  منبع: ForexFactory"
    return s


def _prep(s: str, fam: str, track: float):
    """(text, font family, letter spacing) after language: Persian text uses the Persian font and never letter-spacing."""
    if _LANG.get() in image_locale.SUPPORTED:
        s = image_locale.translate(_LANG.get(), s)
        # Combining marks and Indic conjuncts must be shaped as one run, never
        # drawn character-by-character by the tracking loop.
        native = "native_hi" if re.search(r"[\u0900-\u097f]", s) else "native_latin"
        return s, native, 0
    if _LANG.get() != "fa":
        return s, fam, track
    s2 = _fa(s)
    if _HAS_FA.search(s2):
        return s2, "fa", 0
    return s2, fam, track


def _dline(wd: str, mon: str, day: str, key: str = "", long: bool = False) -> str:
    """'Thu, Oct 29' / 'پنج‌شنبه ۲۹ اکتبر': just the date. (The «به وقت تهران» / «New York time» tail is gone on purpose.)"""
    if not day:
        return ""
    if _LANG.get() in image_locale.SUPPORTED:
        return image_locale.date(_LANG.get(), wd, mon, day)
    if _is_fa():
        from bot import tz as tzmod
        return tzmod.display_date(f"{wd}, {mon} {day}", "fa")
    return f"{DAYS.get(wd, wd)}, {MONTHS.get(mon, mon)} {day}" if long else f"{wd}, {mon} {day}"


def _date_only(wd: str, mon: str, day: str) -> str:
    """Editorial header date: 'THURSDAY, OCTOBER 29' / 'پنج‌شنبه ۲۹ اکتبر' / TODAY."""
    if not day:
        return "TODAY"
    if _LANG.get() in image_locale.SUPPORTED:
        return image_locale.date(_LANG.get(), wd, mon, day)
    if _is_fa():
        from bot import tz as tzmod
        return tzmod.display_date(f"{wd}, {mon} {day}", "fa")
    return (DAYS.get(wd, wd) + ", " + MONTHS.get(mon, mon) + " " + day).upper()


def _month_weekday(wd: str, mon: str) -> str:
    if _LANG.get() in image_locale.SUPPORTED:
        lang = _LANG.get()
        try:
            return (image_locale.MONTHS[lang][image_locale.MON_KEYS.index(mon.title()[:3])] + " · " +
                    image_locale.DAYS[lang][image_locale.DAY_KEYS.index(wd.title()[:3])])
        except ValueError:
            return mon + " · " + wd
    if _is_fa():
        from bot import tz as tzmod
        return f"{tzmod._FA_MONTHS.get(mon.lower(), mon)}  ·  {tzmod._FA_WEEKDAYS.get(wd.lower(), wd)}"
    return mon.upper() + "  ·  " + wd.upper()


def _wd_name(wd: str) -> str:
    if _LANG.get() in image_locale.SUPPORTED:
        try:
            return image_locale.DAYS[_LANG.get()][image_locale.DAY_KEYS.index(wd.title()[:3])]
        except ValueError:
            return wd
    if _is_fa():
        from bot import tz as tzmod
        return tzmod._FA_WEEKDAYS.get(wd.lower(), wd)
    return DAYS.get(wd, wd)


def _mon_name(mon: str) -> str:
    if _LANG.get() in image_locale.SUPPORTED:
        try:
            return image_locale.MONTHS[_LANG.get()][image_locale.MON_KEYS.index(mon.title()[:3])]
        except ValueError:
            return mon
    if _is_fa():
        from bot import tz as tzmod
        return tzmod._FA_MONTHS.get(mon.lower(), mon)
    return MONTHS.get(mon, mon)


def _hud_date(wd: str, mon: str, day: str) -> str:
    if _LANG.get() in image_locale.SUPPORTED:
        return image_locale.date(_LANG.get(), wd, mon, day) if day else ""
    if _is_fa():
        from bot import tz as tzmod
        return tzmod.display_date(f"{wd}, {mon} {day}", "fa") if day else ""
    return " ".join((wd.upper(), mon.upper(), day)).strip()

FONT_DIRS = [HERE / "fonts", Path("/usr/share/fonts/truetype/crosextra"),
             Path("/usr/share/fonts/truetype/liberation"), Path("/usr/share/fonts/truetype/dejavu")]
FAMILIES = {
    "sans": (["Carlito-Regular.ttf", "LiberationSans-Regular.ttf", "DejaVuSans.ttf"],
             ["Carlito-Bold.ttf", "LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf"]),
    "serif": (["LiberationSerif-Regular.ttf", "DejaVuSerif.ttf"], ["LiberationSerif-Bold.ttf", "DejaVuSerif-Bold.ttf"]),
    "mono": (["DejaVuSansMono.ttf", "LiberationMono-Regular.ttf"], ["DejaVuSansMono-Bold.ttf", "LiberationMono-Bold.ttf"]),
    "native_latin": (["NotoSans-Regular.ttf", "DejaVuSans.ttf"], ["NotoSans-Bold.ttf", "DejaVuSans-Bold.ttf"]),
    "native_hi": (["ForexLocalSans-Regular.ttf"], ["ForexLocalSans-Bold.ttf"]),
}
_fc: dict = {}
_SEARCH_DIRS = [HERE / "fonts", Path("/usr/local/share/fonts"), Path("/usr/share/fonts"), Path.home() / ".fonts",
                Path.home() / ".local/share/fonts"]
_found: dict = {}


def _find_font(name: str):
    """File of that font: bot/fonts first, then the usual dirs, searching sub-folders too (case-insensitive)."""
    if name not in _found:
        hit = next((d / name for d in FONT_DIRS if (d / name).exists()), None)
        if hit is None:
            low = name.lower()
            for d in _SEARCH_DIRS:
                if d.exists():
                    hit = next((f for f in d.rglob("*") if f.name.lower() == low), None)
                    if hit:
                        break
        _found[name] = hit
    return _found[name]


def reset_font_cache():
    """Forget which fonts were found/missing (used after font_setup downloads Vazirmatn while the bot runs)."""
    _found.clear()
    _fc.clear()


def _font(size: float, bold: bool = False, fam: str = "sans") -> ImageFont.FreeTypeFont:
    key = (round(size), bold, fam)
    if key not in _fc:
        for name in FAMILIES[fam][1 if bold else 0]:
            hit = _find_font(name)
            if hit:
                f = ImageFont.truetype(str(hit), round(size))
                if "[" in hit.name:                                  # variable font: pick the weight
                    try:
                        axes = f.get_variation_axes()
                        f.set_variation_by_axes([(700 if bold else 400) if a.get("name") in (b"Weight", "Weight") else a.get("default", 100)
                                                 for a in axes])
                    except Exception:
                        pass
                _fc[key] = f
                break
        else:
            _fc[key] = ImageFont.load_default()
    return _fc[key]


def _chan(c, x, y, anchor, col, size=22, fam="sans", max_w=300):
    """The channel id (@mychannel) in the header. Nothing is drawn when no channel was given."""
    ch = _CH.get()
    if not ch:
        return
    t, sz = c.fit(ch, size, True, fam, max_w, 14)
    c.text((x, y), t, sz, True, col, anchor, fam=fam)


def _hls(c):
    h, l, s = colorsys.rgb_to_hls(*(x / 255 for x in c[:3]))
    return h, s, l


def _rgb(h, s, l):
    r, g, b = colorsys.hls_to_rgb(h % 1.0, max(0.0, min(1.0, l)), max(0.0, min(1.0, s)))
    return (int(r * 255 + .5), int(g * 255 + .5), int(b * 255 + .5))


def _mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _load_img(x) -> Image.Image | None:
    if x is None:
        return None
    if isinstance(x, Image.Image):
        return x
    if isinstance(x, (bytes, bytearray)):
        return Image.open(io.BytesIO(x))
    return Image.open(x) if Path(x).exists() else None


def _dominant_color(img: Image.Image):
    q = img.convert("RGB").resize((96, 96)).quantize(colors=8, method=Image.MEDIANCUT)
    pal = q.getpalette()[:24]
    best, best_score = None, 0.0
    for cnt, idx in q.getcolors():
        r, g, b = pal[idx * 3: idx * 3 + 3]
        mx, mn = max(r, g, b), min(r, g, b)
        sat = (mx - mn) / mx if mx else 0
        if mx < 40 or sat < 0.25:
            continue
        if cnt * sat > best_score:
            best, best_score = (r, g, b), cnt * sat
    return best


def brand_color(logo) -> tuple:
    try:
        img = _load_img(logo)
        c = _dominant_color(img) if img is not None else None
    except Exception:
        c = None
    return c or DEFAULT_BRAND


POS_D, NEG_D = (74, 222, 128), (251, 113, 133)
POS_L, NEG_L = (22, 150, 70), (214, 40, 60)
IMPACT = {"High": (239, 68, 68), "Medium": (249, 146, 22), "Low": (234, 179, 8)}
MONTHS = {"Jan": "January", "Feb": "February", "Mar": "March", "Apr": "April", "May": "May", "Jun": "June",
          "Jul": "July", "Aug": "August", "Sep": "September", "Oct": "October", "Nov": "November", "Dec": "December"}
DAYS = {"Mon": "Monday", "Tue": "Tuesday", "Wed": "Wednesday", "Thu": "Thursday", "Fri": "Friday",
        "Sat": "Saturday", "Sun": "Sunday"}


def _date_parts(rows):
    m = re.match(r"(\w{3}),?\s+(\w{3})\s+(\d+)", (rows[0].get("Date") if rows else "") or "")
    if not m:
        return ("", "", "")
    if _is_fa():                                  # Persian picture -> Persian (Jalali) calendar
        from bot import tz as tzmod
        mon, day = tzmod.jalali_parts(m.group(2), m.group(3))
        return (m.group(1), mon, day)
    return (m.group(1), m.group(2), m.group(3))


def _num(x):
    try:
        return float("".join(ch for ch in (x or "") if ch in "0123456789.-"))
    except ValueError:
        return None


def _tone(r, dark: bool):
    a, f = _num(r.get("Actual")), _num(r.get("Forecast"))
    if a is None or f is None or a == f:
        return None
    pos, neg = (POS_D, NEG_D) if dark else (POS_L, NEG_L)
    return pos if a > f else neg



# ───────────────  speech icon: microphone in a glossy circle  ────────────────
def _si_rr(x0, y0, x1, y1, r, n=10):
    pts = []
    for cx, cy, a0 in ((x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0), (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180)):
        pts += [(cx + r * math.cos(math.radians(a0 + 90 * i / n)), cy + r * math.sin(math.radians(a0 + 90 * i / n))) for i in range(n + 1)]
    return pts


def _si_arc(cx, cy, r, a0, a1, n=40):
    return [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / n)), cy + r * math.sin(math.radians(a0 + (a1 - a0) * i / n)))
            for i in range(n + 1)]


@functools.lru_cache(maxsize=64)
def _speech_icon(col: tuple, px: int) -> Image.Image:
    """RGBA icon (px x px), NO background: a microphone with sound waves, drawn in the news-impact colour `col`."""
    S, N = 6, 600
    base = tuple(col[:3])
    light, deep = _mix(base, (255, 255, 255), .38), _mix(base, (0, 0, 0), .32)
    out = Image.new("RGBA", (N, N), (0, 0, 0, 0))

    def mask(fn):
        m = Image.new("L", (N, N), 0)
        fn(ImageDraw.Draw(m))
        return m

    def paint(m, c1, c2=None, alpha=255, clip=None):
        c2 = c2 or c1
        if c1 == c2:
            lay = Image.new("RGBA", (N, N), tuple(c1) + (255,))
        else:
            g = Image.linear_gradient("L").resize((N, N))
            lay = Image.composite(Image.new("RGBA", (N, N), tuple(c2) + (255,)), Image.new("RGBA", (N, N), tuple(c1) + (255,)), g)
        if clip is not None:
            m = ImageChops.multiply(m, clip)
        if alpha < 255:
            m = m.point(lambda v: v * alpha // 255)
        lay.putalpha(m)
        out.alpha_composite(lay)

    k = .70                                               # mic scale; waves are drawn at full size around it
    X = lambda x: (50 + (x - 50) * k) * S                  # noqa: E731
    Y = lambda y: (47 + (y - 50) * k) * S                  # noqa: E731

    def poly(pts):
        return mask(lambda d: d.polygon([(X(x), Y(y)) for x, y in pts], fill=255))

    def stroke(pts, w, scale=True, mapped=True):
        sp = [(X(x), Y(y)) if mapped else (x * S, y * S) for x, y in pts]
        ww = w * (k if mapped else 1) * S

        def fn(d):
            d.line(sp, fill=255, width=int(ww), joint="curve")
            for x, y in (sp[0], sp[-1]):
                d.ellipse((x - ww / 2, y - ww / 2, x + ww / 2, y + ww / 2), fill=255)
        return mask(fn)

    cap = poly(_si_rr(36, 6, 64, 58, 14))
    paint(cap, light, base)
    lines = mask(lambda d: [d.line([(X(36), Y(y)), (X(64), Y(y))], fill=255, width=int(2.4 * k * S)) for y in (18, 26, 34, 42, 50)])
    paint(lines, deep, alpha=150, clip=cap)
    paint(poly(_si_rr(40, 11, 47, 40, 3.5)), (255, 255, 255), alpha=110)
    paint(stroke(_si_arc(50, 38, 24, 5, 175), 5.4), base)
    paint(stroke([(50, 62), (50, 82)], 5.4), base)
    paint(poly(_si_rr(33, 82, 67, 91, 4.5)), base)
    for r, al in ((33, 255), (45, 140)):                   # sound waves on both sides of the mic
        for a0, a1 in ((-36, 36), (144, 216)):
            paint(stroke(_si_arc(50, 40, r, a0, a1), 5.2, mapped=False), base, alpha=al)
    return out.resize((px, px), Image.LANCZOS)


# ───────────────  canvas (2x supersampled)  ────────────────
class Cv:
    def __init__(self, h: int):
        self.H = h
        self.img = Image.new("RGB", (W * SS, h * SS))
        self.d = ImageDraw.Draw(self.img)

    def k(self, v):
        return int(round(v * SS))

    # --- compositing
    def over(self, box, fn, pad=0, blur=0):
        """Draw on a transparent layer covering `box` (+pad), optionally blur it, alpha-composite onto the canvas."""
        X0, Y0, X1, Y1 = (self.k(box[0] - pad), self.k(box[1] - pad), self.k(box[2] + pad), self.k(box[3] + pad))
        layer = Image.new("RGBA", (max(X1 - X0, 1), max(Y1 - Y0, 1)), (0, 0, 0, 0))
        fn(ImageDraw.Draw(layer), lambda x: self.k(x) - X0, lambda y: self.k(y) - Y0)
        if blur:
            layer = layer.filter(ImageFilter.GaussianBlur(self.k(blur)))
        cx0, cy0, cx1, cy1 = max(0, X0), max(0, Y0), min(self.img.width, X1), min(self.img.height, Y1)
        if cx1 <= cx0 or cy1 <= cy0:
            return
        layer = layer.crop((cx0 - X0, cy0 - Y0, cx1 - X0, cy1 - Y0))
        region = self.img.crop((cx0, cy0, cx1, cy1)).convert("RGBA")
        region.alpha_composite(layer)
        self.img.paste(region.convert("RGB"), (cx0, cy0))

    def rrect(self, box, r, fill=None, outline=None, width=1, shadow=None):
        x0, y0, x1, y1 = box
        if shadow:
            col, blur, dy = shadow
            self.over(box, lambda dd, tx, ty: dd.rounded_rectangle(
                (tx(x0), ty(y0 + dy), tx(x1), ty(y1 + dy)), self.k(r), fill=col), pad=blur * 3, blur=blur)
        if fill is not None or outline is not None:
            self.over(box, lambda dd, tx, ty: dd.rounded_rectangle(
                (tx(x0), ty(y0), tx(x1) - 1, ty(y1) - 1), self.k(r), fill=fill, outline=outline,
                width=self.k(width) if outline else 0), pad=2)

    def glow(self, cx, cy, r, col, alpha):
        self.over((cx - r, cy - r, cx + r, cy + r), lambda dd, tx, ty: dd.ellipse(
            (tx(cx - r), ty(cy - r), tx(cx + r), ty(cy + r)), fill=tuple(col) + (alpha,)), pad=r, blur=r * .55)

    def tint(self, cx, cy, r, col, alpha):
        """Like glow(), but the blur keeps the colour pure (no dark fringe): for glows on LIGHT backgrounds."""
        X0, Y0, X1, Y1 = self.k(cx - 2 * r), self.k(cy - 2 * r), self.k(cx + 2 * r), self.k(cy + 2 * r)
        layer = Image.new("RGBA", (max(X1 - X0, 1), max(Y1 - Y0, 1)), tuple(col[:3]) + (0,))
        ImageDraw.Draw(layer).ellipse((self.k(cx - r) - X0, self.k(cy - r) - Y0, self.k(cx + r) - X0, self.k(cy + r) - Y0),
                                      fill=tuple(col[:3]) + (alpha,))
        layer.putalpha(layer.getchannel("A").filter(ImageFilter.GaussianBlur(self.k(r * .55))))
        cx0, cy0, cx1, cy1 = max(0, X0), max(0, Y0), min(self.img.width, X1), min(self.img.height, Y1)
        if cx1 <= cx0 or cy1 <= cy0:
            return
        layer = layer.crop((cx0 - X0, cy0 - Y0, cx1 - X0, cy1 - Y0))
        region = self.img.crop((cx0, cy0, cx1, cy1)).convert("RGBA")
        region.alpha_composite(layer)
        self.img.paste(region.convert("RGB"), (cx0, cy0))

    def line(self, p0, p1, col, width=1, alpha=255):
        x0, y0, x1, y1 = min(p0[0], p1[0]), min(p0[1], p1[1]), max(p0[0], p1[0]), max(p0[1], p1[1])
        self.over((x0, y0, x1, y1), lambda dd, tx, ty: dd.line(
            (tx(p0[0]), ty(p0[1]), tx(p1[0]), ty(p1[1])), fill=tuple(col) + (alpha,), width=self.k(width)), pad=width + 2)

    def circle(self, cx, cy, r, fill=None, outline=None, width=1):
        self.d.ellipse((self.k(cx - r), self.k(cy - r), self.k(cx + r), self.k(cy + r)), fill=fill, outline=outline,
                       width=self.k(width) if outline else 0)

    def gradient(self, c1, c2, diag=False, box=None, radius=0):
        x0, y0, x1, y1 = box or (0, 0, W, self.H)
        w, h = self.k(x1 - x0), self.k(y1 - y0)
        v = Image.linear_gradient("L").resize((w, h))
        if diag:
            v = ImageChops.add(v, Image.linear_gradient("L").rotate(90).resize((w, h)), scale=2)
        g = Image.composite(Image.new("RGB", (w, h), c2), Image.new("RGB", (w, h), c1), v)
        mask = None
        if radius:
            mask = Image.new("L", (w, h), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), self.k(radius), fill=255)
        self.img.paste(g, (self.k(x0), self.k(y0)), mask)

    # --- text
    def tw(self, s, size, bold=False, fam="sans", track=0):
        s, fam, track = _prep(s, fam, track)
        f = _font(self.k(size), bold, fam)
        return (self.d.textlength(s, font=f) + self.k(track) * max(len(s) - 1, 0)) / SS

    def text(self, xy, s, size, bold=False, fill=(255, 255, 255), anchor="la", fam="sans", track=0):
        s, fam, track = _prep(s, fam, track)
        f = _font(self.k(size), bold, fam)
        x, y = self.k(xy[0]), self.k(xy[1])
        if not track:
            self.d.text((x, y), s, font=f, fill=fill, anchor=anchor)
            return self.tw(s, size, bold, fam)
        total = self.d.textlength(s, font=f) + self.k(track) * (len(s) - 1)
        x -= {"l": 0, "m": total / 2, "r": total}[anchor[0]]
        for ch in s:
            self.d.text((x, y), ch, font=f, fill=fill, anchor="l" + anchor[1])
            x += self.d.textlength(ch, font=f) + self.k(track)
        return total / SS

    def fit(self, s, size, bold, fam, max_w, min_size):
        for sz in range(int(size), int(min_size) - 1, -1):
            if self.tw(s, sz, bold, fam) <= max_w:
                return s, sz
        while s and self.tw(s + "…", min_size, bold, fam) > max_w:
            s = s[:-1]
        return s + "…", min_size

    def wrap(self, s, size, bold, fam, max_w, max_lines=2):
        words, lines, cur = s.split(), [], ""
        for w_ in words:
            t = (cur + " " + w_).strip()
            if self.tw(t, size, bold, fam) <= max_w or not cur:
                cur = t
            else:
                lines.append(cur)
                cur = w_
        lines.append(cur)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = self.fit(lines[-1] + " …", size, bold, fam, max_w, size - 6)[0]
        return lines

    def logo(self, img, x, y, size, radius, bg=None):
        if img is None:
            return
        lg = img.convert("RGB").resize((self.k(size), self.k(size)), Image.LANCZOS)
        m = Image.new("L", lg.size, 0)
        ImageDraw.Draw(m).rounded_rectangle((0, 0, lg.width - 1, lg.height - 1), self.k(radius), fill=255)
        self.img.paste(lg, (self.k(x), self.k(y)), m)

    def meter(self, x, base, n, col, off, bw=5, step=8, h0=8, dh=6, glow=0):
        """Three rising rounded bars (a signal-strength glyph): the first `n` are lit in `col`, the rest in `off`."""
        for i in range(3):
            on = i < n
            if on and glow:
                self.glow(x + i * step + bw / 2, base - (h0 + i * dh) / 2, glow, col, 70)
            self.rrect((x + i * step, base - (h0 + i * dh), x + i * step + bw, base), bw / 2, fill=col if on else off)
        return 2 * step + bw

    def corner_glow(self, box, radius, col, alpha, gx, gy, gr):
        """A soft coloured light (centre gx,gy radius gr) that stays inside the rounded box."""
        x0, y0, x1, y1 = box
        w_, h_ = self.k(x1 - x0), self.k(y1 - y0)
        lay = Image.new("RGBA", (w_, h_), (0, 0, 0, 0))
        ImageDraw.Draw(lay).ellipse((self.k(gx - x0 - gr), self.k(gy - y0 - gr), self.k(gx - x0 + gr), self.k(gy - y0 + gr)),
                                    fill=tuple(col) + (alpha,))
        lay = lay.filter(ImageFilter.GaussianBlur(self.k(gr * .45)))
        m = Image.new("L", (w_, h_), 0)
        ImageDraw.Draw(m).rounded_rectangle((0, 0, w_ - 1, h_ - 1), self.k(radius), fill=255)
        lay.putalpha(ImageChops.multiply(lay.getchannel("A"), m))
        ox, oy = self.k(x0), self.k(y0)
        region = self.img.crop((ox, oy, ox + w_, oy + h_)).convert("RGBA")
        region.alpha_composite(lay)
        self.img.paste(region.convert("RGB"), (ox, oy))

    def speaker(self, cx, cy, size, col, alpha=255):
        """The speech icon (a mic in a glossy circle), tinted from `col`, centred on (cx, cy)."""
        px = max(self.k(size), 8)
        ic = _speech_icon(tuple(col[:3]), px)
        if alpha < 255:
            ic = ic.copy()
            ic.putalpha(ic.getchannel("A").point(lambda v: v * alpha // 255))
        self.img.paste(ic, (self.k(cx) - px // 2, self.k(cy) - px // 2), ic)

    def speech_badge(self, x, cy, kind, col, txt=(255, 255, 255), fill=None, outline=None, h=56, anchor="r",
                     fam="sans", radius=None, glow=True, imp=None):
        """Speech icon + 'SPEECH' / 'CONFERENCE', with NO pill/border/glow around it. `x` is its left / right edge / centre.
        (fill/outline/radius/glow are accepted only so older callers keep working.) The icon takes the news-impact colour."""
        label = "CONFERENCE" if kind == "conf" else "SPEECH"
        fs, isz, gap = h * (.40 if _is_fa() else .34), h * .86, h * .12
        wlab = self.tw(label, fs, True, fam, track=2)
        w_ = isz + gap + wlab
        x0 = {"l": x, "m": x - w_ / 2, "r": x - w_}[anchor]
        icol = IMPACT.get(imp) or IMPACT.get(str(imp).capitalize()) or col      # icon colour = impact of the news
        self.speaker(x0 + isz / 2, cy, isz, icol)
        self.text((x0 + isz + gap, cy), label, fs, True, txt, "lm", fam=fam, track=2)
        return w_

    def pill(self, x, y, w, h, fill, outline=None):
        self.rrect((x, y, x + w, y + h), h / 2, fill=fill, outline=outline)

    def done(self) -> Image.Image:
        return self.img.resize((W, self.H), Image.LANCZOS)


_SPEECH_RE = re.compile(r"\b(speaks?|speech|testif(?:ies|y)|testimony|remarks|address(?:es)?)\b", re.I)
_CONF_RE = re.compile(r"press conference|news conference|\bconference\b", re.I)


def _speech_kind(r):
    """'speech' / 'conf' for a talk-type event (a speech or a press conference) that has no forecast/previous/actual
    numbers to show, otherwise None. These rows get a speaker badge instead of empty dashes."""
    if any((r.get(k) or "").strip() for k in ("Forecast", "Previous", "Actual")):
        return None
    name = r.get("Event", "") or ""
    if _CONF_RE.search(name):
        return "conf"
    if _SPEECH_RE.search(name):
        return "speech"
    return None


def _row(r):
    return dict(time=r.get("Time", "") or "", cur=(r.get("Currency") or "")[:4], name=r.get("Event", "") or "",
                fc=(r.get("Forecast") or "").strip(), pv=(r.get("Previous") or "").strip(),
                act=(r.get("Actual") or "").strip(), imp=r.get("Impact", "High"))


# ═══════════════  1 · TIMELINE  ═══════════════
def _timeline(rows, title, logo, brand, label, bot):
    h, s, _ = _hls(brand)
    s = max(.5, min(s, .85))
    head, rh, foot = 236, 124, 100
    c = Cv(head + max(len(rows), 1) * rh + foot)
    c.gradient(_rgb(h, .55, .06), _rgb(h, .50, .11))
    c.glow(930, 90, 330, _rgb(h, .9, .5), 110)
    c.glow(40, c.H * .5, 300, _rgb(h + .10, .8, .45), 70)
    c.glow(820, c.H - 60, 280, _rgb(h - .08, .8, .45), 70)
    acc = _rgb(h, .9, .68)
    wd, mon, day = _date_parts(rows)
    fa = _is_fa()                                   # Persian: logo + title on the RIGHT, date block on the LEFT
    off = 118 if logo is not None else 0
    lx, (tx0, ta) = (W - 48 - 92, (W - 48 - off, "rm")) if fa else (48, (48 + off, "lm"))
    c.logo(logo, lx, 50, 92, 26)
    t, sz = c.fit(title, 46, True, "sans", 560, 32)
    c.text((tx0, 80), t, sz, True, (255, 255, 255), ta)
    c.text((tx0, 128), _dline(wd, mon, day, "sub"), 23, False,
           _rgb(h, .5, .82), ta)
    if day:
        dx0, da = (48, "lm") if fa else (W - 48, "rm")
        c.text((dx0, 84), _n(day), 92, True, (255, 255, 255), da)
        c.text((dx0, 148), _month_weekday(wd, mon), 20, True, acc, da, track=3)
    _chan(c, 48 if fa else W - 48, 192, "lm" if fa else "rm", _rgb(h, .5, .82))

    x_axis = 178
    ys = [head + i * rh + rh / 2 for i in range(len(rows))]
    if len(ys) > 1:
        c.line((x_axis, ys[0]), (x_axis, ys[-1]), acc, 3, 90)
    for i, (r, cy) in enumerate(zip(map(_row, rows), ys)):
        y = head + i * rh
        col = IMPACT.get(r["imp"], IMPACT["High"])
        c.text((140, cy), r["time"], 30, True, (255, 255, 255), "rm")
        c.glow(x_axis, cy, 26, col, 120)
        c.circle(x_axis, cy, 14, fill=_rgb(h, .5, .08), outline=col, width=4)
        c.circle(x_axis, cy, 5, fill=col)
        box = (224, y + 8, W - 48, y + rh - 8)
        c.rrect(box, 26, fill=(255, 255, 255, 22), outline=(255, 255, 255, 44), shadow=((0, 0, 0, 80), 14, 8))
        cw = c.tw(r["cur"], 20, True) + 28
        dy = 0 if (r["fc"] or r["pv"]) else rh / 2 - 45          # no forecast/previous line -> centre the row
        c.pill(248, y + 27 + dy, cw, 36, acc + (46,))
        c.text((248 + cw / 2, y + 45 + dy), r["cur"], 20, True, acc, "mm")
        name, nsz = c.fit(r["name"], 29, True, "sans", W - 48 - 190 - (262 + cw), 21)
        c.text((262 + cw, y + 45 + dy), name, nsz, True, (255, 255, 255), "lm")
        cx = 250
        if r["fc"] or r["pv"]:
            for lab, val in (("FORECAST", r["fc"] or "–"), ("PREVIOUS", r["pv"] or "–")):
                cx += c.text((cx, y + 84), lab, 14, True, _rgb(h, .35, .68), "lm", track=1.5) + 8
                cx += c.text((cx, y + 84), val, 21, True, (235, 240, 250), "lm") + 26
        sk = _speech_kind(rows[i])
        if sk:
            c.speech_badge(W - 74, y + rh / 2, sk, acc, h=60, imp=rows[i].get("Impact"))
        else:
            tone = _tone(rows[i], True)
            c.text((W - 74, y + 36), "ACTUAL", 13, True, _rgb(h, .35, .68), "rm", track=2)
            if r["act"]:
                c.text((W - 74, y + 70), r["act"], 36, True, tone or (255, 255, 255), "rm")
            else:
                c.text((W - 74, y + 70), "—", 32, True, _rgb(h, .25, .50), "rm")
    fy = c.H - foot
    c.line((48, fy + 12), (W - 48, fy + 12), (255, 255, 255), 1, 40)
    c.text((48, fy + 52), _footer(bot), 19, False, _rgb(h, .4, .74), "lm")
    return c.done()


# ═══════════════  2 · BENTO  ═══════════════
def _bento(rows, title, logo, brand, label, bot):
    h, s, _ = _hls(brand)
    s = max(.5, min(s, .85))
    pad, gap, th, hero_h = 40, 16, 200, 262
    tw_ = (W - 2 * pad - gap) / 2
    n = len(rows)
    nrows = (n + 1) // 2
    c = Cv(pad + hero_h + gap + max(nrows, 1) * (th + gap) + 70)
    c.gradient(_rgb(h, .35, .955), _rgb(h + .03, .35, .91))
    # hero
    hb = (pad, pad, W - pad, pad + hero_h)
    c.rrect(hb, 44, shadow=(_rgb(h, .7, .30) + (90,), 22, 14))
    c.gradient(_rgb(h, s, .46), _rgb(h + .07, s, .30), diag=True, box=hb, radius=44)
    c.over(hb, lambda dd, tx, ty: [dd.ellipse((tx(W - 360 + i * 70), ty(pad - 150 + i * 30), tx(W + 60 + i * 70 - 120),
                                                 ty(pad + 250 + i * 30 - 150)), outline=(255, 255, 255, 34), width=c.k(2))
                                   for i in range(3)], pad=0)
    wd, mon, day = _date_parts(rows)
    fa = _is_fa()
    plate = 148 if logo is not None else 36
    if fa:      # Persian: title beside the logo (right), big day at the right edge, weekday/month to its left
        c.text((W - pad - plate, pad + 76), title, 26, True, (255, 255, 255), "rm")
        dn = _n(day or "")
        c.text((W - pad - 36, pad + 238), dn, 150, True, (255, 255, 255), "rs")
        dx = W - pad - 36 - c.tw(dn, 150, True) - 24
        c.text((dx, pad + 176), _wd_name(wd), 44, True, (255, 255, 255), "rs")
        c.text((dx, pad + 220), _mon_name(mon), 34, False, _rgb(h, .5, .88), "rs")
        _chan(c, pad + 36, pad + 76, "lm", (255, 255, 255), 24)
    else:
        c.text((pad + 36, pad + 40), title.upper(), 19, True, (255, 255, 255), "lm", track=3)
        c.text((pad + 30, pad + 190), _n(day or ""), 168, True, (255, 255, 255), "ls")
        dx = pad + 36 + c.tw(_n(day or ""), 168, True) + 24
        c.text((dx, pad + 128), _wd_name(wd), 44, True, (255, 255, 255), "ls")
        c.text((dx, pad + 172), _mon_name(mon), 34, False, _rgb(h, .5, .88), "ls")
        _chan(c, W - pad - plate, pad + 76, "rm", (255, 255, 255), 24)
    if logo is not None:
        c.rrect((W - pad - 128, pad + 28, W - pad - 32, pad + 124), 28, fill=(255, 255, 255, 255))
        c.logo(logo, W - pad - 122, pad + 34, 84, 22)
    hi = sum(1 for r in rows if r.get("Impact") == "High")
    for i, txt in enumerate((f"{hi} high impact",)):
        pw = c.tw(txt, 20, True) + 36
        xr = (pad + 36 + pw) if fa else W - pad - 36 - sum(c.tw(t, 20, True) + 36 + 10 for t in (f"{hi} high impact",)[:i])
        c.pill(xr - pw, pad + hero_h - 76, pw, 42, (255, 255, 255, 40), (255, 255, 255, 80))
        c.text((xr - pw / 2, pad + hero_h - 55), txt, 20, True, (255, 255, 255), "mm")
    ink, mut = _rgb(h, .5, .12), _rgb(h, .15, .48)
    y0 = pad + hero_h + gap
    for i, r0 in enumerate(rows):
        r = _row(r0)
        x, y = pad + (i % 2) * (tw_ + gap), y0 + (i // 2) * (th + gap)
        c.rrect((x, y, x + tw_, y + th), 32, fill=(255, 255, 255, 255), shadow=(_rgb(h, .5, .35) + (46,), 18, 10))
        cw = c.tw(r["cur"], 21, True) + 30
        c.pill(x + 24, y + 22, cw, 38, _rgb(h, .7, .93))
        c.text((x + 24 + cw / 2, y + 41), r["cur"], 21, True, _rgb(h, .75, .32), "mm")
        icol = IMPACT.get(r["imp"], IMPACT["High"])
        ilab = r["imp"] if r["imp"] in IMPACT else "High"
        c.corner_glow((x, y, x + tw_, y + th), 32, icol, 52, x + tw_ - 30, y + 20, 125)      # tinted light in the corner
        c.rrect((x, y, x + tw_, y + th), 32, outline=tuple(icol) + (46,), width=1.5)          # faint tinted edge
        chw = 12 + 21 + 9 + c.tw(ilab, 17, True) + 14
        c.pill(x + 24 + cw + 10, y + 24, chw, 34, tuple(icol) + (34,))
        c.meter(x + 24 + cw + 10 + 12, y + 41 + 10, {"High": 3, "Medium": 2, "Low": 1}.get(ilab, 3), icol,
                _mix(icol, (255, 255, 255), .72), glow=0)
        c.text((x + 24 + cw + 10 + 12 + 21 + 9, y + 41), ilab, 17, True, _mix(icol, (0, 0, 0), .30), "lm")
        c.text((x + tw_ - 26, y + 41), r["time"], 32, True, ink, "rm")
        lines = c.wrap(r["name"], 29, True, "sans", tw_ - 52, 2)
        sz = 29 if len(lines) == 1 else 26
        for j, ln in enumerate(lines):
            c.text((x + 26, y + 86 + j * 33), ln, sz, True, ink, "lm")
        by = y + th - 34
        cx = x + 26
        for lab, val in (("F", r["fc"]), ("P", r["pv"])):
            if val:
                lw = c.text((cx, by), lab, 15, True, mut, "lm") + 6
                cx += lw + c.text((cx + lw, by), val, 22, True, ink, "lm") + 38
        sk = _speech_kind(r0)
        if sk:
            c.speech_badge(x + 26, by, sk, _rgb(h, .8, .42), txt=_rgb(h, .75, .28), fill=_rgb(h, .75, .94),
                           outline=_rgb(h, .6, .82), h=50, anchor="l", glow=False, imp=r0.get("Impact"))
        tone = _tone(r0, False)
        if r["act"]:
            aw = c.tw(r["act"], 24, True) + 34
            col = tone or mut
            c.pill(x + tw_ - 26 - aw, by - 20, aw, 40, tuple(col) + (36,))
            c.text((x + tw_ - 26 - aw / 2, by), r["act"], 24, True, col, "mm")
    if n % 2:
        x, y = pad + tw_ + gap, y0 + (n // 2) * (th + gap)
        c.rrect((x, y, x + tw_, y + th), 32, fill=_rgb(h, .6, .90))
        c.text((x + 28, y + 62), "Source", 17, True, _rgb(h, .5, .40), "lm", track=3)
        c.text((x + 28, y + 100), "ForexFactory", 30, True, ink, "lm")
        c.text((x + 28, y + 150), f"powered by: {_bn(bot)}", 22, True, _rgb(h, .75, .34), "lm")
    c.text((pad + 8, c.H - 36), _footer(bot), 19, False, mut, "lm")
    return c.done()


# ═══════════════  3 · EDITORIAL  ═══════════════
def _editorial(rows, title, logo, brand, label, bot):
    h, s, _ = _hls(brand)
    s = max(.5, min(s, .85))
    paper = _rgb(h, .14, .965)
    ink, mut, rule = _rgb(h, .45, .09), _rgb(h, .12, .46), _rgb(h, .15, .80)
    acc = _rgb(h, s, .42)
    px, rh, head, foot = 64, 112, 336, 96
    c = Cv(head + max(len(rows), 1) * rh + foot)
    c.gradient(paper, _rgb(h, .14, .935))
    wd, mon, day = _date_parts(rows)
    c.text((W + 40, -70), _n(day or ""), 640, True, _mix(acc, paper, .93), "ra", fam="serif")
    c.logo(logo, px, 52, 60, 14)
    c.text((px + (80 if logo is not None else 0), 82), "ECONOMIC CALENDAR", 18, True, acc, "lm", track=4)
    lines = c.wrap(title, 64, True, "serif", 700, 2)
    for i, ln in enumerate(lines):
        c.text((px, 170 + i * 70), ln, 64, True, ink, "lm", fam="serif")
    ly = 170 + len(lines) * 70 + 8
    c.line((px, ly), (W - px, ly), ink, 3)
    c.text((px, ly + 30), _date_only(wd, mon, day), 19, True,
           ink, "lm", track=3)
    y0 = ly + 56
    for i, r0 in enumerate(rows):
        r = _row(r0)
        y = y0 + i * rh
        c.line((px, y), (W - px, y), rule, 1)
        cy = y + rh / 2
        c.text((px, cy - 12), r["time"], 46, True, ink, "lm", fam="serif")
        c.text((px, cy + 32), r["cur"], 19, True, acc, "lm", track=3)
        name, sz = c.fit(r["name"], 31, True, "sans", 420, 22)
        nx = 262
        if r["imp"] == "High":
            wname = c.tw(name, sz, True)
            c.rrect((nx - 8, cy - 12, nx + wname + 8, cy + 8), 3, fill=_rgb(h, .95, .72) + (140,))
        c.text((nx, cy - 8), name, sz, True, ink, "lm")
        for j, (lab, val) in enumerate((("FORECAST", r["fc"]), ("PREVIOUS", r["pv"]))):
            c.text((700, cy - 16 + j * 32), lab, 13, True, mut, "lm", track=1.5)
            c.text((836, cy - 16 + j * 32), val or "–", 22, True, ink if val else rule, "rm")
        tone = _tone(r0, False)
        c.text((W - px, cy - 30), "ACTUAL", 13, True, mut, "rm", track=2)
        c.text((W - px, cy + 10), r["act"] or "—", 44, True, tone or (ink if r["act"] else rule), "rm", fam="serif")
    ey = y0 + len(rows) * rh
    c.line((px, ey), (W - px, ey), ink, 3)
    c.text((px, ey + 44), _footer(bot), 19, False, mut, "lm")
    return c.done()


# ═══════════════  4 · HUD  ═══════════════
def _hud(rows, title, logo, brand, label, bot):
    h, s, _ = _hls(brand)
    acc = _rgb(h, .95, .60)
    bg = _rgb(h, .55, .15)
    mut = _rgb(h, .25, .55)
    head, rh, gap, foot = 232, 92, 10, 110
    c = Cv(head + max(len(rows), 1) * (rh + gap) + foot)
    c.gradient(_rgb(h, .58, .20), _rgb(h + .05, .62, .11), diag=True)
    c.glow(140, 90, 420, acc, 80)
    c.glow(W - 80, c.H - 80, 380, _rgb(h + .15, .9, .5), 70)
    grid = _mix(acc, bg, .90)
    for gx in range(0, W, 54):
        c.d.line((c.k(gx), 0, c.k(gx), c.k(c.H)), fill=grid, width=c.k(1))
    for gy in range(0, c.H, 54):
        c.d.line((0, c.k(gy), c.k(W), c.k(gy)), fill=grid, width=c.k(1))
    for (bx, by, sx, sy) in ((26, 26, 1, 1), (W - 26, 26, -1, 1), (26, c.H - 26, 1, -1), (W - 26, c.H - 26, -1, -1)):
        c.d.line((c.k(bx), c.k(by), c.k(bx + 54 * sx), c.k(by)), fill=acc, width=c.k(4))
        c.d.line((c.k(bx), c.k(by), c.k(bx), c.k(by + 54 * sy)), fill=acc, width=c.k(4))
    px = 70
    fa = _is_fa()                                   # Persian: logo + title RIGHT, date LEFT
    c.logo(logo, (W - px - 70) if fa else px, 60, 70, 16)
    lo = 90 if logo is not None else 0
    t, sz = c.fit(title.upper(), 36, True, "mono", 640, 22)
    c.text(((W - px - lo) if fa else (px + lo), 95), t, sz, True, (240, 248, 255), "rm" if fa else "lm", fam="mono")
    wd, mon, day = _date_parts(rows)
    c.text((px if fa else W - px, 95), _hud_date(wd, mon, day), 26, True, acc, "lm" if fa else "rm", fam="mono")
    _chan(c, (W - px - lo) if fa else (px + lo), 138, "rm" if fa else "lm", _rgb(h, .5, .78), 20, "mono")
    c.line((px, 166), (W - px, 166), acc, 2, 150)
    labs = (("TIME", px + 22, "l"), ("CUR", 214, "l"), ("EVENT", 300, "l"), ("IMPACT", 628, "l"),
            ("FCST", 790, "m"), ("PREV", 872, "m"), ("ACT", 950, "m"))
    for tx_, x, a in labs:
        c.text((x, 200), tx_, 14, True, mut, a + "m", fam="mono", track=1)
    y = head
    for r0 in rows:
        r = _row(r0)
        col = IMPACT.get(r["imp"], IMPACT["High"])
        c.rrect((px, y, W - px, y + rh), 8, fill=acc + (16,), outline=acc + (40,))
        c.rrect((px, y, px + 6, y + rh), 3, fill=col)
        cy = y + rh / 2
        c.text((px + 22, cy), r["time"], 29, True, acc, "lm", fam="mono")
        c.text((214, cy), f"[{r['cur']}]", 21, True, (240, 248, 255), "lm", fam="mono")
        nm, nsz = c.fit(r["name"].upper(), 20, True, "mono", 310, 13)
        c.text((300, cy), nm, nsz, True, (240, 248, 255), "lm", fam="mono")
        lvl = {"High": 3, "Medium": 2, "Low": 1}.get(r["imp"], 3)
        for b in range(3):
            on = b < lvl
            c.rrect((628 + b * 24, cy - 14, 628 + b * 24 + 18, cy + 14), 3, fill=col if on else _mix(col, bg, .85))
            if on:
                c.glow(628 + b * 24 + 9, cy, 18, col, 70)
        sk = _speech_kind(r0)
        if sk:
            c.speech_badge(990, cy, sk, acc, txt=(240, 248, 255), fill=acc + (26,), outline=acc + (210,), h=52, anchor="r",
                           fam="mono", radius=8, imp=r0.get("Impact"))
        else:
            for val, cx in ((r["fc"], 790), (r["pv"], 872)):
                c.text((cx, cy), val or "--", 23, True, (230, 240, 250) if val else _mix(mut, bg, .5), "mm", fam="mono")
            tone = _tone(r0, True)
            if r["act"]:
                aw = c.tw(r["act"], 23, True, "mono") + 26
                cc = tone or acc
                c.rrect((950 - aw / 2, cy - 22, 950 + aw / 2, cy + 22), 8, fill=tuple(cc) + (34,), outline=tuple(cc) + (200,),
                        width=2)
                c.text((950, cy), r["act"], 23, True, cc, "mm", fam="mono")
            else:
                c.text((950, cy), "--", 23, True, _mix(mut, bg, .5), "mm", fam="mono")
        y += rh + gap
    c.text((px, c.H - 62), f"> {_footer(bot).upper()}", 16, False, mut, "lm", fam="mono")
    c.rrect((W - px - 16, c.H - 72, W - px, c.H - 52), 2, fill=acc)
    return c.done()


# ═══════════════  5 · TICKET  ═══════════════
def _ticket(rows, title, logo, brand, label, bot):
    h, s, _ = _hls(brand)
    s = max(.5, min(s, .85))
    head, th, gap, foot = 230, 116, 28, 84
    c = Cv(head + max(len(rows), 1) * (th + gap) + foot)
    c.gradient(_rgb(h, .5, .12), _rgb(h + .05, .5, .06), diag=True)
    c.glow(900, 60, 380, _rgb(h, .9, .45), 80)
    c.glow(100, c.H - 40, 320, _rgb(h + .1, .8, .4), 60)
    acc = _rgb(h, .9, .70)
    paper, ink, mut = _rgb(h, .30, .965), _rgb(h, .5, .10), _rgb(h, .12, .48)
    wd, mon, day = _date_parts(rows)
    fa = _is_fa()                                   # Persian: logo + texts RIGHT, impact key LEFT
    lo = 110 if logo is not None else 0
    tx0, ta = (W - 48 - lo, "rm") if fa else (48 + lo, "lm")
    c.logo(logo, (W - 48 - 84) if fa else 48, 50, 84, 22)
    c.text((tx0, 66), "ECONOMIC CALENDAR  ·  DAILY PASS", 17, True, acc, ta, track=3)
    t, sz = c.fit(title, 46, True, "sans", 640, 30)
    c.text((tx0, 112), t, sz, True, (255, 255, 255), ta)
    c.text((tx0, 160), _dline(wd, mon, day, "plain", long=True),
           23, False, _rgb(h, .5, .80), ta)
    _chan(c, 48 if fa else W - 48, 92, "lm" if fa else "rm", acc, 24)
    # impact key: signal bars + colour, so the stub colours below are self-explanatory
    def _bars(bx, base, n, lit_col, off_col, bw=6, step=9, h0=10, dh=8):
        for i in range(3):
            c.rrect((bx + i * step, base - (h0 + i * dh), bx + i * step + bw, base), 2.5, fill=lit_col if i < n else off_col)
    kx = W - 48
    seq = ([("IMPACT", 0), ("High", 3), ("Medium", 2), ("Low", 1)] if _is_fa()        # Persian reads right -> left
           else [("Low", 1), ("Medium", 2), ("High", 3), ("IMPACT", 0)])               # English: IMPACT High Medium Low
    if fa:      # the key sits on the LEFT edge (the right side belongs to the logo + title)
        kx = 48 - 30 + sum((c.tw(lab, 15, True, track=3) + 30) if n == 0 else (c.tw(lab, 19, True) + 64) for lab, n in seq)
    for lab, n in seq:
        if n == 0:
            kx -= c.tw(lab, 15, True, track=3)
            c.text((kx, 160), lab, 15, True, _rgb(h, .4, .62), "lm", track=3)
            kx -= 30
        else:
            kx -= c.tw(lab, 19, True)
            c.text((kx, 160), lab, 19, True, _rgb(h, .35, .86), "lm")
            kx -= 10 + 24
            _bars(kx, 171, n, IMPACT[lab], _rgb(h, .3, .30), bw=5, step=8, h0=8, dh=6)
            kx -= 30
    y = head
    for r0 in rows:
        r = _row(r0)
        x0, x1, sx = 48, W - 48, 272
        bgp = lambda px_, py_: c.img.getpixel((c.k(px_), c.k(py_)))  # noqa: E731
        c.rrect((x0, y, x1, y + th), 22, fill=paper, shadow=((0, 0, 0, 120), 16, 10))
        # impact "class" stub: coloured edge band + a tint over the whole stub (like a boarding-pass class colour)
        col = IMPACT.get(r["imp"], IMPACT[
            "High"])
        lit = {"High": 3, "Medium": 2, "Low": 1}.get(r["imp"], 3)
        c.over((x0, y, sx, y + th), lambda dd, tx, ty: (
            dd.rounded_rectangle((tx(x0), ty(y), tx(sx), ty(y + th)), c.k(22), fill=tuple(col) + (255,), corners=(True, False, False, True)),
            dd.rounded_rectangle((tx(x0 + 10), ty(y), tx(sx) + c.k(30), ty(y + th)), c.k(22), fill=tuple(paper) + (255,), corners=(True, False, False, True)),
            dd.rounded_rectangle((tx(x0 + 10), ty(y), tx(sx), ty(y + th)), c.k(22), fill=tuple(col) + (30,), corners=(True, False, False, True))))
        for nx_, ny_, rad in ((sx, y, 12), (sx, y + th, 12), (x0, y + th / 2, 12), (x1, y + th / 2, 12)):
            c.circle(nx_, ny_, rad, fill=bgp(nx_, y - 24 if ny_ == y else (y + th + 22 if ny_ == y + th else y - 24)))
        for dy in range(24, th - 14, 14):
            c.rrect((sx - 1.5, y + dy, sx + 1.5, y + dy + 7), 1.5, fill=_rgb(h, .2, .78))
        c.text(((x0 + sx) / 2 - 8, y + 44), r["time"], 40, True, ink, "mm")
        for i in range(3):                                   # signal bars = impact (3 lit = high)
            c.rrect((233 + i * 9, y + 58 - (11 + i * 8), 233 + i * 9 + 6, y + 58), 2.5, fill=col if i < lit else _rgb(h, .15, .82))
        cw = c.tw(r["cur"], 19, True) + 30
        c.pill((x0 + sx) / 2 - cw / 2 - 8, y + 74, cw, 30, _rgb(h, s, .40))
        c.text(((x0 + sx) / 2 - 8, y + 89), r["cur"], 19, True, (255, 255, 255), "mm")
        name, nsz = c.fit(r["name"], 29, True, "sans", 345, 21)
        c.text((sx + 34, y + th / 2), name, nsz, True, ink, "lm")
        sk = _speech_kind(r0)
        if sk:
            tcol = _rgb(h, s, .40)
            c.speech_badge(990, y + th / 2, sk, tcol, txt=tcol, fill=tcol + (28,), outline=tcol + (170,), h=58, anchor="r",
                           glow=False, imp=r0.get("Impact"))
        else:
            for val, lab, cx in ((r["fc"], "FCST", 722), (r["pv"], "PREV", 806)):
                c.text((cx, y + 40), lab, 13, True, mut, "mm", track=2)
                c.text((cx, y + 72), val or "–", 24, True, ink if val else _rgb(h, .15, .75), "mm")
            tone = _tone(r0, False)
            cc = tone or (ink if r["act"] else _rgb(h, .15, .70))
            if r["act"]:
                sw = max(c.tw(r["act"], 28, True) + 40, 120)
                layer = Image.new("RGBA", (c.k(sw + 20), c.k(84)), (0, 0, 0, 0))
                dd = ImageDraw.Draw(layer)
                dd.rounded_rectangle((c.k(10), c.k(8), c.k(sw + 10), c.k(76)), c.k(14), outline=tuple(cc) + (230,), width=c.k(3),
                                     fill=tuple(cc) + (24,))
                dd.text((c.k((sw + 20) / 2), c.k(48)), r["act"], font=_font(c.k(28), True), fill=tuple(cc) + (255,), anchor="mm")
                _lab, _fam, _ = _prep("ACTUAL", "sans", 0)
                dd.text((c.k((sw + 20) / 2), c.k(22)), _lab, font=_font(c.k(12), True, _fam), fill=tuple(cc) + (200,), anchor="mm")
                layer = layer.rotate(7, resample=Image.BICUBIC, expand=True)
                cx_, cy_ = 940, y + th / 2
                region_box = (cx_ - layer.width / 2 / SS, cy_ - layer.height / 2 / SS, cx_ + layer.width / 2 / SS,
                              cy_ + layer.height / 2 / SS)
                c.over(region_box, lambda d_, tx, ty: d_._image.paste(layer, (tx(region_box[0]), ty(region_box[1])), layer))
            else:
                c.text((940, y + th / 2), "—", 30, True, _rgb(h, .15, .70), "mm")
        y += th + gap
    c.text((48, c.H - 36), _footer(bot), 18, False, _rgb(h, .4, .75), "lm")
    return c.done()


# ═══════════════  6 · SPOTLIGHT  ═══════════════
def _spotlight(rows, title, logo, brand, label, bot):
    h, s, _ = _hls(brand)
    s = max(.55, min(s, .9))
    hero_h, rh = 396, 80
    rest = rows[1:]
    head = 150
    c = Cv(head + hero_h + 24 + (len(rest) * rh + 84 if rest else 0) + 90)
    c.gradient(_rgb(h, .78, .27), _rgb(h + .15, .80, .46), diag=True)
    c.glow(900, 120, 380, (255, 255, 255), 52)
    c.glow(80, c.H - 100, 360, _rgb(h - .05, .9, .12), 120)
    for i in range(4):
        c.d.ellipse((c.k(W - 300 + i * 46 - 160), c.k(-160 + i * 46), c.k(W - 300 + i * 46 + 300), c.k(300 + i * 46 - 160)),
                    outline=_mix(_rgb(h, .7, .4), (255, 255, 255), .12 + i * .02), width=c.k(2))
    wd, mon, day = _date_parts(rows)
    fa = _is_fa()                                   # Persian: logo + title on the RIGHT
    lo = 92 if logo is not None else 0
    tx0, ta = (W - 48 - lo, "rm") if fa else (48 + lo, "lm")
    c.logo(logo, (W - 48 - 72) if fa else 48, 44, 72, 20)
    t, sz = c.fit(title, 36, True, "sans", 560, 26)
    c.text((tx0, 66), t, sz, True, (255, 255, 255), ta)
    c.text((tx0, 104), _dline(wd, mon, day, "sub"), 21, False,
           (255, 255, 255), ta)
    _chan(c, 48 if fa else W - 48, 84, "lm" if fa else "rm", (255, 255, 255), 24)
    if rows:
        r0 = rows[0]
        r = _row(r0)
        hb = (48, head, W - 48, head + hero_h)
        c.rrect(hb, 44, fill=(255, 255, 255, 36), outline=(255, 255, 255, 90), shadow=((0, 0, 0, 90), 24, 16))
        c.pill(88, head + 34, c.tw("SPOTLIGHT", 17, True, track=3) + 40, 40, (255, 255, 255, 255))
        c.text((88 + (c.tw("SPOTLIGHT", 17, True, track=3) + 40) / 2, head + 54), "SPOTLIGHT", 17, True, _rgb(h, .8, .25),
               "mm", track=3)
        cw = c.tw(r["cur"], 20, True) + 34
        cx0 = 88 + c.tw("SPOTLIGHT", 17, True, track=3) + 40 + 14
        c.pill(cx0, head + 34, cw, 40, (255, 255, 255, 60), (255, 255, 255, 110))
        c.text((cx0 + cw / 2, head + 54), r["cur"], 20, True, (255, 255, 255), "mm")
        icol = IMPACT.get(r["imp"], IMPACT["High"])
        ilab = (r["imp"] if r["imp"] in IMPACT else "High") + " impact"
        c.corner_glow(hb, 44, icol, 120, W - 150, head + 30, 260)                      # the hero is lit in the impact colour
        c.rrect((hb[0], hb[1], hb[2], hb[3]), 44, outline=tuple(icol) + (120,), width=2)
        ix0 = cx0 + cw + 14
        chw = 16 + 21 + 10 + c.tw(ilab, 17, True) + 18
        c.pill(ix0, head + 34, chw, 40, (255, 255, 255, 40), tuple(icol) + (200,))
        c.meter(ix0 + 16, head + 54 + 10, {"High": 3, "Medium": 2, "Low": 1}.get(r["imp"], 3), icol, (255, 255, 255, 70), glow=16)
        c.text((ix0 + 16 + 21 + 10, head + 54), ilab, 17, True, (255, 255, 255), "lm")
        c.text((84, head + 150), r["time"], 150, True, (255, 255, 255), "lm")
        lines = c.wrap(r["name"], 46, True, "sans", W - 200, 2)
        for i, ln in enumerate(lines):
            c.text((88, head + 262 + i * 50), ln, 46 if len(lines) == 1 else 40, True, (255, 255, 255), "lm")
        sy = head + hero_h - 52
        tone = _tone(r0, True)
        sk = _speech_kind(r0)
        if sk:
            c.speech_badge(88, sy - 8, sk, (255, 255, 255), fill=(255, 255, 255, 46), outline=(255, 255, 255, 140), h=86,
                           anchor="l", imp=r0.get("Impact"))
        else:
            for j, (lab, val) in enumerate((("FORECAST", r["fc"]), ("PREVIOUS", r["pv"]), ("ACTUAL", r["act"]))):
                x = 88 + j * 270
                if lab == "ACTUAL" and val:
                    vw = c.tw(val, 34, True) + 36
                    c.pill(x - 14, sy - 16, vw + 40, 54, (255, 255, 255, 46))
                c.text((x, sy - 36), lab, 14, True, (255, 255, 255), "lm", track=2)
                c.text((x, sy + 8), val or "—", 36, True, (tone if lab == "ACTUAL" and tone else (255, 255, 255)), "lm")
    if rest:
        py = head + hero_h + 24
        pb = (48, py, W - 48, py + len(rest) * rh + 84)
        c.rrect(pb, 36, fill=(0, 0, 0, 56), outline=(255, 255, 255, 40))
        for lab, cx in (("FORECAST", 730), ("PREVIOUS", 840), ("ACTUAL", 955)):
            c.text((cx, py + 38), lab, 13, True, (255, 255, 255), "mm", track=2)
        for i, r0 in enumerate(rest):
            r = _row(r0)
            ry = py + 64 + i * rh
            if i:
                c.line((84, ry), (W - 84, ry), (255, 255, 255), 1, 40)
            cy = ry + rh / 2
            c.text((86, cy), r["time"], 27, True, (255, 255, 255), "lm")
            cw = c.tw(r["cur"], 18, True) + 26
            c.pill(194, cy - 16, cw, 32, (255, 255, 255, 50))
            c.text((194 + cw / 2, cy), r["cur"], 18, True, (255, 255, 255), "mm")
            icol = IMPACT.get(r["imp"], IMPACT["High"])
            c.glow(66, cy, 30, icol, 90)
            c.rrect((62, cy - 20, 69, cy + 20), 3.5, fill=icol)
            c.meter(194 + cw + 12, cy + 10, {"High": 3, "Medium": 2, "Low": 1}.get(r["imp"], 3), icol, (255, 255, 255, 60), glow=0)
            nm, nsz = c.fit(r["name"], 25, True, "sans", 640 - (194 + cw + 52), 17)
            c.text((194 + cw + 46, cy), nm, nsz, True, (255, 255, 255), "lm")
            sk = _speech_kind(r0)
            if sk:
                c.speech_badge(1000, cy, sk, (255, 255, 255), fill=(255, 255, 255, 40), outline=(255, 255, 255, 120), h=48,
                               anchor="r", glow=False, imp=r0.get("Impact"))
            else:
                tone = _tone(r0, True)
                for val, cx in ((r["fc"], 730), (r["pv"], 840)):
                    c.text((cx, cy), val or "–", 23, True, (255, 255, 255) if val else (210, 220, 240), "mm")
                c.text((955, cy), r["act"] or "—", 25, True, tone or ((255, 255, 255) if r["act"] else (210, 220, 240)), "mm")
    c.text((48, c.H - 42), _footer(bot), 19, False, (255, 255, 255), "lm")
    return c.done()


# ═══════════════  7 · LEDGER  ═══════════════
def _clock_min(t: str):
    """'14:30' -> 870 (minutes since midnight); None for 'All Day', 'Tentative', ..."""
    m = re.match(r"\s*(\d{1,2}):(\d{2})\s*$", t or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _ledger(rows, title, logo, brand, label, bot):
    h, s, _ = _hls(brand)
    s = max(.5, min(s, .85))
    n = len(rows)
    rh, band, pulse_y, pulse_h, tbl_y, th_h = 62, 214, 238, 84, 346, 56
    foot = 96
    c = Cv(tbl_y + th_h + max(n, 1) * rh + 24 + foot)
    ink, mut, rule = _rgb(h, .45, .12), _rgb(h, .14, .50), _rgb(h, .20, .91)
    deep = _rgb(h, s, .30)
    c.gradient(_rgb(h, .30, .955), _rgb(h, .28, .925))

    # ── header band: logo, title, date, impact counts ──
    c.gradient(_rgb(h, s, .30), _rgb(h + .05, s, .20), diag=True, box=(0, -90, W, band), radius=54)
    c.over((0, 0, W, band), lambda dd, tx, ty: [dd.ellipse((tx(W - 250 + i * 54), ty(-190 + i * 20), tx(W + 190 + i * 54),
                                                            ty(250 + i * 20)), outline=(255, 255, 255, 22), width=c.k(2))
                                              for i in range(4)])
    wd, mon, day = _date_parts(rows)
    fa = _is_fa()                                   # Persian: logo + title RIGHT, impact counts LEFT (High first from the right)
    bx = (W - 44 - 84) if fa else 44                # logo plate x
    lo = 108 if logo is not None else 0
    tx0, ta = (W - 44 - lo, "rm") if fa else (44 + lo, "lm")
    if logo is not None:
        c.rrect((bx, 46, bx + 84, 130), 24, fill=(255, 255, 255, 255))
        c.logo(logo, bx + 6, 52, 72, 18)
    t, sz = c.fit(title, 42, True, "sans", 470, 28)
    c.text((tx0, 62), "ECONOMIC CALENDAR", 15, True, _rgb(h, .9, .78), ta, track=3)
    c.text((tx0, 98), t, sz, True, (255, 255, 255), ta)
    c.text((tx0, 138), _dline(wd, mon, day, "sub"), 21, False, _rgb(h, .5, .86), ta)
    _chan(c, tx0, 176, ta, _rgb(h, .9, .78), 22)
    cnt = {k: sum(1 for r in rows if r.get("Impact") == k) for k in ("High", "Medium", "Low")}
    cx0, sw = (44 if fa else W - 44 - 3 * 124), 124
    c.rrect((cx0, 44, cx0 + 3 * sw, 140), 30, fill=(255, 255, 255, 26), outline=(255, 255, 255, 64))
    for i, k in enumerate(("High", "Medium", "Low")):
        mx = cx0 + sw * ((2 - i if fa else i) + .5)
        if i:
            lx_ = cx0 + sw * (3 - i if fa else i)
            c.line((lx_, 64), (lx_, 120), (255, 255, 255), 1, 60)
        c.text((mx, 80), _n(cnt[k]), 40, True, (255, 255, 255), "mm")
        lw = c.tw(k, 15, True)
        if fa:
            c.circle(mx + lw / 2 + 9, 117, 5, fill=IMPACT[k])
            c.text((mx - 4, 117), k, 15, True, _rgb(h, .5, .88), "mm")
        else:
            c.circle(mx - lw / 2 - 9, 117, 5, fill=IMPACT[k])
            c.text((mx + 4, 117), k, 15, True, _rgb(h, .5, .88), "mm")

    # ── 24h pulse strip: every event as a tick at its clock time, taller = more impact ──
    c.rrect((36, pulse_y, W - 36, pulse_y + pulse_h), 30, fill=(255, 255, 255, 255), shadow=(_rgb(h, .5, .30) + (40,), 16, 8))
    c.text((72, pulse_y + 30), "DAY PULSE", 15, True, deep, "lm", track=3)
    ax0, ax1, base = 250, W - 72, pulse_y + 62
    c.line((ax0, base), (ax1, base), rule, 2)
    for hh in (0, 6, 12, 18, 24):
        x = ax0 + (ax1 - ax0) * hh / 24
        c.line((x, base), (x, base + 6), mut, 1.5, 150)
        c.text((x, base + 15), f"{hh:02d}", 12, True, mut, "mm")
    for r in rows:
        mins = _clock_min(r.get("Time"))
        if mins is None:
            continue
        x = ax0 + (ax1 - ax0) * mins / 1440
        hh = {"High": 38, "Medium": 26, "Low": 14}.get(r.get("Impact"), 14)
        col = IMPACT.get(r.get("Impact"), IMPACT["High"])
        c.rrect((x - 3, base - hh, x + 3, base), 3, fill=tuple(col) + (235,))

    # ── the table ──
    cx0_, cx1_ = 36, W - 36
    ty1 = tbl_y + th_h + max(n, 1) * rh + 18
    c.rrect((cx0_, tbl_y, cx1_, ty1), 36, fill=(255, 255, 255, 255), shadow=(_rgb(h, .5, .30) + (46,), 20, 10))
    for lab, x, a in (("TIME", cx0_ + 44, "l"), ("CUR", cx0_ + 150, "m"), ("EVENT", cx0_ + 248, "l"),
                      ("FCST", 770, "r"), ("PREV", 880, "r"), ("ACT", 968, "m")):
        c.text((x + (0 if a != "m" else 0), tbl_y + 36), lab, 13, True, mut, a + "m", track=2)
    c.line((cx0_ + 28, tbl_y + th_h - 4), (cx1_ - 28, tbl_y + th_h - 4), rule, 1.5)
    prev_time = None
    for i, r0 in enumerate(rows):
        r = _row(r0)
        y = tbl_y + th_h + i * rh
        cy = y + rh / 2
        col = IMPACT.get(r["imp"], IMPACT["High"])
        hi = r["imp"] == "High"
        if hi:
            c.rrect((cx0_ + 12, y + 3, cx1_ - 12, y + rh - 3), 18, fill=tuple(col) + (24,))
        elif i:
            c.line((cx0_ + 40, y), (cx1_ - 40, y), rule, 1, 200)
        c.rrect((cx0_ + 20, y + 14, cx0_ + 26, y + rh - 14), 3, fill=col)
        same = prev_time == r["time"]
        prev_time = r["time"]
        c.text((cx0_ + 44, cy), r["time"], 26, True, _mix(ink, (255, 255, 255), .62) if same else ink, "lm")
        cw = max(c.tw(r["cur"], 19, True) + 26, 70)
        c.pill(cx0_ + 150 - cw / 2, cy - 17, cw, 34, _rgb(h, .65, .93))
        c.text((cx0_ + 150, cy), r["cur"], 19, True, deep, "mm")
        nm, nsz = c.fit(r["name"], 25, True, "sans", 372, 16)
        c.text((cx0_ + 248, cy), nm, nsz, True, ink, "lm")
        sk = _speech_kind(r0)
        if sk:
            c.speech_badge(1004, cy, sk, deep, txt=deep, fill=_rgb(h, .65, .93), outline=_rgb(h, .5, .78), h=46, anchor="r",
                           glow=False, imp=r0.get("Impact"))
        else:
            for val, x in ((r["fc"], 770), (r["pv"], 880)):
                c.text((x, cy), val or "–", 22, True, ink if val else _mix(mut, (255, 255, 255), .55), "rm")
            tone = _tone(r0, False)
            if r["act"]:
                aw = max(c.tw(r["act"], 22, True) + 28, 70)
                cc = tone or ink
                c.pill(968 - aw / 2, cy - 19, aw, 38, tuple(cc) + (34,))
                c.text((968, cy), r["act"], 22, True, cc, "mm")
            else:
                c.text((968, cy), "—", 22, True, _mix(mut, (255, 255, 255), .55), "mm")
    if not rows:
        c.text((W / 2, tbl_y + th_h + rh / 2), "—", 30, True, mut, "mm")

    # ── footer: source + impact legend ──
    fy = ty1 + 52
    c.text((48, fy), _footer(bot), 19, False, mut, "lm")
    return c.done()




# ═══════════════  3 · SOFT LEDGER (replaces Editorial; same table as 7, tinted theme)  ═══════════════
def _ledger_soft(rows, title, logo, brand, label, bot):
    h, s, _ = _hls(brand)
    s = max(.5, min(s, .85))
    n = len(rows)
    rh, band, pulse_y, pulse_h, tbl_y, th_h = 62, 214, 238, 84, 346, 56
    foot = 96
    c = Cv(tbl_y + th_h + max(n, 1) * rh + 24 + foot)
    ink, mut, rule = _rgb(h, .45, .12), _rgb(h, .16, .44), _rgb(h, .22, .84)
    deep = _rgb(h, s, .30)
    card = _rgb(h, .34, .945)                      # soft tinted card: never pure white (easier on the eyes)
    dim = _mix(mut, card, .45)
    c.gradient(_rgb(h, .30, .885), _rgb(h, .28, .85))

    # ── header band: logo, title, date, impact counts ──
    c.gradient(_rgb(h, s, .30), _rgb(h + .05, s, .20), diag=True, box=(0, -90, W, band), radius=54)
    c.over((0, 0, W, band), lambda dd, tx, ty: [dd.ellipse((tx(W - 250 + i * 54), ty(-190 + i * 20), tx(W + 190 + i * 54),
                                                            ty(250 + i * 20)), outline=(255, 255, 255, 22), width=c.k(2))
                                              for i in range(4)])
    wd, mon, day = _date_parts(rows)
    fa = _is_fa()                                   # Persian: logo + title RIGHT, impact counts LEFT (High first from the right)
    bx = (W - 44 - 84) if fa else 44                # logo plate x
    lo = 108 if logo is not None else 0
    tx0, ta = (W - 44 - lo, "rm") if fa else (44 + lo, "lm")
    if logo is not None:
        c.rrect((bx, 46, bx + 84, 130), 24, fill=(255, 255, 255, 255))
        c.logo(logo, bx + 6, 52, 72, 18)
    t, sz = c.fit(title, 42, True, "sans", 470, 28)
    c.text((tx0, 62), "ECONOMIC CALENDAR", 15, True, _rgb(h, .9, .78), ta, track=3)
    c.text((tx0, 98), t, sz, True, (255, 255, 255), ta)
    c.text((tx0, 138), _dline(wd, mon, day, "sub"), 21, False, _rgb(h, .5, .86), ta)
    _chan(c, tx0, 176, ta, _rgb(h, .9, .78), 22)
    cnt = {k: sum(1 for r in rows if r.get("Impact") == k) for k in ("High", "Medium", "Low")}
    cx0, sw = (44 if fa else W - 44 - 3 * 124), 124
    c.rrect((cx0, 44, cx0 + 3 * sw, 140), 30, fill=(255, 255, 255, 26), outline=(255, 255, 255, 64))
    for i, k in enumerate(("High", "Medium", "Low")):
        mx = cx0 + sw * ((2 - i if fa else i) + .5)
        if i:
            lx_ = cx0 + sw * (3 - i if fa else i)
            c.line((lx_, 64), (lx_, 120), (255, 255, 255), 1, 60)
        c.text((mx, 80), _n(cnt[k]), 40, True, (255, 255, 255), "mm")
        lw = c.tw(k, 15, True)
        if fa:
            c.circle(mx + lw / 2 + 9, 117, 5, fill=IMPACT[k])
            c.text((mx - 4, 117), k, 15, True, _rgb(h, .5, .88), "mm")
        else:
            c.circle(mx - lw / 2 - 9, 117, 5, fill=IMPACT[k])
            c.text((mx + 4, 117), k, 15, True, _rgb(h, .5, .88), "mm")

    # ── 24h pulse strip: every event as a tick at its clock time, taller = more impact ──
    c.rrect((36, pulse_y, W - 36, pulse_y + pulse_h), 30, fill=card + (255,), shadow=(_rgb(h, .5, .30) + (52,), 16, 8))
    c.text((72, pulse_y + 30), "DAY PULSE", 15, True, deep, "lm", track=3)
    ax0, ax1, base = 250, W - 72, pulse_y + 62
    c.line((ax0, base), (ax1, base), rule, 2)
    for hh in (0, 6, 12, 18, 24):
        x = ax0 + (ax1 - ax0) * hh / 24
        c.line((x, base), (x, base + 6), mut, 1.5, 150)
        c.text((x, base + 15), f"{hh:02d}", 12, True, mut, "mm")
    for r in rows:
        mins = _clock_min(r.get("Time"))
        if mins is None:
            continue
        x = ax0 + (ax1 - ax0) * mins / 1440
        hh = {"High": 38, "Medium": 26, "Low": 14}.get(r.get("Impact"), 14)
        col = IMPACT.get(r.get("Impact"), IMPACT["High"])
        c.rrect((x - 3, base - hh, x + 3, base), 3, fill=tuple(col) + (235,))

    # ── the table ──
    cx0_, cx1_ = 36, W - 36
    ty1 = tbl_y + th_h + max(n, 1) * rh + 18
    c.rrect((cx0_, tbl_y, cx1_, ty1), 36, fill=card + (255,), shadow=(_rgb(h, .5, .30) + (58,), 20, 10))
    for lab, x, a in (("TIME", cx0_ + 44, "l"), ("CUR", cx0_ + 150, "m"), ("EVENT", cx0_ + 248, "l"),
                      ("FCST", 770, "r"), ("PREV", 880, "r"), ("ACT", 968, "m")):
        c.text((x + (0 if a != "m" else 0), tbl_y + 36), lab, 13, True, mut, a + "m", track=2)
    c.line((cx0_ + 28, tbl_y + th_h - 4), (cx1_ - 28, tbl_y + th_h - 4), rule, 1.5)
    prev_time = None
    for i, r0 in enumerate(rows):
        r = _row(r0)
        y = tbl_y + th_h + i * rh
        cy = y + rh / 2
        col = IMPACT.get(r["imp"], IMPACT["High"])
        hi = r["imp"] == "High"
        if hi:
            c.rrect((cx0_ + 12, y + 3, cx1_ - 12, y + rh - 3), 18, fill=tuple(col) + (24,))
        elif i:
            c.line((cx0_ + 40, y), (cx1_ - 40, y), rule, 1, 200)
        c.rrect((cx0_ + 20, y + 14, cx0_ + 26, y + rh - 14), 3, fill=col)
        same = prev_time == r["time"]
        prev_time = r["time"]
        c.text((cx0_ + 44, cy), r["time"], 26, True, _mix(ink, card, .62) if same else ink, "lm")
        cw = max(c.tw(r["cur"], 19, True) + 26, 70)
        c.pill(cx0_ + 150 - cw / 2, cy - 17, cw, 34, _rgb(h, .55, .87))
        c.text((cx0_ + 150, cy), r["cur"], 19, True, deep, "mm")
        nm, nsz = c.fit(r["name"], 25, True, "sans", 372, 16)
        c.text((cx0_ + 248, cy), nm, nsz, True, ink, "lm")
        sk = _speech_kind(r0)
        if sk:
            c.speech_badge(1004, cy, sk, deep, txt=deep, fill=_rgb(h, .55, .87), outline=_rgb(h, .5, .78), h=46, anchor="r",
                           glow=False, imp=r0.get("Impact"))
        else:
            for val, x in ((r["fc"], 770), (r["pv"], 880)):
                c.text((x, cy), val or "–", 22, True, ink if val else dim, "rm")
            tone = _tone(r0, False)
            if r["act"]:
                aw = max(c.tw(r["act"], 22, True) + 28, 70)
                cc = tone or ink
                c.pill(968 - aw / 2, cy - 19, aw, 38, tuple(cc) + (34,))
                c.text((968, cy), r["act"], 22, True, cc, "mm")
            else:
                c.text((968, cy), "—", 22, True, dim, "mm")
    if not rows:
        c.text((W / 2, tbl_y + th_h + rh / 2), "—", 30, True, mut, "mm")

    # ── footer: source + impact legend ──
    fy = ty1 + 52
    c.text((48, fy), _footer(bot), 19, False, mut, "lm")
    return c.done()



# ═══════════════  7/8 · AURORA (light + dark)  ═══════════════
# Frosted glass over a living aurora. Fully right-to-left in Persian (logo / time / name on the RIGHT, values on the LEFT).
def _aurora(rows, title, logo, brand, label, bot, light=False):
    h, s, _ = _hls(brand)
    fa = _is_fa()
    st, sg = ("r", -1) if fa else ("l", 1)               # start side of a line of text, and the direction it grows in
    X = (lambda d: W - d) if fa else (lambda d: d)       # x at distance d from the START edge
    head, rh, gap, foot = 276, 126, 14, 96
    n = max(len(rows), 1)
    c = Cv(head + n * (rh + gap) + foot)
    glow = c.tint if light else c.glow
    if light:       # pearl-white page, pastel aurora, ink text
        ink, sub, mutc = _rgb(h, .50, .12), _rgb(h, .25, .38), _rgb(h, .18, .46)
        acc, acc2 = _rgb(h, .80, .42), _rgb(h + .16, .75, .40)
        gl, gl_o = (255, 255, 255, 150), (255, 255, 255, 255)           # glass fill / glass edge
        shadow = (_rgb(h, .5, .30) + (46,), 18, 10)
        c.gradient(_rgb(h, .35, .975), _rgb(h + .05, .40, .935))
        glow(120, 110, 440, _rgb(h, .90, .76), 130)
        glow(W - 60, 330, 420, _rgb(h + .16, .85, .80), 120)
        glow(60, c.H * .52, 460, _rgb(h - .10, .85, .78), 110)
        glow(W - 40, c.H - 140, 500, _rgb(h + .32, .80, .82), 120)
        glow(W * .5, c.H - 10, 360, _rgb(h + .06, .85, .80), 80)
    else:           # deep night, neon aurora, white text
        ink, sub, mutc = (255, 255, 255), _rgb(h, .45, .86), _rgb(h, .40, .74)
        acc, acc2 = _rgb(h, .95, .70), _rgb(h + .16, .90, .72)
        gl, gl_o = (255, 255, 255, 20), (255, 255, 255, 60)
        shadow = ((0, 0, 0, 100), 16, 9)
        c.gradient(_rgb(h, .60, .045), _rgb(h + .04, .55, .09))
        glow(120, 110, 440, _rgb(h, .95, .54), 150)
        glow(W - 60, 330, 420, _rgb(h + .16, .90, .52), 125)
        glow(60, c.H * .52, 460, _rgb(h - .10, .90, .48), 105)
        glow(W - 40, c.H - 140, 500, _rgb(h + .32, .85, .52), 115)
        glow(W * .5, c.H - 10, 360, _rgb(h + .06, .90, .55), 60)
        rnd = random.Random(11)
        for _ in range(44):
            sx_, sy_, r_, a_ = (rnd.uniform(14, W - 14), rnd.uniform(14, c.H - 14), rnd.choice((1, 1, 1.4, 2)),
                                rnd.randint(50, 160))
            c.over((sx_ - 3, sy_ - 3, sx_ + 3, sy_ + 3), lambda dd, tx, ty, sx_=sx_, sy_=sy_, r_=r_, a_=a_: dd.ellipse(
                (tx(sx_ - r_), ty(sy_ - r_), tx(sx_ + r_), ty(sy_ + r_)), fill=(255, 255, 255, a_)))
    hair = ink if light else (255, 255, 255)

    # ── glass header: logo + title + chips on the start side, a glowing date orb on the other ──
    wd, mon, day = _date_parts(rows)
    hb = (36, 36, W - 36, 244)
    c.rrect(hb, 52, fill=gl, outline=gl_o, width=2 if light else 1, shadow=shadow if light else ((0, 0, 0, 110), 26, 14))
    ls_, ly = 104, 88
    lx = X(66) - ls_ if fa else 66
    if logo is not None:
        glow(lx + ls_ / 2, ly + ls_ / 2, 80, acc, 60 if light else 90)
        c.logo(logo, lx, ly, ls_, 30)
        c.rrect((lx - 5, ly - 5, lx + ls_ + 5, ly + ls_ + 5), 35, outline=acc + (215,), width=3)
    tx0 = X(66 + (ls_ + 28 if logo is not None else 0))
    t, sz = c.fit(title, 44, True, "sans", 540, 30)
    c.text((tx0, 104), t, sz, True, ink, st + "m")
    c.text((tx0, 150), _dline(wd, mon, day, "sub"), 23, False, sub, st + "m")
    cx_ = tx0
    for txt_, col_ in ((label, acc2), (_CH.get(), ink)):
        if not txt_:
            continue
        t_, ts_ = c.fit(txt_, 19, True, "sans", 250, 14)
        tw_ = c.tw(t_, ts_, True) + 34
        x0_ = cx_ - tw_ if fa else cx_
        c.pill(x0_, 182, tw_, 36, (255, 255, 255, 190) if light else (255, 255, 255, 26),
               (acc + (70,)) if light else (255, 255, 255, 72))
        c.text((x0_ + tw_ / 2, 200), t_, ts_, True, col_, "mm")
        cx_ += sg * (tw_ + 10)
    ocx, ocy, orad = (144 if fa else W - 144), 140, 78
    glow(ocx, ocy, 124, acc, 55 if light else 95)
    c.over((ocx - orad - 8, ocy - orad - 8, ocx + orad + 8, ocy + orad + 8), lambda dd, tx, ty: (
        dd.ellipse((tx(ocx - orad), ty(ocy - orad), tx(ocx + orad), ty(ocy + orad)),
                   fill=(255, 255, 255, 200) if light else (255, 255, 255, 24), outline=acc + (240,), width=c.k(5)),
        dd.ellipse((tx(ocx - orad + 16), ty(ocy - orad + 16), tx(ocx + orad - 16), ty(ocy + orad - 16)),
                   outline=(acc + (70,)) if light else (255, 255, 255, 48), width=c.k(2))))
    c.text((ocx, ocy - 10), _n(day) if day else "•", 78, True, ink, "mm")
    if mon:
        c.text((ocx, ocy + 46), _mon_name(mon), 22, True, acc, "mm")

    # ── event cards ──
    y = head
    for i, r0 in enumerate(rows):
        r = _row(r0)
        col = IMPACT.get(r["imp"], IMPACT["High"])
        cy = y + rh / 2
        box = (40, y, W - 40, y + rh)
        c.rrect(box, 34, fill=(255, 255, 255, 170) if light else (255, 255, 255, 18),
                outline=(255, 255, 255, 255) if light else (255, 255, 255, 52), width=2 if light else 1, shadow=shadow)
        c.corner_glow(box, 34, col, 62 if light else 80, X(40), cy, 130)     # lit from its start edge in the impact colour
        c.rrect((X(66) - 3, cy - 32, X(66) + 3, cy + 32), 3, fill=col)
        c.text((X(92), cy - 17), r["time"], 36, True, ink, st + "m")
        cw = c.tw(r["cur"], 17, True) + 26
        px_ = X(92) - cw if fa else X(92)
        c.pill(px_, cy + 9, cw, 30, acc + (38 if light else 46,))
        c.text((px_ + cw / 2, cy + 24), r["cur"], 17, True, acc, "mm")
        c.line((X(226), cy - 34), (X(226), cy + 34), hair, 1, 34 if light else 40)
        has_v = bool(r["fc"] or r["pv"])
        nm, nsz = c.fit(r["name"], 29, True, "sans", 540, 20)
        c.text((X(250), cy - 17 if has_v else cy), nm, nsz, True, ink, st + "m")
        if has_v:
            vx = X(250)
            for lab, val in (("FORECAST", r["fc"] or "–"), ("PREVIOUS", r["pv"] or "–")):
                vx += sg * (c.text((vx, cy + 24), lab, 14, True, mutc, st + "m", track=1.5) + 8)
                vx += sg * (c.text((vx, cy + 24), val, 21, True, ink if light else (235, 240, 250), st + "m") + 28)
        bw_, bh_ = 190, 74
        bx0 = 62 if fa else W - 62 - bw_
        sk = _speech_kind(r0)
        if sk:
            c.speech_badge(bx0 if fa else bx0 + bw_, cy, sk, acc, txt=ink, h=60, anchor="l" if fa else "r",
                           imp=r0.get("Impact"))
        else:
            tone = _tone(r0, not light)
            tc = tone or ink
            by0 = cy - bh_ / 2
            lit = bool(tone and r["act"])
            if lit and not light:
                glow(bx0 + bw_ / 2, cy, 74, tc, 46)
            c.rrect((bx0, by0, bx0 + bw_, by0 + bh_), 26,
                    fill=(tc + ((30 if light else 36),)) if lit else ((255, 255, 255, 150) if light else (255, 255, 255, 14)),
                    outline=(tc + (200,)) if lit else ((acc + (50,)) if light else (255, 255, 255, 44)), width=2)
            c.text((bx0 + bw_ / 2, by0 + 17), "ACTUAL", 12, True, mutc, "mm", track=2)
            if r["act"]:
                a_, f_ = _num(r["act"]), _num(r["fc"])
                arrow = tone is not None and a_ is not None and f_ is not None
                vx_ = bx0 + bw_ / 2 + (sg * 12 if arrow else 0)
                c.text((vx_, by0 + 47), r["act"], 32, True, tc, "mm")
                if arrow:
                    ax_, ay_ = (bx0 + bw_ - 26 if fa else bx0 + 26), by0 + 47
                    pts = ([(ax_ - 9, ay_ + 7), (ax_ + 9, ay_ + 7), (ax_, ay_ - 8)] if a_ > f_
                           else [(ax_ - 9, ay_ - 7), (ax_ + 9, ay_ - 7), (ax_, ay_ + 8)])
                    c.d.polygon([(c.k(px), c.k(py)) for px, py in pts], fill=tc)
            else:
                c.text((bx0 + bw_ / 2, by0 + 47), "—", 30, True, mutc, "mm")
        y += rh + gap

    fy = c.H - foot
    c.line((40, fy + 10), (W - 40, fy + 10), hair, 1, 36 if not light else 30)
    c.text((W / 2, fy + 52), _footer(bot), 19, False, mutc, "mm")
    return c.done()


def _aurora_light(rows, title, logo, brand, label, bot):
    return _aurora(rows, title, logo, brand, label, bot, light=True)


RENDERERS = {"timeline": _timeline, "bento": _bento, "editorial": _ledger_soft, "hud": _hud, "ticket": _ticket,
             "spotlight": _spotlight, "ledger": _ledger, "aurora": _aurora, "aurora_light": _aurora_light}   # "ledger" stays drawable for old data


# ───────────────  public API  ────────────────
def render_image(records, style="timeline", title="Economic Calendar", logo=None, bot_name=BOT_NAME,
                 impact_label="High impact", max_rows=12, brand=None, tz="ny", language="en", channel="") -> Image.Image:
    """tz = a zone name ('Asia/Dubai') or the old keys 'ny' / 'tehran'; language 'fa' = Persian labels (needs libraqm,
    otherwise the picture stays English). Event names, currencies and values are never translated."""
    logo_img = _load_img(logo)
    fn = RENDERERS.get(style, _timeline)
    if language == "hi" and not RTL_OK:
        raise RuntimeError("Hindi image rendering requires Pillow built with libraqm")
    lang = language if language in image_locale.SUPPORTED else ("fa" if (language == "fa" and RTL_OK) else "en")
    t1, t2, t3 = _TZ.set(tz or "ny"), _LANG.set(lang), _ZT.set(_zone_text(tz or "ny", lang))
    t4 = _CH.set(channel or "")
    try:
        return fn(records[:max_rows], title, logo_img, brand or brand_color(logo_img), impact_label, bot_name)
    finally:
        _CH.reset(t4)
        _ZT.reset(t3)
        _LANG.reset(t2)
        _TZ.reset(t1)


def render_calendar(records, style="timeline", title="Economic Calendar", logo=None, bot_name=BOT_NAME,
                    impact_label="High impact", max_rows=12, tz="ny", language="en", channel="") -> bytes:
    out = io.BytesIO()
    render_image(records, style, title, logo, bot_name, impact_label, max_rows, tz=tz, language=language,
                 channel=channel).save(
        out, "PNG", optimize=True)
    return out.getvalue()


SAMPLE = [
    {"Date": "Thu, Oct 29", "Time": "08:30", "Currency": "USD", "Event": "Core PCE Price Index m/m",
     "Forecast": "0.2%", "Previous": "0.1%", "Actual": "0.3%", "Impact": "High"},
    {"Date": "Thu, Oct 29", "Time": "08:30", "Currency": "USD", "Event": "Unemployment Claims",
     "Forecast": "224K", "Previous": "218K", "Actual": "219K", "Impact": "High"},
    {"Date": "Thu, Oct 29", "Time": "09:45", "Currency": "EUR", "Event": "ECB Press Conference",
     "Forecast": "", "Previous": "", "Actual": "", "Impact": "High"},
    {"Date": "Thu, Oct 29", "Time": "10:00", "Currency": "CAD", "Event": "BOC Rate Statement",
     "Forecast": "", "Previous": "", "Actual": "", "Impact": "High"},
    {"Date": "Thu, Oct 29", "Time": "10:30", "Currency": "GBP", "Event": "Nationwide HPI m/m",
     "Forecast": "0.3%", "Previous": "-0.2%", "Actual": "", "Impact": "Medium"},
    {"Date": "Thu, Oct 29", "Time": "12:30", "Currency": "EUR", "Event": "German Unemployment Change",
     "Forecast": "5K", "Previous": "12K", "Actual": "", "Impact": "Low"},
    {"Date": "Thu, Oct 29", "Time": "19:30", "Currency": "JPY", "Event": "Tokyo Core CPI y/y",
     "Forecast": "2.1%", "Previous": "2.4%", "Actual": "", "Impact": "High"},
]


def _number_badge(n: int, size: int = 66) -> Image.Image:
    big = size * 4
    im = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse((0, 0, big - 1, big - 1), fill=(255, 255, 255, 245))
    d.ellipse((big * .07, big * .07, big * .93, big * .93), outline=(20, 24, 36, 255), width=int(big * .05))
    d.text((big / 2, big / 2 + big * .02), str(n), font=_font(big * .56, True), fill=(20, 24, 36, 255), anchor="mm")
    return im.resize((size, size), Image.LANCZOS)


def _assemble_sheet(cells: list, cell_w: int = 640, cols: int = 2, language: str = "en") -> Image.Image:
    """Numbered contact sheet of the 8 designs (names from STYLE_NAMES)."""
    thumbs = [c.resize((cell_w, int(c.height * cell_w / c.width)), Image.LANCZOS) for c in cells]
    gap, label_h = 26, 84
    nrows = (len(thumbs) + cols - 1) // cols
    row_h = [max(t.height for t in thumbs[r * cols:(r + 1) * cols]) for r in range(nrows)]
    sheet = Image.new("RGB", (gap + cols * (cell_w + gap), gap + sum(label_h + hh + gap for hh in row_h)), (14, 16, 22))
    d = ImageDraw.Draw(sheet)
    y = gap
    for r in range(nrows):
        for ci in range(cols):
            i = r * cols + ci
            if i >= len(thumbs):
                continue
            x = gap + ci * (cell_w + gap)
            badge = _number_badge(i + 1)
            sheet.paste(badge, (x, y + 4), badge)
            d.text((x + 84, y + 37), image_locale.style_name(language, STYLE_KEYS[i], STYLE_NAMES[STYLE_KEYS[i]]), font=_font(34, True, ("native_hi" if language == "hi" else "native_latin") if language in image_locale.SUPPORTED else "sans"), fill=(235, 238, 247), anchor="lm")
            m = Image.new("L", thumbs[i].size, 0)
            ImageDraw.Draw(m).rounded_rectangle((0, 0, thumbs[i].width - 1, thumbs[i].height - 1), 22, fill=255)
            sheet.paste(thumbs[i], (x, y + label_h), m)
        y += label_h + row_h[r] + gap
    return sheet


def _sheet_png(sheet: Image.Image) -> bytes:
    out = io.BytesIO()
    sheet.save(out, "PNG", optimize=True)
    return out.getvalue()


def _sample_for(tz_key: str) -> list:
    """The sample rows with their clock times moved from New York to the user's zone (preview only)."""
    try:
        import datetime as _dt
        import pytz
        now = _dt.datetime.now(pytz.utc)
        delta = (now.astimezone(pytz.timezone(_LEGACY_ZONES.get(tz_key, tz_key))).utcoffset()
                 - now.astimezone(pytz.timezone("America/New_York")).utcoffset())
        out = []
        for r in SAMPLE:
            h, m = map(int, r["Time"].split(":"))
            mins = (h * 60 + m + int(delta.total_seconds() // 60)) % 1440
            out.append({**r, "Time": f"{mins // 60:02d}:{mins % 60:02d}"})
        return out
    except Exception:
        return SAMPLE


def render_sheet(logo=None, cell_w: int = 640, cols: int = 2, progress=None, language: str = "en", tz: str = "ny",
                 channel: str = "") -> bytes:
    """📰 NEWS: one picture with all 6 news designs, numbered 1..6, built from the same logo."""
    logo_img = _load_img(logo)
    brand = brand_color(logo_img)
    steps = len(STYLE_KEYS) + 1              # 8 designs + final assembly
    cells = []
    for n, key in enumerate(STYLE_KEYS, 1):
        cells.append(render_image(_sample_for(tz)[:(7 if key in ("ledger", "editorial") else 4)], key,
                                  "اخبار اقتصادی فارکس" if (language == "fa" and RTL_OK)
                                  else image_locale.label(language, "news") if language in image_locale.SUPPORTED else "Forex Economic News",
                                  logo_img, brand=brand, tz=tz, language=language, channel=channel,
                                  impact_label="اهمیت بالا" if (language == "fa" and RTL_OK)
                                  else image_locale.label(language, "high_impact") if language in image_locale.SUPPORTED else "High impact"))
        if progress:
            progress(n, steps)
    png = _sheet_png(_assemble_sheet(cells, cell_w, cols, language))
    if progress:
        progress(steps, steps)
    return png


# ═══════════════  DATE CARD (▦ today's calendar)  ═══════════════
# Same 8 designs, same logo/brand colour as the news picture. The caller (telegram_bot) builds the `model`:
#   rtl      True when Persian text is drawn (needs libraqm, see RTL_OK)
#   title    "Today's calendar" / "تقویم امروز"          sub    "New York time (ET)"
#   kind     small label of the main calendar            hero   {wd, day, month, year}
#   grid     {title, heads[7], start (0-6), days, today, weekend[cols]}
#   second   {label, big, sub} or None                   stats  [(label, value), ...] 2-3 items
RTL_OK = features.check("raqm")
FAMILIES["fa"] = (["Vazirmatn-Regular.ttf", "Vazirmatn[wght].ttf", "Vazir.ttf", "Vazir-Regular.ttf", "Shabnam.ttf",
                   "Sahel.ttf", "Samim.ttf", "NotoSansArabic-Regular.ttf", "NotoSansArabic[wdth,wght].ttf",
                   "NotoNaskhArabic-Regular.ttf", "DejaVuSans.ttf"],
                  ["Vazirmatn-Bold.ttf", "Vazirmatn[wght].ttf", "Vazir-Bold.ttf", "Shabnam-Bold.ttf", "Sahel-Bold.ttf",
                   "Samim-Bold.ttf", "NotoSansArabic-Bold.ttf", "NotoSansArabic[wdth,wght].ttf",
                   "NotoNaskhArabic-Bold.ttf", "DejaVuSans-Bold.ttf"])
DATE_H = 900



# ═══════════════  SEASONS (date card): every design repainted in the colours of the season + hand-drawn decor  ═══════════════
SEASON_KEYS = ("spring", "summer", "autumn", "winter")
# hue of the whole design; the decor below adds the season's own motifs (blossoms, sun + sea, leaves, snow)
_SEASON_HUE = {"spring": .40, "summer": .53, "autumn": .065, "winter": .59}
_LIGHT_DATE_STYLES = ("bento", "editorial", "ledger", "aurora_light")


def season_brand(season: str) -> tuple:
    return _rgb(_SEASON_HUE[season], .72, .48)


def _rot(pts, cx, cy, a):
    ca, sa = math.cos(a), math.sin(a)
    return [(cx + (x - cx) * ca - (y - cy) * sa, cy + (x - cx) * sa + (y - cy) * ca) for x, y in pts]


def _ell(cx, cy, rx, ry, a=0.0, n=26):
    pts = [(cx + rx * math.cos(2 * math.pi * i / n), cy + ry * math.sin(2 * math.pi * i / n)) for i in range(n)]
    return _rot(pts, cx, cy, a)


def _blossom(cx, cy, r, a, col, mid):
    """Five-petal blossom: petals are fat ellipses around the centre, a darker heart and little stamens."""
    out = []
    for i in range(5):
        ang = a + i * 2 * math.pi / 5
        out.append((_ell(cx + r * .52 * math.cos(ang), cy + r * .52 * math.sin(ang), r * .56, r * .44, ang), col))
    out.append((_ell(cx, cy, r * .22, r * .22), mid))
    for i in range(7):
        ang = a + i * 2 * math.pi / 7
        out.append((_ell(cx + r * .34 * math.cos(ang), cy + r * .34 * math.sin(ang), r * .05, r * .05), (255, 214, 120)))
    return out


def _leaf(cx, cy, L, a, n=22):
    """Pointed leaf: a lens with a short stem; returns (outline, midrib, stem)."""
    up, dn = [], []
    for i in range(n + 1):
        t = i / n
        x = (t - .5) * L
        w = L * .30 * math.sin(math.pi * t) ** .8 * (1 - .25 * t)
        up.append((cx + x, cy - w))
        dn.append((cx + x, cy + w))
    outline = _rot(up + dn[::-1], cx, cy, a)
    rib = _rot([(cx - L * .5, cy), (cx + L * .45, cy)], cx, cy, a)
    stem = _rot([(cx - L * .5, cy), (cx - L * .66, cy + L * .05)], cx, cy, a)
    return outline, rib, stem


def _flake_lines(cx, cy, R, rot=0.0):
    segs = []
    for i in range(6):
        ang = rot + i * math.pi / 3
        ex, ey = cx + R * math.cos(ang), cy + R * math.sin(ang)
        segs.append(((cx, cy), (ex, ey)))
        for frac, ln in ((.50, .34), (.78, .22)):
            bx, by = cx + R * frac * math.cos(ang), cy + R * frac * math.sin(ang)
            for sgn in (-1, 1):
                a2 = ang + sgn * math.pi / 3.2
                segs.append(((bx, by), (bx + R * ln * math.cos(a2), by + R * ln * math.sin(a2))))
    return segs


def _season_decor(c, season: str, light: bool, header_side: str = "l"):
    """Paint the season's motifs on the background (behind the cards). Fixed seeds: the picture is always the same."""
    rnd = random.Random({"spring": 11, "summer": 23, "autumn": 37, "winter": 51}[season])
    H = c.H
    K = c.k

    def layer(items, blur=0, base=(255, 255, 255)):
        """Draw the items on one transparent layer and composite it. A blurred layer keeps its colour (no dark halo):
        the empty pixels carry `base` as their colour, only their alpha is 0."""
        lay = Image.new("RGBA", (K(W), K(H)), tuple(base) + (0,))
        dd = ImageDraw.Draw(lay)
        for kind, geo, col, *w in items:
            if kind == "poly":
                dd.polygon([(K(x), K(y)) for x, y in geo], fill=col)
            elif kind == "line":
                dd.line([(K(x), K(y)) for x, y in geo], fill=col, width=K(w[0]), joint="curve")
        if blur:
            lay = lay.filter(ImageFilter.GaussianBlur(K(blur)))
        region = c.img.convert("RGBA")
        region.alpha_composite(lay)
        c.img.paste(region.convert("RGB"))

    # the title sits on one side of the header: keep the decor off it so the text stays clean
    ban = (W - 520, 24, W - 24, 168) if header_side == "r" else (24, 24, 520, 168)

    def scatter(n, margin=30, clear=False):
        """Random points, never on the title, on the footer line, and (clear=True) never on the month grid / hero text."""
        bans = [ban, (0, H - 74, W, H)] + ([(560, 160, W - 30, 650), (40, 160, 560, 620), (40, 664, W - 30, 814)] if clear else [])
        pts = []
        for _ in range(n):
            for _try in range(40):
                x, y = rnd.uniform(margin, W - margin), rnd.uniform(margin, H - margin)
                if not any(b[0] <= x <= b[2] and b[1] <= y <= b[3] for b in bans):
                    break
            pts.append((x, y))
        return pts

    if season == "spring":
        petals = [(255, 150, 196), (255, 118, 176), (255, 196, 220)] if not light else [(244, 126, 168), (249, 164, 196), (238, 104, 150)]
        mid = (232, 92, 140) if not light else (214, 62, 110)
        a_back = 150 if not light else 150
        a_front = 215 if not light else 225
        # a flowering branch from the top corner
        bx = W if header_side == "l" else 0
        sx = -1 if header_side == "l" else 1
        branch = [(bx, 40), (bx + sx * 120, 70), (bx + sx * 240, 66), (bx + sx * 330, 118), (bx + sx * 400, 190)]
        wood = (96, 62, 54, 190 if light else 150)
        back, front = [], []
        back.append(("line", branch, wood, 7))
        for fx, fy, r in ((bx + sx * 110, 68, 34), (bx + sx * 235, 70, 40), (bx + sx * 330, 118, 32), (bx + sx * 398, 188, 28)):
            for poly, col in _blossom(fx, fy, r, rnd.uniform(0, 6.28), petals[0], mid):
                front.append(("poly", poly, col + (a_front,)))
        # big soft blossoms behind the cards + floating petals
        for (x, y) in scatter(7, clear=True):
            r = rnd.uniform(46, 84)
            col = rnd.choice(petals)
            for poly, pc in _blossom(x, y, r, rnd.uniform(0, 6.28), col, mid):
                back.append(("poly", poly, pc + (a_back,)))
        for (x, y) in scatter(34, clear=True):
            r = rnd.uniform(7, 15)
            front.append(("poly", _ell(x, y, r, r * .55, rnd.uniform(0, 6.28)), rnd.choice(petals) + (rnd.randint(150, 230),)))
        # fresh leaves
        gcol = (88, 190, 120) if not light else (70, 160, 100)
        for (x, y) in scatter(9, clear=True):
            o, rib, st = _leaf(x, y, rnd.uniform(46, 80), rnd.uniform(0, 6.28))
            back.append(("poly", o, gcol + (110,)))
        layer(back, blur=2.2, base=petals[0])
        layer(front)

    elif season == "summer":
        gold = (255, 202, 70) if not light else (255, 170, 30)
        # sun with rays in the top corner opposite the title
        sx_, sy_ = (W - 120 if header_side == "l" else 120), 104
        c.glow(sx_, sy_, 230, gold, 90 if not light else 120)
        items = []
        for i in range(16):
            a = i * 2 * math.pi / 16
            a1, a2 = a - .07, a + .07
            r0, r1 = 74, 158 if i % 2 else 126
            items.append(("poly", [(sx_ + r0 * math.cos(a1), sy_ + r0 * math.sin(a1)), (sx_ + r1 * math.cos(a), sy_ + r1 * math.sin(a)),
                                   (sx_ + r0 * math.cos(a2), sy_ + r0 * math.sin(a2))], gold + (150,)))
        items.append(("poly", _ell(sx_, sy_, 62, 62), gold + (235,)))
        items.append(("poly", _ell(sx_, sy_, 46, 46), (255, 236, 150, 255)))
        layer(items)
        # three layered sea waves across the bottom + sparkles
        sea = [((60, 190, 225), 70), ((30, 150, 205), 90), ((20, 110, 180), 110)]
        if light:
            sea = [((120, 205, 235), 120), ((70, 170, 220), 120), ((40, 130, 200), 110)]
        waves = []
        for i, (col, al) in enumerate(sea):
            base = H - 96 + i * 26
            amp, ph, fr = 14 + i * 3, rnd.uniform(0, 6), 2.2 + i * .5
            pts = [(x, base + amp * math.sin(fr * x / W * 2 * math.pi + ph)) for x in range(0, W + 20, 20)]
            waves.append(("poly", pts + [(W, H), (0, H)], col + (al,)))
        layer(waves)
        sp = []
        for (x, y) in scatter(26, clear=True):
            r = rnd.uniform(2.5, 6)
            sp.append(("poly", _ell(x, y, r, r), (255, 255, 255, rnd.randint(110, 200)) if not light else gold + (rnd.randint(110, 190),)))
        layer(sp)

    elif season == "autumn":
        cols = [(226, 96, 34), (198, 52, 38), (244, 172, 44), (164, 84, 32), (210, 130, 36)]
        back, front = [], []
        for (x, y) in scatter(10, clear=True):
            o, rib, st = _leaf(x, y, rnd.uniform(90, 150), rnd.uniform(0, 6.28))
            back.append(("poly", o, rnd.choice(cols) + (120 if not light else 95,)))
        for (x, y) in scatter(22, clear=True):
            L = rnd.uniform(36, 74)
            o, rib, st = _leaf(x, y, L, rnd.uniform(0, 6.28))
            col = rnd.choice(cols)
            front.append(("poly", o, col + (rnd.randint(190, 240),)))
            front.append(("line", rib, (90, 40, 20, 150), 2))
            front.append(("line", st, (90, 40, 20, 190), 3))
        layer(back, blur=3, base=(226, 110, 40))
        layer(front)
        c.glow(W * .5, H + 40, 420, (255, 150, 40), 55)

    else:  # winter
        ice = (255, 255, 255) if not light else (92, 150, 220)
        back, front = [], []
        for (x, y) in scatter(9, clear=True):
            R = rnd.uniform(60, 110)
            for a, b in _flake_lines(x, y, R, rnd.uniform(0, 1)):
                back.append(("line", [a, b], ice + (95 if not light else 120,), 6))
        for (x, y) in scatter(16, clear=True):
            R = rnd.uniform(20, 44)
            for a, b in _flake_lines(x, y, R, rnd.uniform(0, 1)):
                front.append(("line", [a, b], ice + (225 if not light else 210,), 2.6))
        for (x, y) in scatter(70, 10, clear=True):
            r = rnd.uniform(2, 5.5)
            front.append(("poly", _ell(x, y, r, r), ice + (rnd.randint(120, 220),)))
        layer(back, blur=2.4, base=ice)
        layer(front)
        hill = (255, 255, 255) if not light else (214, 230, 248)
        pts = [(x, H - 54 + 22 * math.sin(x / W * 5.4 + .6) + 10 * math.sin(x / W * 13)) for x in range(0, W + 20, 20)]
        layer([("poly", pts + [(W, H), (0, H)], hill + (56 if not light else 190,))])


def _date_theme(style: str, brand: tuple) -> dict:
    h, s, _ = _hls(brand)
    s = max(.5, min(s, .85))
    T = dict(fam="sans", rad=26, panel=False, hero_tile=False, today_fill=None, notch=None)
    if style == "timeline":
        def bg(c):
            c.gradient(_rgb(h, .55, .06), _rgb(h, .50, .11))
            c.glow(930, 90, 330, _rgb(h, .9, .5), 110)
            c.glow(40, c.H * .5, 300, _rgb(h + .10, .8, .45), 70)
            c.glow(820, c.H - 60, 280, _rgb(h - .08, .8, .45), 70)
        acc = _rgb(h, .9, .68)
        T.update(bg=bg, fg=(255, 255, 255), mut=_rgb(h, .4, .78), acc=acc, card=(255, 255, 255, 22),
                 line=(255, 255, 255, 44), shadow=((0, 0, 0, 80), 14, 8), today_fill=acc, today_txt=_rgb(h, .55, .08),
                 head_fg=(255, 255, 255), head_mut=_rgb(h, .4, .78))
    elif style == "bento":
        def bg(c):
            c.gradient(_rgb(h, .5, .945), _rgb(h + .03, .4, .975))
        ink, acc = (20, 24, 38), _rgb(h, .8, .42)
        T.update(bg=bg, fg=ink, mut=(105, 112, 130), acc=acc, card=(255, 255, 255, 255), line=None,
                 shadow=((30, 40, 80, 46), 18, 8), rad=32, hero_tile=True, today_fill=acc, today_txt=(255, 255, 255),
                 head_fg=ink, head_mut=(105, 112, 130))
    elif style == "editorial":
        def bg(c):
            c.gradient((250, 246, 238), (244, 238, 226))
        ink, acc = (30, 27, 24), _rgb(h, .7, .34)
        T.update(bg=bg, fg=ink, mut=(120, 112, 100), acc=acc, card=None, line=ink + (70,), shadow=None, rad=4,
                 fam="serif", today_fill=ink, today_txt=(250, 246, 238), head_fg=ink, head_mut=(120, 112, 100))
    elif style == "hud":
        acc, bgc = _rgb(h, .95, .60), _rgb(h, .45, .035)

        def bg(c):
            c.gradient(bgc, _rgb(h, .45, .06))
            c.glow(120, 80, 360, acc, 55)
            c.glow(W - 80, c.H - 80, 320, _rgb(h + .15, .9, .5), 40)
            grid = _mix(acc, bgc, .93)
            for gx in range(0, W, 54):
                c.d.line((c.k(gx), 0, c.k(gx), c.k(c.H)), fill=grid, width=c.k(1))
            for gy in range(0, c.H, 54):
                c.d.line((0, c.k(gy), c.k(W), c.k(gy)), fill=grid, width=c.k(1))
            for (bx, by, sx, sy) in ((26, 26, 1, 1), (W - 26, 26, -1, 1), (26, c.H - 26, 1, -1), (W - 26, c.H - 26, -1, -1)):
                c.d.line((c.k(bx), c.k(by), c.k(bx + 54 * sx), c.k(by)), fill=acc, width=c.k(4))
                c.d.line((c.k(bx), c.k(by), c.k(bx), c.k(by + 54 * sy)), fill=acc, width=c.k(4))
        T.update(bg=bg, fg=(240, 248, 255), mut=_rgb(h, .25, .58), acc=acc, card=acc + (16,), line=acc + (60,),
                 shadow=None, rad=8, fam="mono", today_fill=acc, today_txt=bgc, head_fg=(240, 248, 255),
                 head_mut=_rgb(h, .25, .58))
    elif style == "ticket":
        bgc = _rgb(h, .5, .13)

        def bg(c):
            c.gradient(bgc, _rgb(h, .5, .17))
            c.glow(900, 60, 300, _rgb(h, .9, .45), 90)
        ink, acc = (22, 26, 40), _rgb(h, .8, .42)
        T.update(bg=bg, fg=ink, mut=(110, 116, 134), acc=acc, card=_mix(acc, (255, 255, 255), .90) + (255,), line=None,
                 shadow=None, rad=22, panel=True, notch=bgc, today_fill=acc, today_txt=(255, 255, 255),
                 head_fg=(255, 255, 255), head_mut=_rgb(h, .4, .78))
    elif style == "ledger":
        def bg(c):
            c.gradient(_rgb(h, .30, .955), _rgb(h, .28, .925))
        ink, acc = _rgb(h, .45, .12), _rgb(h, s, .34)
        T.update(bg=bg, fg=ink, mut=_rgb(h, .14, .50), acc=acc, card=(255, 255, 255, 255), line=None,
                 shadow=(_rgb(h, .5, .30) + (46,), 20, 10), rad=30, today_fill=acc, today_txt=(255, 255, 255),
                 head_fg=ink, head_mut=_rgb(h, .14, .50))
    elif style == "aurora_light":
        def bg(c):
            c.gradient(_rgb(h, .35, .975), _rgb(h + .05, .40, .935))
            c.tint(120, 110, 440, _rgb(h, .90, .76), 130)
            c.tint(W - 60, 360, 420, _rgb(h + .16, .85, .80), 120)
            c.tint(60, c.H * .55, 460, _rgb(h - .10, .85, .78), 110)
            c.tint(W - 40, c.H - 140, 500, _rgb(h + .32, .80, .82), 120)
        ink, acc = _rgb(h, .50, .12), _rgb(h, .80, .42)
        T.update(bg=bg, fg=ink, mut=_rgb(h, .18, .46), acc=acc, card=(255, 255, 255, 190), line=(255, 255, 255, 255),
                 shadow=(_rgb(h, .5, .30) + (46,), 18, 10), rad=34, today_fill=acc, today_txt=(255, 255, 255),
                 head_fg=ink, head_mut=_rgb(h, .25, .38))
    elif style == "aurora":
        def bg(c):
            c.gradient(_rgb(h, .60, .045), _rgb(h + .04, .55, .09))
            c.glow(120, 110, 430, _rgb(h, .95, .52), 120)
            c.glow(W - 60, 360, 400, _rgb(h + .16, .90, .50), 95)
            c.glow(60, c.H * .55, 440, _rgb(h - .10, .90, .46), 80)
            c.glow(W - 40, c.H - 140, 480, _rgb(h + .32, .85, .50), 85)
        acc = _rgb(h, .95, .70)
        T.update(bg=bg, fg=(255, 255, 255), mut=_rgb(h, .35, .74), acc=acc, card=(255, 255, 255, 20),
                 line=(255, 255, 255, 58), shadow=((0, 0, 0, 100), 18, 10), rad=34, today_fill=acc,
                 today_txt=_rgb(h, .6, .06), head_fg=(255, 255, 255), head_mut=_rgb(h, .4, .80))
    else:  # spotlight
        def bg(c):
            c.gradient(_rgb(h, .4, .03), _rgb(h, .4, .05))
            c.glow(300, 400, 460, _rgb(h, .9, .5), 125)
            c.glow(300, 380, 220, _rgb(h, .9, .62), 90)
            c.glow(W - 40, c.H - 40, 300, _rgb(h + .12, .8, .45), 60)
        acc = _rgb(h, .9, .70)
        T.update(bg=bg, fg=(255, 255, 255), mut=_rgb(h, .35, .72), acc=acc, card=(255, 255, 255, 16),
                 line=(255, 255, 255, 36), shadow=((0, 0, 0, 90), 16, 8), rad=30, today_fill=acc,
                 today_txt=_rgb(h, .5, .06), head_fg=(255, 255, 255), head_mut=_rgb(h, .35, .72))
    return T


def _date_card(m: dict, style: str, logo, brand, bot: str) -> Image.Image:
    season = m.get("season") if m.get("season") in SEASON_KEYS else None
    if season:
        brand = season_brand(season)            # the whole design takes the season's colour
    T = _date_theme(style, brand)
    rtl = bool(m.get("rtl"))
    fam = "fa" if rtl else T["fam"]
    up = (lambda s: s) if rtl else (lambda s: s.upper())
    c = Cv(DATE_H)
    T["bg"](c)
    if season:
        _season_decor(c, season, style in _LIGHT_DATE_STYLES, header_side="r" if rtl else "l")

    def txt(xy, s, size, bold=False, fill=None, anchor="lm", track=0):
        return c.text(xy, s, size, bold, fill or T["fg"], anchor, fam=fam, track=0 if rtl else track)

    def card(box, fill=True):
        c.rrect(box, T["rad"], fill=T["card"] if fill else None, outline=T["line"], shadow=T["shadow"])

    # ── header ──
    c.logo(logo, (W - 48 - 92) if rtl else 48, 48, 92, 26)       # Persian: logo + title RIGHT, tag pill LEFT
    t, sz = c.fit(m["title"], 46, True, fam, 560, 30)
    lo = 118 if logo is not None else 0
    htx, hta = (W - 48 - lo, "rm") if rtl else (48 + lo, "lm")
    has_sub = bool(m.get("sub"))
    txt((htx, 78 if has_sub else 94), t, sz, True, T["head_fg"], hta)
    if has_sub:
        txt((htx, 126), m["sub"], 22, False, T["head_mut"], hta, track=1)
    if m.get("kind"):                                  # empty = no label (a Persian calendar needs no "Persian" tag)
        kw = c.tw(up(m["kind"]), 17, True, fam, track=0 if rtl else 2) + 40
        kx0 = 48 if rtl else W - 48 - kw
        c.pill(kx0, 62, kw, 42, (255, 255, 255, 30) if style in ("timeline", "hud", "ticket", "spotlight") else T["acc"] + (30,),
               (255, 255, 255, 60) if style in ("timeline", "hud", "ticket", "spotlight") else T["acc"] + (90,))
        txt((kx0 + kw / 2, 83), up(m["kind"]), 17, True,
            T["acc"] if style != "ticket" else (255, 255, 255), "mm", track=2)

    top, mid = 172, 648
    if T["panel"]:
        c.rrect((32, top - 14, W - 32, 830), 38, fill=(252, 252, 254), shadow=((0, 0, 0, 90), 24, 12))
        for nx in (32, W - 32):
            c.circle(nx, 656, 20, fill=T["notch"])
        for dx in range(70, W - 60, 24):
            c.line((dx, 656), (dx + 11, 656), (150, 156, 175), 2)

    # ── hero (big day) ──
    hero = m["hero"]
    hx0, hx1 = 48, 548
    hfg, hacc = T["fg"], T["acc"]
    if T["hero_tile"]:
        c.gradient(_rgb(*_hls(brand)[:1], .8, .50), _rgb(_hls(brand)[0] + .05, .85, .32), True,
                   (hx0 - 8, top, hx1 + 12, mid - 10), 34)
        c.glow(hx1 - 40, top + 40, 150, (255, 255, 255), 60)
        hfg, hacc = (255, 255, 255), (255, 255, 255)
        hx0 += 22
    txt((hx0 + 4, top + 56), up(hero["wd"]), 34, True, hacc, track=5)
    day = hero["day"]
    dsz = 300 if len(day) <= 2 else 230
    c.text((hx0 - 6, top + 215), day, dsz, True, hfg, "lm", fam=fam)
    if style == "spotlight":
        c.glow(hx0 + 120, top + 215, 150, T["acc"], 40)
    mt, msz = c.fit(f"{hero['month']} {hero['year']}", 56, True, fam, hx1 - hx0 - 30, 34)
    txt((hx0 + 2, top + 386), mt, msz, True, hfg)
    c.rrect((hx0 + 4, top + 430, hx0 + 124, top + 438), 4, fill=hacc)

    # ── month grid ──
    g = m["grid"]
    gx0, gx1 = 580, W - 48
    card((gx0, top, gx1, mid - 10))
    txt(((gx0 + gx1) / 2, top + 42), up(g["title"]), 26, True, T["fg"], "mm", track=3)
    inner = 22
    cw = (gx1 - gx0 - 2 * inner) / 7
    cx_of = (lambda col: gx0 + inner + cw * ((6 - col) + .5)) if rtl else (lambda col: gx0 + inner + cw * (col + .5))
    for i, hd in enumerate(g["heads"]):
        wk = i in g["weekend"]
        txt((cx_of(i), top + 98), up(hd), 17, True, T["acc"] if wk else T["mut"], "mm", track=1)
    c.line((gx0 + inner, top + 124), (gx1 - inner, top + 124), T["fg"], 1, 40)
    rh = 56
    for d in range(1, g["days"] + 1):
        idx = g["start"] + d - 1
        r, col = divmod(idx, 7)
        cx, cy = cx_of(col), top + 160 + r * rh
        if d == g["today"]:
            if style == "editorial" or style == "hud":
                c.rrect((cx - 24, cy - 24, cx + 24, cy + 24), 4 if style == "hud" else 24, fill=T["today_fill"])
            else:
                c.glow(cx, cy, 30, T["acc"], 70)
                c.circle(cx, cy, 25, fill=T["today_fill"])
            txt((cx, cy), m["nums"][d - 1], 25, True, T["today_txt"], "mm")
        else:
            txt((cx, cy), m["nums"][d - 1], 24, col in g["weekend"], T["acc"] if col in g["weekend"] else T["fg"], "mm")

    # ── bottom row: other calendar + stats ──
    by0, by1 = mid + 28, 806
    sec = m.get("second")
    stats = list(m["stats"])
    if sec is None and stats:
        first = stats.pop(0)
        sec = {"label": first[0], "big": first[1], "sub": ""}
    if sec:
        card((48, by0, 548, by1))
        txt((76, by0 + 32), up(sec["label"]), 16, True, T["acc"], track=2)
        bt, bsz = c.fit(sec["big"], 40, True, fam, 440, 24)
        txt((76, by0 + 74), bt, bsz, True, T["fg"])
        if sec.get("sub"):
            txt((76, by0 + 107), sec["sub"], 20, False, T["mut"])
    if stats:
        n, gap = len(stats), 14
        sw = (W - 48 - 580 - gap * (n - 1)) / n
        for i, (lab, val) in enumerate(stats):
            x0 = 580 + i * (sw + gap)
            card((x0, by0, x0 + sw, by1))
            txt((x0 + sw / 2, by0 + 36), up(lab), 15, True, T["mut"], "mm", track=1.5)
            vt, vsz = c.fit(val, 38, True, fam, sw - 24, 20)
            txt((x0 + sw / 2, by0 + 86), vt, vsz, True, T["acc"] if style != "editorial" else T["fg"], "mm")

    # ── footer ──
    fy = DATE_H - 38
    c.line((48, fy - 26), (W - 48, fy - 26), (255, 255, 255) if style in ("timeline", "hud", "ticket", "spotlight") else T["fg"], 1, 40)
    txt((48, fy), f"powered by: {_bn(bot)}", 18, False, T["head_mut"])
    if m.get("sub"):
        txt((W - 48, fy), m["sub"], 18, False, T["head_mut"], "rm")
    return c.done()


def render_date_card(model: dict, style: str = "timeline", logo=None, bot_name: str = BOT_NAME,
                     brand=None) -> bytes:
    logo_img = _load_img(logo)
    out = io.BytesIO()
    _date_card(model, style if style in STYLE_KEYS else STYLE_KEYS[0], logo_img,
               brand or brand_color(logo_img), bot_name).save(out, "PNG", optimize=True)
    return out.getvalue()


def render_date_sheet(model: dict, logo=None, cell_w: int = 640, cols: int = 2, progress=None) -> bytes:
    """▦ CALENDAR: one picture with all 6 DATE-CARD designs (today's date, not the news), numbered 1..6."""
    logo_img = _load_img(logo)
    brand = brand_color(logo_img)
    steps = len(STYLE_KEYS) + 1
    cells = []
    for n, key in enumerate(STYLE_KEYS, 1):
        cells.append(_date_card(model, key, logo_img, brand, BOT_NAME))
        if progress:
            progress(n, steps)
    png = _sheet_png(_assemble_sheet(cells, cell_w, cols, model.get("lang", "en")))
    if progress:
        progress(steps, steps)
    return png


if __name__ == "__main__":
    Path("previews").mkdir(exist_ok=True)
    Path("previews/sheet.png").write_bytes(render_sheet(DEFAULT_LOGO))
    for key in STYLE_KEYS:
        Path(f"previews/{key}.png").write_bytes(render_calendar(SAMPLE, key, "Daily High-Impact News", DEFAULT_LOGO))
    print("ok")
