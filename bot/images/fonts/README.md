# bot/images/fonts

Fonts used to draw the calendar / news pictures. All are free fonts (OFL or the DejaVu licence).

| File | Used for |
|------|----------|
| Carlito-Regular/Bold.ttf | English sans text |
| LiberationSerif-Regular/Bold.ttf | the "Editorial" design |
| DejaVuSansMono(-Bold).ttf | the "HUD" design |
| DejaVuSans(-Bold).ttf | last-resort fallback (also for Persian) |
| **Vazirmatn-Regular.ttf, Vazirmatn-Bold.ttf** | **Persian text** (not in git, see below) |
| NotoSans-Regular/Bold.ttf | Cyrillic and Latin scripts, including Vietnamese |
| NotoSansDevanagari-Regular/Bold.ttf | Devanagari script |
| ForexLocalSans-Regular/Bold.ttf | Hindi/Latin mixed-script text (derived from the two Noto families above) |

Vazirmatn is downloaded automatically by `scripts/install.sh` and by the bot at start-up (`bot/images/font_setup.py`).
If the server has no internet access, put the two files here by hand:
https://github.com/rastikerdar/vazirmatn/releases (folder `fonts/ttf`), then restart the bot.
Or: `sudo apt install fonts-vazirmatn` (the bot also looks in /usr/share/fonts).

The Noto fonts and the renamed ForexLocalSans derivatives are distributed under
the SIL Open Font License 1.1; see `NOTO-LICENSE.txt`. Image rendering for
Hindi requires a Pillow build with libraqm. The font files alone do not enable
new languages in the bot interface.
