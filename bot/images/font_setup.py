"""
font_setup.py: makes sure a real Persian font (Vazirmatn) is in bot/images/fonts/.

    python -m bot.images.font_setup          # run from the project folder (install.sh does this)
    font_setup.ensure_persian_fonts() # called by the bot at start-up, in the background

Downloads Vazirmatn-Regular.ttf / Vazirmatn-Bold.ttf (SIL OFL licence) from a few mirrors, checks that each file
really is a font with the Persian letters, and only then moves it into place. Never raises: on any failure the
pictures simply keep using the fallback chain in calendar_image.py (DejaVu is the last resort).
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

FONT_DIR = Path(__file__).parent / "fonts"
FILES = ("Vazirmatn-Regular.ttf", "Vazirmatn-Bold.ttf")
MIRRORS = (
    "https://cdn.jsdelivr.net/gh/rastikerdar/vazirmatn@master/fonts/ttf/{name}",
    "https://raw.githubusercontent.com/rastikerdar/vazirmatn/master/fonts/ttf/{name}",
    "https://cdn.jsdelivr.net/npm/vazirmatn@33.0.3/fonts/ttf/{name}",
    "https://github.com/rastikerdar/vazirmatn/raw/master/fonts/ttf/{name}",
)
PERSIAN_LETTERS = "ابپتثجچحخدذرزژسشصضطظعغفقکگلمنوهیء"
MAX_BYTES = 8 * 1024 * 1024


def _is_good_font(path: Path) -> bool:
    """A real TrueType/OpenType file that has all the Persian letters."""
    try:
        from PIL import ImageFont
        ImageFont.truetype(str(path), 20)                      # loads at all?
        try:
            from fontTools.ttLib import TTFont                 # optional (only for the glyph check)
            cmap = TTFont(str(path), lazy=True).getBestCmap()
            return all(ord(ch) in cmap for ch in PERSIAN_LETTERS)
        except ImportError:
            return path.stat().st_size > 50_000
    except Exception:
        return False


def _fetch(url: str, timeout: int = 25) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (font-setup)"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(MAX_BYTES + 1)
        return data if 50_000 < len(data) <= MAX_BYTES else None
    except Exception as e:
        log.info("font download failed (%s): %s", url, e)
        return None


def missing() -> list[str]:
    return [n for n in FILES if not (FONT_DIR / n).exists() or not _is_good_font(FONT_DIR / n)]


def ensure_persian_fonts(verbose: bool = False) -> bool:
    """True when Vazirmatn Regular + Bold are in bot/images/fonts/ (already there or just downloaded)."""
    try:
        FONT_DIR.mkdir(parents=True, exist_ok=True)
        for name in missing():
            done = False
            for tpl in MIRRORS:
                data = _fetch(tpl.format(name=name))
                if not data:
                    continue
                with tempfile.NamedTemporaryFile(dir=FONT_DIR, suffix=".part", delete=False) as tmp:
                    tmp.write(data)
                tmp_path = Path(tmp.name)
                if _is_good_font(tmp_path):
                    os.replace(tmp_path, FONT_DIR / name)
                    done = True
                    if verbose:
                        print(f"  ok    {name}  <- {tpl.split('/{name}')[0]}")
                    break
                tmp_path.unlink(missing_ok=True)
            if not done and verbose:
                print(f"  FAIL  {name}  (no mirror worked)")
    except Exception as e:                                      # never break the bot over a font
        log.warning("font setup error: %s", e)
        return False
    ok = not missing()
    if ok:
        try:
            from bot.images import calendar_image
            calendar_image.reset_font_cache()                   # pick the new font up without a restart
        except Exception:
            pass
    return ok


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    print("Persian font (Vazirmatn):")
    good = ensure_persian_fonts(verbose=True)
    print("ready." if good else
          "NOT installed: put Vazirmatn-Regular.ttf and Vazirmatn-Bold.ttf into bot/images/fonts/ by hand "
          "(https://github.com/rastikerdar/vazirmatn/releases) or run: apt install fonts-vazirmatn")
    sys.exit(0)
