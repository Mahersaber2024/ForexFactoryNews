"""
calendar_image.py: the economic calendar as a picture, in 6 numbered designs that differ in LAYOUT (not just colour).

    render_calendar(records, style="timeline", title=..., logo=<path|bytes|PIL>)  -> PNG bytes
    render_sheet(logo)                                                            -> ONE PNG: all 6 designs, numbered

    1 Timeline   glass cards on a glowing time axis
    2 Bento      soft tiles, big date hero
    3 Editorial  magazine page: serif type, marker highlights
    4 HUD        futuristic terminal with LED impact meters
    5 Ticket     perforated boarding-pass tickets with a stamped result
    6 Spotlight  the key event as a giant hero, the rest in a list

Every design is generated from the main colour of the user's logo. Drawn at 2x and scaled down.
Needs only Pillow. Fonts live in bot/fonts/ (Carlito, Liberation Serif, DejaVu Sans / Sans Mono; Persian: Vazirmatn, fetched by bot/font_setup.py) with system fallbacks.
Persian text needs a real Persian font (Vazirmatn in bot/fonts/ or `apt install fonts-vazirmatn`/`fonts-noto-core`); DejaVu is the last resort and looks rough.
"""
from __future__ import annotations

import colorsys
import contextvars
import io
import random
import re
from pathlib import Path

from PIL import features, Image, ImageChops, ImageDraw, ImageFilter, ImageFont

BOT_NAME = "@MarketingToolssBot"
HERE = Path(__file__).parent
DEFAULT_LOGO = HERE / "logo.jpg"
DEFAULT_BRAND = (53, 85, 138)
SS = 2
W = 1080

STYLES = [("timeline", "Timeline"), ("bento", "Bento"), ("editorial", "Editorial"),
          ("hud", "HUD"), ("ticket", "Ticket"), ("spotlight", "Spotlight")]
STYLE_KEYS = [k for k, _ in STYLES]
STYLE_NAMES = dict(STYLES)

# Which clock the news rows are in and which language the labels speak. ContextVars so parallel renders never mix.
_TZ = contextvars.ContextVar("calendar_tz", default="ny")          # legacy key ("ny"/"tehran") or a zone name
_LANG = contextvars.ContextVar("calendar_lang", default="en")      # "fa" only when Persian can really be drawn
_ZT = contextvars.ContextVar("calendar_zone_text", default=None)   # {sub, caps, mono, plain} for this render
TZ_TEXT = {"ny": dict(sub="New York time (ET)", caps="NEW YORK TIME (ET)", mono="T+0 NEW YORK (ET)", plain="New York time"),
           "tehran": dict(sub="Tehran time", caps="TEHRAN TIME", mono="T+0 TEHRAN (IRST)", plain="Tehran time")}
_LEGACY_ZONES = {"ny": "America/New_York", "tehran": "Asia/Tehran"}


def _zone_text(tz_key: str, lang: str) -> dict:
    """The four captions that say which clock the times are on, for this zone and language."""
    try:
        from bot import tz as tzmod
        return tzmod.zone_text(_LEGACY_ZONES.get(tz_key, tz_key), lang)
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


_IMP_FA = {"high": "بالا", "medium": "متوسط", "low": "کم"}
_FA_EXACT = {
    "ECONOMIC CALENDAR": "تقویم اقتصادی", "ECONOMIC CALENDAR  ·  DAILY PASS": "تقویم اقتصادی  ·  بلیت روزانه",
    "// ECON.CALENDAR  ·  LIVE": "// تقویم اقتصادی  ·  زنده", "TODAY": "امروز",
    "FORECAST": "پیش‌بینی", "PREVIOUS": "قبلی", "ACTUAL": "واقعی", "FCST": "پیش‌بینی", "PREV": "قبلی", "ACT": "واقعی",
    "TIME": "ساعت", "CUR": "ارز", "EVENT": "خبر", "EVENTS": "خبر", "IMPACT": "اهمیت", "SPOTLIGHT": "خبر ویژه",
    "ADMIT": "ورود", "Source": "منبع", "F": "پیش‌بینی", "P": "قبلی",
    "Low": "کم", "Medium": "متوسط", "High": "بالا",
    "Daily High-Impact News": "اخبار مهم روزانه", "Daily Economic News": "اخبار اقتصادی روزانه",
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
    if _LANG.get() != "fa":
        return s, fam, track
    s2 = _fa(s)
    if _HAS_FA.search(s2):
        return s2, "fa", 0
    return s2, fam, track


def _dline(wd: str, mon: str, day: str, key: str, long: bool = False) -> str:
    """'Thu, Oct 29  ·  New York time (ET)' / 'پنج‌شنبه ۲۹ اکتبر  ·  به وقت تهران'; just the zone text without a date."""
    if not day:
        return _tzt(key)
    if _is_fa():
        from bot import tz as tzmod
        return f"{tzmod.display_date(f'{wd}, {mon} {day}', 'fa')}  ·  {_tzt(key)}"
    return (f"{DAYS.get(wd, wd)}, {MONTHS.get(mon, mon)} {day}" if long else f"{wd}, {mon} {day}") + f"  ·  {_tzt(key)}"


def _date_only(wd: str, mon: str, day: str) -> str:
    """Editorial header date: 'THURSDAY, OCTOBER 29' / 'پنج‌شنبه ۲۹ اکتبر' / TODAY."""
    if not day:
        return "TODAY"
    if _is_fa():
        from bot import tz as tzmod
        return tzmod.display_date(f"{wd}, {mon} {day}", "fa")
    return (DAYS.get(wd, wd) + ", " + MONTHS.get(mon, mon) + " " + day).upper()


def _month_weekday(wd: str, mon: str) -> str:
    if _is_fa():
        from bot import tz as tzmod
        return f"{tzmod._FA_MONTHS.get(mon.lower(), mon)}  ·  {tzmod._FA_WEEKDAYS.get(wd.lower(), wd)}"
    return mon.upper() + "  ·  " + wd.upper()


def _wd_name(wd: str) -> str:
    if _is_fa():
        from bot import tz as tzmod
        return tzmod._FA_WEEKDAYS.get(wd.lower(), wd)
    return DAYS.get(wd, wd)


def _mon_name(mon: str) -> str:
    if _is_fa():
        from bot import tz as tzmod
        return tzmod._FA_MONTHS.get(mon.lower(), mon)
    return MONTHS.get(mon, mon)


def _hud_date(wd: str, mon: str, day: str) -> str:
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
    return (m.group(1), m.group(2), m.group(3)) if m else ("", "", "")


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

    def pill(self, x, y, w, h, fill, outline=None):
        self.rrect((x, y, x + w, y + h), h / 2, fill=fill, outline=outline)

    def done(self) -> Image.Image:
        return self.img.resize((W, self.H), Image.LANCZOS)


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
    c.logo(logo, 48, 50, 92, 26)
    t, sz = c.fit(title, 46, True, "sans", 560, 32)
    c.text((166, 80), t, sz, True, (255, 255, 255), "lm")
    c.text((166, 128), _dline(wd, mon, day, "sub"), 23, False,
           _rgb(h, .5, .82), "lm")
    if day:
        c.text((W - 48, 84), _n(day), 92, True, (255, 255, 255), "rm")
        c.text((W - 48, 148), _month_weekday(wd, mon), 20, True, acc, "rm", track=3)
    n = f"{len(rows)} EVENT{'' if len(rows) == 1 else 'S'}"
    pw = c.tw(n, 17, True, track=2) + 34
    c.pill(48, 176, pw, 38, (255, 255, 255, 28), (255, 255, 255, 60))
    c.text((48 + pw / 2, 195), n, 17, True, acc, "mm", track=2)

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
        c.pill(248, y + 27, cw, 36, acc + (46,))
        c.text((248 + cw / 2, y + 45), r["cur"], 20, True, acc, "mm")
        name, nsz = c.fit(r["name"], 29, True, "sans", W - 48 - 190 - (262 + cw), 21)
        c.text((262 + cw, y + 45), name, nsz, True, (255, 255, 255), "lm")
        cx = 250
        if r["fc"] or r["pv"]:
            for lab, val in (("FORECAST", r["fc"] or "–"), ("PREVIOUS", r["pv"] or "–")):
                cx += c.text((cx, y + 84), lab, 14, True, _rgb(h, .35, .68), "lm", track=1.5) + 8
                cx += c.text((cx, y + 84), val, 21, True, (235, 240, 250), "lm") + 26
        else:
            c.circle(256, y + 84, 6, fill=col)
            c.text((270, y + 84), f"{r['imp']} impact", 19, False, _rgb(h, .35, .72), "lm")
        tone = _tone(rows[i], True)
        c.text((W - 74, y + 36), "ACTUAL", 13, True, _rgb(h, .35, .68), "rm", track=2)
        if r["act"]:
            c.text((W - 74, y + 70), r["act"], 36, True, tone or (255, 255, 255), "rm")
        else:
            c.text((W - 74, y + 70), "—", 32, True, _rgb(h, .25, .50), "rm")
    fy = c.H - foot
    c.line((48, fy + 12), (W - 48, fy + 12), (255, 255, 255), 1, 40)
    c.text((48, fy + 52), f"Source: ForexFactory  ·  {label}  |  by {bot}", 19, False, _rgb(h, .4, .74), "lm")
    lx = W - 48
    for nm in ("Low", "Medium", "High"):
        lx -= c.tw(nm, 17) + 6
        c.text((lx, fy + 52), nm, 17, False, _rgb(h, .4, .74), "lm")
        c.circle(lx - 10, fy + 52, 5, fill=IMPACT[nm])
        lx -= 34
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
    c.text((pad + 36, pad + 40), title.upper(), 19, True, (255, 255, 255), "lm", track=3)
    c.text((pad + 30, pad + 190), _n(day or ""), 168, True, (255, 255, 255), "ls")
    dx = pad + 36 + c.tw(_n(day or ""), 168, True) + 24
    c.text((dx, pad + 128), _wd_name(wd), 44, True, (255, 255, 255), "ls")
    c.text((dx, pad + 172), _mon_name(mon), 34, False, _rgb(h, .5, .88), "ls")
    c.rrect((W - pad - 128, pad + 28, W - pad - 32, pad + 124), 28, fill=(255, 255, 255, 255))
    c.logo(logo, W - pad - 122, pad + 34, 84, 22)
    hi = sum(1 for r in rows if r.get("Impact") == "High")
    for i, txt in enumerate((f"{n} events", f"{hi} high impact")[::-1]):
        pw = c.tw(txt, 20, True) + 36
        xr = W - pad - 36 - sum(c.tw(t, 20, True) + 36 + 10 for t in (f"{n} events", f"{hi} high impact")[::-1][:i])
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
        c.circle(x + 24 + cw + 16, y + 41, 6, fill=IMPACT.get(r["imp"], IMPACT["High"]))
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
        tone = _tone(r0, False)
        if r["act"]:
            aw = c.tw(r["act"], 24, True) + 34
            col = tone or mut
            c.pill(x + tw_ - 26 - aw, by - 20, aw, 40, tuple(col) + (36,))
            c.text((x + tw_ - 26 - aw / 2, by), r["act"], 24, True, col, "mm")
        elif not (r["fc"] or r["pv"]):
            c.text((x + 26, by), f"{r['imp']} impact", 18, False, mut, "lm")
    if n % 2:
        x, y = pad + tw_ + gap, y0 + (n // 2) * (th + gap)
        c.rrect((x, y, x + tw_, y + th), 32, fill=_rgb(h, .6, .90))
        c.text((x + 28, y + 62), "Source", 17, True, _rgb(h, .5, .40), "lm", track=3)
        c.text((x + 28, y + 100), "ForexFactory", 30, True, ink, "lm")
        c.text((x + 28, y + 150), f"by {bot}", 22, True, _rgb(h, .75, .34), "lm")
    c.text((W / 2, c.H - 36), f"Source: ForexFactory  ·  {label}  |  by {bot}", 19, False, mut, "mm")
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
    c.text((px + 80, 82), "ECONOMIC CALENDAR", 18, True, acc, "lm", track=4)
    lines = c.wrap(title, 64, True, "serif", 700, 2)
    for i, ln in enumerate(lines):
        c.text((px, 170 + i * 70), ln, 64, True, ink, "lm", fam="serif")
    ly = 170 + len(lines) * 70 + 8
    c.line((px, ly), (W - px, ly), ink, 3)
    c.text((px, ly + 30), _date_only(wd, mon, day), 19, True,
           ink, "lm", track=3)
    c.text((W - px, ly + 30), _tzt("caps"), 17, True, mut, "rm", track=3)
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
        icol = IMPACT.get(r["imp"], IMPACT["High"])
        c.circle(nx + 5, cy + 28, 5, fill=icol)
        c.text((nx + 18, cy + 28), r["imp"].upper() + " IMPACT", 14, True, _mix(icol, ink, .25), "lm", track=2)
        for j, (lab, val) in enumerate((("FORECAST", r["fc"]), ("PREVIOUS", r["pv"]))):
            c.text((700, cy - 16 + j * 32), lab, 13, True, mut, "lm", track=1.5)
            c.text((836, cy - 16 + j * 32), val or "–", 22, True, ink if val else rule, "rm")
        tone = _tone(r0, False)
        c.text((W - px, cy - 30), "ACTUAL", 13, True, mut, "rm", track=2)
        c.text((W - px, cy + 10), r["act"] or "—", 44, True, tone or (ink if r["act"] else rule), "rm", fam="serif")
    ey = y0 + len(rows) * rh
    c.line((px, ey), (W - px, ey), ink, 3)
    c.text((px, ey + 44), f"Source: ForexFactory  ·  {label}  |  by {bot}", 19, False, mut, "lm")
    return c.done()


# ═══════════════  4 · HUD  ═══════════════
def _hud(rows, title, logo, brand, label, bot):
    h, s, _ = _hls(brand)
    acc = _rgb(h, .95, .60)
    bg = _rgb(h, .45, .035)
    mut = _rgb(h, .25, .55)
    head, rh, gap, foot = 250, 92, 10, 110
    c = Cv(head + max(len(rows), 1) * (rh + gap) + foot)
    c.gradient(bg, _rgb(h, .45, .06))
    c.glow(120, 80, 360, acc, 55)
    c.glow(W - 80, c.H - 80, 320, _rgb(h + .15, .9, .5), 40)
    grid = _mix(acc, bg, .93)
    for gx in range(0, W, 54):
        c.d.line((c.k(gx), 0, c.k(gx), c.k(c.H)), fill=grid, width=c.k(1))
    for gy in range(0, c.H, 54):
        c.d.line((0, c.k(gy), c.k(W), c.k(gy)), fill=grid, width=c.k(1))
    for (bx, by, sx, sy) in ((26, 26, 1, 1), (W - 26, 26, -1, 1), (26, c.H - 26, 1, -1), (W - 26, c.H - 26, -1, -1)):
        c.d.line((c.k(bx), c.k(by), c.k(bx + 54 * sx), c.k(by)), fill=acc, width=c.k(4))
        c.d.line((c.k(bx), c.k(by), c.k(bx), c.k(by + 54 * sy)), fill=acc, width=c.k(4))
    px = 70
    c.logo(logo, px, 64, 70, 16)
    c.text((px + 90, 76), "// ECON.CALENDAR  ·  LIVE", 17, True, acc, "lm", fam="mono")
    t, sz = c.fit(title.upper(), 36, True, "mono", 640, 22)
    c.text((px + 90, 116), t, sz, True, (240, 248, 255), "lm", fam="mono")
    wd, mon, day = _date_parts(rows)
    c.text((W - px, 76), f"[ {len(rows):02d} EVENTS ]", 18, True, mut, "rm", fam="mono")
    c.text((W - px, 116), _hud_date(wd, mon, day), 26, True, acc, "rm", fam="mono")
    c.text((px, 168), f"{_tzt('mono')}  ·  SRC FOREXFACTORY", 15, False, mut, "lm", fam="mono")
    c.line((px, 190), (W - px, 190), acc, 2, 150)
    labs = (("TIME", px + 22, "l"), ("CUR", 214, "l"), ("EVENT", 300, "l"), ("IMPACT", 628, "l"),
            ("FCST", 790, "m"), ("PREV", 872, "m"), ("ACT", 950, "m"))
    for tx_, x, a in labs:
        c.text((x, 220), tx_, 14, True, mut, a + "m", fam="mono", track=1)
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
    c.text((px, c.H - 62), f"> {label.upper()}  |  BY {bot.upper()}", 16, False, mut, "lm", fam="mono")
    c.rrect((W - px - 16, c.H - 72, W - px, c.H - 52), 2, fill=acc)
    return c.done()


# ═══════════════  5 · TICKET  ═══════════════
def _ticket(rows, title, logo, brand, label, bot):
    h, s, _ = _hls(brand)
    s = max(.5, min(s, .85))
    head, th, gap, foot = 230, 116, 28, 140
    c = Cv(head + max(len(rows), 1) * (th + gap) + foot)
    c.gradient(_rgb(h, .5, .12), _rgb(h + .05, .5, .06), diag=True)
    c.glow(900, 60, 380, _rgb(h, .9, .45), 80)
    c.glow(100, c.H - 40, 320, _rgb(h + .1, .8, .4), 60)
    acc = _rgb(h, .9, .70)
    paper, ink, mut = _rgb(h, .30, .965), _rgb(h, .5, .10), _rgb(h, .12, .48)
    wd, mon, day = _date_parts(rows)
    c.logo(logo, 48, 50, 84, 22)
    c.text((158, 66), "ECONOMIC CALENDAR  ·  DAILY PASS", 17, True, acc, "lm", track=3)
    t, sz = c.fit(title, 46, True, "sans", 640, 30)
    c.text((158, 112), t, sz, True, (255, 255, 255), "lm")
    c.text((158, 160), _dline(wd, mon, day, "plain", long=True),
           23, False, _rgb(h, .5, .80), "lm")
    c.text((W - 48, 66), "ADMIT", 15, True, acc, "rm", track=4)
    c.text((W - 48, 112), _n(f"{len(rows):02d}"), 52, True, (255, 255, 255), "rm")
    c.text((W - 48, 160), "EVENTS", 15, True, acc, "rm", track=4)
    y = head
    for r0 in rows:
        r = _row(r0)
        x0, x1, sx = 48, W - 48, 272
        bgp = lambda px_, py_: c.img.getpixel((c.k(px_), c.k(py_)))  # noqa: E731
        c.rrect((x0, y, x1, y + th), 22, fill=paper, shadow=((0, 0, 0, 120), 16, 10))
        for nx_, ny_, rad in ((sx, y, 12), (sx, y + th, 12), (x0, y + th / 2, 12), (x1, y + th / 2, 12)):
            c.circle(nx_, ny_, rad, fill=bgp(nx_, y - 24 if ny_ == y else (y + th + 22 if ny_ == y + th else y - 24)))
        for dy in range(24, th - 14, 14):
            c.rrect((sx - 1.5, y + dy, sx + 1.5, y + dy + 7), 1.5, fill=_rgb(h, .2, .78))
        col = IMPACT.get(r["imp"], IMPACT["High"])
        c.text(((x0 + sx) / 2, y + 44), r["time"], 40, True, ink, "mm")
        cw = c.tw(r["cur"], 19, True) + 30
        c.pill((x0 + sx) / 2 - cw / 2, y + 74, cw, 30, _rgb(h, s, .40))
        c.text(((x0 + sx) / 2, y + 89), r["cur"], 19, True, (255, 255, 255), "mm")
        name, nsz = c.fit(r["name"], 29, True, "sans", 345, 21)
        c.text((sx + 34, y + 44), name, nsz, True, ink, "lm")
        c.circle(sx + 40, y + 84, 6, fill=col)
        c.text((sx + 54, y + 84), r["imp"].upper() + " IMPACT", 14, True, _mix(col, ink, .3), "lm", track=2)
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
    # barcode
    rnd = random.Random(f"{mon}{day}{len(rows)}")
    bx, by = 48, c.H - 112
    while bx < 400:
        wbar = rnd.choice((2, 2, 3, 5))
        c.d.rectangle((c.k(bx), c.k(by), c.k(bx + wbar), c.k(by + 44)), fill=(240, 244, 255))
        bx += wbar + rnd.choice((3, 4, 6))
    c.text((48, c.H - 36), f"Source: ForexFactory  ·  {label}  |  by {bot}", 18, False, _rgb(h, .4, .75), "lm")
    c.text((W - 48, c.H - 90), "NO. " + f"{abs(hash((mon, day))) % 900000 + 100000}", 17, True, acc, "rm", track=3)
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
    c.logo(logo, 48, 44, 72, 20)
    t, sz = c.fit(title, 36, True, "sans", 560, 26)
    c.text((140, 66), t, sz, True, (255, 255, 255), "lm")
    c.text((140, 104), _dline(wd, mon, day, "sub"), 21, False,
           (255, 255, 255), "lm")
    n = f"{len(rows)} events"
    pw = c.tw(n, 19, True) + 36
    c.pill(W - 48 - pw, 50, pw, 42, (255, 255, 255, 38), (255, 255, 255, 90))
    c.text((W - 48 - pw / 2, 71), n, 19, True, (255, 255, 255), "mm")
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
        col = IMPACT.get(r["imp"], IMPACT["High"])
        iw = c.tw(r["imp"].upper() + " IMPACT", 15, True, track=2) + 52
        c.pill(W - 88 - iw, head + 34, iw, 40, col + (255,))
        c.text((W - 88 - iw / 2 + 8, head + 54), r["imp"].upper() + " IMPACT", 15, True, (255, 255, 255), "mm", track=2)
        c.circle(W - 88 - iw + 22, head + 54, 5, fill=(255, 255, 255))
        c.text((84, head + 150), r["time"], 150, True, (255, 255, 255), "lm")
        lines = c.wrap(r["name"], 46, True, "sans", W - 200, 2)
        for i, ln in enumerate(lines):
            c.text((88, head + 262 + i * 50), ln, 46 if len(lines) == 1 else 40, True, (255, 255, 255), "lm")
        sy = head + hero_h - 52
        tone = _tone(r0, True)
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
            c.circle(194 + cw + 16, cy, 5, fill=IMPACT.get(r["imp"], IMPACT["High"]))
            nm, nsz = c.fit(r["name"], 25, True, "sans", 640 - (194 + cw + 36), 17)
            c.text((194 + cw + 32, cy), nm, nsz, True, (255, 255, 255), "lm")
            tone = _tone(r0, True)
            for val, cx in ((r["fc"], 730), (r["pv"], 840)):
                c.text((cx, cy), val or "–", 23, True, (255, 255, 255) if val else (210, 220, 240), "mm")
            c.text((955, cy), r["act"] or "—", 25, True, tone or ((255, 255, 255) if r["act"] else (210, 220, 240)), "mm")
    c.text((W / 2, c.H - 42), f"Source: ForexFactory  ·  {label}  |  by {bot}", 19, False, (255, 255, 255), "mm")
    return c.done()


RENDERERS = {"timeline": _timeline, "bento": _bento, "editorial": _editorial, "hud": _hud, "ticket": _ticket,
             "spotlight": _spotlight}


# ───────────────  public API  ────────────────
def render_image(records, style="timeline", title="Economic Calendar", logo=None, bot_name=BOT_NAME,
                 impact_label="High impact", max_rows=12, brand=None, tz="ny", language="en") -> Image.Image:
    """tz = a zone name ('Asia/Dubai') or the old keys 'ny' / 'tehran'; language 'fa' = Persian labels (needs libraqm,
    otherwise the picture stays English). Event names, currencies and values are never translated."""
    logo_img = _load_img(logo) if logo is not None else _load_img(DEFAULT_LOGO)
    fn = RENDERERS.get(style, _timeline)
    lang = "fa" if (language == "fa" and RTL_OK) else "en"
    t1, t2, t3 = _TZ.set(tz or "ny"), _LANG.set(lang), _ZT.set(_zone_text(tz or "ny", lang))
    try:
        return fn(records[:max_rows], title, logo_img, brand or brand_color(logo_img), impact_label, bot_name)
    finally:
        _ZT.reset(t3)
        _LANG.reset(t2)
        _TZ.reset(t1)


def render_calendar(records, style="timeline", title="Economic Calendar", logo=None, bot_name=BOT_NAME,
                    impact_label="High impact", max_rows=12, tz="ny", language="en") -> bytes:
    out = io.BytesIO()
    render_image(records, style, title, logo, bot_name, impact_label, max_rows, tz=tz, language=language).save(
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


def _assemble_sheet(cells: list, cell_w: int = 640, cols: int = 2) -> Image.Image:
    """Numbered contact sheet of the 6 designs (names from STYLE_NAMES)."""
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
            d.text((x + 84, y + 37), STYLE_NAMES[STYLE_KEYS[i]], font=_font(34, True), fill=(235, 238, 247), anchor="lm")
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


def render_sheet(logo=None, cell_w: int = 640, cols: int = 2, progress=None, language: str = "en", tz: str = "ny") -> bytes:
    """📰 NEWS: one picture with all 6 news designs, numbered 1..6, built from the same logo."""
    logo_img = _load_img(logo) if logo is not None else _load_img(DEFAULT_LOGO)
    brand = brand_color(logo_img)
    steps = len(STYLE_KEYS) + 1              # 6 designs + final assembly
    cells = []
    for n, key in enumerate(STYLE_KEYS, 1):
        cells.append(render_image(_sample_for(tz)[:4], key,
                                  "اخبار مهم روزانه" if (language == "fa" and RTL_OK) else "Daily High-Impact News",
                                  logo_img, brand=brand, tz=tz, language=language,
                                  impact_label="اهمیت بالا" if (language == "fa" and RTL_OK) else "High impact"))
        if progress:
            progress(n, steps)
    png = _sheet_png(_assemble_sheet(cells, cell_w, cols))
    if progress:
        progress(steps, steps)
    return png


# ═══════════════  DATE CARD (▦ today's calendar)  ═══════════════
# Same 6 designs, same logo/brand colour as the news picture. The caller (telegram_bot) builds the `model`:
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
    T = _date_theme(style, brand)
    rtl = bool(m.get("rtl"))
    fam = "fa" if rtl else T["fam"]
    up = (lambda s: s) if rtl else (lambda s: s.upper())
    c = Cv(DATE_H)
    T["bg"](c)

    def txt(xy, s, size, bold=False, fill=None, anchor="lm", track=0):
        return c.text(xy, s, size, bold, fill or T["fg"], anchor, fam=fam, track=0 if rtl else track)

    def card(box, fill=True):
        c.rrect(box, T["rad"], fill=T["card"] if fill else None, outline=T["line"], shadow=T["shadow"])

    # ── header ──
    c.logo(logo, 48, 48, 92, 26)
    t, sz = c.fit(m["title"], 46, True, fam, 560, 30)
    txt((166, 78), t, sz, True, T["head_fg"])
    txt((166, 126), m["sub"], 22, False, T["head_mut"], track=1)
    if m.get("kind"):                                  # empty = no label (a Persian calendar needs no "Persian" tag)
        kw = c.tw(up(m["kind"]), 17, True, fam, track=0 if rtl else 2) + 40
        c.pill(W - 48 - kw, 62, kw, 42, (255, 255, 255, 30) if style in ("timeline", "hud", "ticket", "spotlight") else T["acc"] + (30,),
               (255, 255, 255, 60) if style in ("timeline", "hud", "ticket", "spotlight") else T["acc"] + (90,))
        txt((W - 48 - kw / 2, 83), up(m["kind"]), 17, True,
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
    txt((48, fy), f"by {bot}", 18, False, T["head_mut"])
    txt((W - 48, fy), m["sub"], 18, False, T["head_mut"], "rm")
    return c.done()


def render_date_card(model: dict, style: str = "timeline", logo=None, bot_name: str = BOT_NAME,
                     brand=None) -> bytes:
    logo_img = _load_img(logo) if logo is not None else _load_img(DEFAULT_LOGO)
    out = io.BytesIO()
    _date_card(model, style if style in STYLE_KEYS else STYLE_KEYS[0], logo_img,
               brand or brand_color(logo_img), bot_name).save(out, "PNG", optimize=True)
    return out.getvalue()


def render_date_sheet(model: dict, logo=None, cell_w: int = 640, cols: int = 2, progress=None) -> bytes:
    """▦ CALENDAR: one picture with all 6 DATE-CARD designs (today's date, not the news), numbered 1..6."""
    logo_img = _load_img(logo) if logo is not None else _load_img(DEFAULT_LOGO)
    brand = brand_color(logo_img)
    steps = len(STYLE_KEYS) + 1
    cells = []
    for n, key in enumerate(STYLE_KEYS, 1):
        cells.append(_date_card(model, key, logo_img, brand, BOT_NAME))
        if progress:
            progress(n, steps)
    png = _sheet_png(_assemble_sheet(cells, cell_w, cols))
    if progress:
        progress(steps, steps)
    return png


if __name__ == "__main__":
    Path("previews").mkdir(exist_ok=True)
    Path("previews/sheet.png").write_bytes(render_sheet(DEFAULT_LOGO))
    for key in STYLE_KEYS:
        Path(f"previews/{key}.png").write_bytes(render_calendar(SAMPLE, key, "Daily High-Impact News", DEFAULT_LOGO))
    print("ok")
