# bot/fonts

Fonts used to draw the calendar / news pictures. All are free fonts (OFL or the DejaVu licence).

| File | Used for |
|------|----------|
| Carlito-Regular/Bold.ttf | English sans text |
| LiberationSerif-Regular/Bold.ttf | the "Editorial" design |
| DejaVuSansMono(-Bold).ttf | the "HUD" design |
| DejaVuSans(-Bold).ttf | last-resort fallback (also for Persian) |
| **Vazirmatn-Regular.ttf, Vazirmatn-Bold.ttf** | **Persian text** (not in git, see below) |

Vazirmatn is downloaded automatically by `scripts/install.sh` and by the bot at start-up (`bot/font_setup.py`).
If the server has no internet access, put the two files here by hand:
https://github.com/rastikerdar/vazirmatn/releases (folder `fonts/ttf`), then restart the bot.
Or: `sudo apt install fonts-vazirmatn` (the bot also looks in /usr/share/fonts).
