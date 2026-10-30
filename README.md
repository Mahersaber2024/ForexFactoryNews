# Forex Factory

A lightweight Flask API for ForexFactory high-impact news, with optional Telegram bot support and optional domain-based HTTPS access for external clients such as MetaTrader 5.

Sponsor: [@HeySoloATM](https://t.me/HeySoloATM)

## Features

- Flask API for ForexFactory economic calendar data
- Optional Telegram bot
- Public API access through server IP
- Optional domain setup with Nginx reverse proxy
- Optional HTTPS with Let's Encrypt and Certbot
- Interactive installer with systemd service setup
- Flask API and Telegram bot can run on **separate servers**; the bot admin can switch which Flask API the bot uses at runtime, without reinstalling
- **Full backup & restore from inside the bot** (`/admin` → 💾 / ♻️)
- One config tool, `bot_settings.py`, builds and edits `.env`
- **Website** ships with the project (`website/content`) and is served on the same domain as the API
- **EA versions from the bot**: upload new `.ex5` builds to the download page, or delete old ones
- **📢 Send to my channel**: users can get the daily digest in their own Telegram channel

## Project structure

```text
forexfactory-api/
├── bot_settings.py          → builds/edits .env, shared config for everything
├── requirements.txt
├── README.md
├── api/                     → Flask API
│   ├── flask_server.py
│   └── forexFactoryScrapper.py
├── bot/                     → Telegram bot
│   ├── telegram_bot.py
│   ├── backup_manager.py    → full backup / restore logic
│   ├── ea_admin.py          → /admin → 📦 EA versions (upload / delete builds)
│   └── channels.py          → 📢 users' own channel for the daily digest
├── website/
│   ├── content/             → the site (index.html, downloadpage.html, assets/...), served by nginx
│   │   └── assets/downloads/ → EA builds (.ex5 uploaded from the bot, not in git)
│   └── nginx/               → nginx template (site + /api/) + rate-limit zone
├── scripts/
│   ├── install.sh
│   └── uninstall.sh
├── .env                     → runtime config (created by bot_settings.py)
└── data/                    → runtime data (not in git)
    ├── forexbot.db
    └── backups/
```

Installed to `/opt/forexfactory-api`. `Dockerfile` and `envshowrun` were removed; `bot_settings.py` replaces them.

## Quick Install

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Mahersaber2024/ForexFactoryNews/main/scripts/install.sh)
```

## Manager (status / conflict check / install / update / uninstall)

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Mahersaber2024/ForexFactoryNews/main/manager.sh)
```

## Installer Prompts

The installer supports three modes:

1. **Flask only** — runs just the Flask API on this server (no bot). Use this on the server that hosts the data, e.g. `iran.heysolo.ir`.
2. **Flask + Telegram Bot** — both run together on the same server.
3. **Telegram Bot only** — runs just the bot on this server, and points it at a Flask API running on a *different* server. Use this when the bot server and the Flask API server are separate machines.

Depending on the mode chosen, it asks for:

- Flask port (modes 1 and 2)
- Optional domain for the site + API over HTTPS, e.g. `heysolo.online` (modes 1 and 2)
- SSL email address if a domain is provided (modes 1 and 2)
- Telegram bot token (modes 2 and 3)
- Telegram admin chat ID (modes 2 and 3)
- The remote Flask API base URL, e.g. `https://iran.heysolo.ir/api/forex` (mode 3 only)
- Whether to send a restart notification to users (modes 2 and 3)

In modes 1 and 2 the **website and the API share one domain**: `https://DOMAIN/` is the site, `https://DOMAIN/api/forex/...` is the API. There's no separate website question.

If you press Enter on the domain question, the site and API are served on the server IP over HTTP.

## API Endpoints

- `/api/forex/yesterday`
- `/api/forex/today`
- `/api/forex/tomorrow`
- `/api/forex/weekly`

Example:

```bash
curl http://127.0.0.1:45869/api/forex/today
```

## API Access Modes

### Local access

```text
http://127.0.0.1:45869/api/forex/today
```

### Public access by server IP

If Nginx is installed and running on port 80, the API can be accessed from outside the server using:

```text
http://YOUR_SERVER_IP/api/forex/today
```

Example:

```text
http://185.28.119.231/api/forex/today
```

### Public access by domain

If you provide a domain during installation and its DNS points to your server, the installer configures Nginx and tries to obtain an SSL certificate automatically.

Example:

```text
https://news.example.com/api/forex/today
```

## MetaTrader 5

If you are using direct server IP access:

```mql5
input string NewsURL = "http://YOUR_SERVER_IP";
```

If you are using a domain with HTTPS:

```mql5
input string NewsURL = "https://news.example.com";
```

Then your EA can request:

```text
/api/forex/today
```

## Services

> **Upgrading from an older version?** The services used to be called `flask.service` and `telegram-bot.service`.
> Run this once on the server (it only touches this project's own units; data and `.env` stay as they are):
>
> ```bash
> sudo bash /opt/forexfactory-api/scripts/rename-services.sh
> ```
>
> `manager.sh update forex` and `scripts/install.sh` do it automatically.

The installer creates these systemd services (depending on the mode chosen):

- `forexfactory-api.service`
- `forexfactory-bot.service` only if bot mode is enabled

Check service status:

```bash
sudo systemctl status forexfactory-api.service
sudo systemctl status forexfactory-bot.service
```

Restart services:

```bash
sudo systemctl restart forexfactory-api.service
sudo systemctl restart forexfactory-bot.service
```

View logs:

```bash
sudo journalctl -u forexfactory-api.service -n 100 --no-pager
sudo journalctl -u forexfactory-bot.service -n 100 --no-pager
```

## Running Flask and the Bot on Separate Servers

You don't have to run the Flask API and the Telegram bot on the same machine. A common setup:

- **Server A (data server)**: run the installer, choose mode **1) Flask only**, and give it the domain `iran.heysolo.ir`. This exposes:

```text
https://iran.heysolo.ir/api/forex/yesterday
https://iran.heysolo.ir/api/forex/today
https://iran.heysolo.ir/api/forex/tomorrow
https://iran.heysolo.ir/api/forex/weekly
```

- **Server B (bot server)**: run the installer, choose mode **3) Telegram Bot only**, and when asked for the remote Flask API base URL, enter:

```text
https://iran.heysolo.ir/api/forex
```

Server B only installs Python, the venv, and the `forexfactory-bot.service` — no Flask, no Nginx.

### Letting the admin switch Flask servers from inside the bot

Once the bot is running, the Telegram user whose chat ID matches `ADMIN_CHAT_ID` can change which Flask API the bot talks to at any time, straight from a button menu — no redeploy, no restart needed.

Just send `/admin` in the bot and everything is done with buttons:

- **🔀 Switch server** — shows all saved servers, tap one to make it active instantly
- **➕ Add server** — bot asks for `name https://url`, you reply with one message and it's saved
- **🗑 Delete server** — tap a saved server to remove it

There's also a standalone `/myid` command that shows your chat ID, useful the first time you're setting `ADMIN_CHAT_ID`.

The active URL is stored in the bot's local SQLite database (`data/forexbot.db`), so it survives bot restarts, and it overrides the `API_BASE_URL` value from `.env` once set. Only the configured `ADMIN_CHAT_ID` can use these commands; everyone else is ignored.

This is useful if you run several Flask servers (e.g. one in Iran, one elsewhere) and want to fail over or switch between them without touching the server.

## Backup & Restore (inside the bot)

Send `/admin` and use the buttons:

- **💾 Full backup**: the bot creates a `.zip` and sends it to you. It contains `forexbot.db` (all users, subscriptions, currencies, saved servers, active server) + `settings.env` (your `.env`) + `manifest.json`. ⚠️ It contains the bot token, keep it private.
- **♻️ Restore**: tap it, then send the backup `.zip` (or an old `forexbot.db`) as a file. The bot validates it first (zip integrity, SQLite integrity check, required tables) and shows a summary. Then pick:
  - **Restore data**: only the database
  - **Data + settings**: database + `.env`; the bot restarts itself (systemd brings it back)
- **🗂 Saved backups**: backups stored on the server (`data/backups/`, last `BACKUP_KEEP` kept). Send one to yourself or restore it.

Before every restore the bot automatically saves a **pre-restore** backup, so a wrong restore can be undone from 🗂 Saved backups. Max file size is 20 MB (Telegram bot API limit).

Moving to a new server: back up on the old bot → install on the new server → `/admin` → ♻️ Restore → send the file → **Data + settings**.

## Website

The site is part of this repo (`website/content`), so it's updated with the rest of the project (`git pull`). nginx serves it straight from `/opt/forexfactory-api/website/content` together with the API (template: `website/nginx/site.conf.template`, based on the original heysolo.online config; HTTPS is added by certbot).

### EA versions from the bot

`/admin` → **📦 EA versions**:

- **➕ Upload new version**: send `.ex5` / `.ex4` files, they go to `assets/downloads/` and show up on `/download` right away. Same name = replaced
- The list shows every build on the site, tap one to **delete** it
- Shortcut: as admin, just send a `.ex5` to the bot
- Names must match the download page, e.g. `HeySolo[ATM]v4.2.ex5`, `HeySolo[ATM]_PROPv4.2.ex5`, `Heysolo-SecTMv1.0.ex5`, `Heysolo-ForwardTester_v1.0.ex5` (the bot warns otherwise)
- Max 20 MB per file (Telegram bot limit)

Uploaded builds are ignored by git (`.gitignore`) and included in 💾 Full backup / ♻️ Restore. Works in mode 2 (Flask + bot on the same server, where the site is hosted).

Note: the nginx config blocks `curl`, `wget`, `python-requests` user agents (from the original config). Test with a browser or `curl -A Mozilla ...`.

## Send to my channel (users)

In the bot menu, **📢 Send to my channel**:

1. The user adds **@ForexFactoryN_Bot** to their channel as **admin** (with "Post messages")
2. Sends the channel username (`@mychannel` or a `t.me/...` link), or forwards any post from the channel (private channels)

The bot checks it's admin there and that the user is an admin of that channel, then the daily digest (same currencies and time) is posted in the channel instead of the private chat. If the bot is later removed from the channel, the user is told and the digest falls back to the private chat. Connecting a channel turns the daily digest on.

## Settings (`bot_settings.py`)

```bash
cd /opt/forexfactory-api
.venv/bin/python bot_settings.py                      # interactive wizard, offers to restart services
.venv/bin/python bot_settings.py show                 # show config (token masked)
.venv/bin/python bot_settings.py get API_BASE_URL
.venv/bin/python bot_settings.py set SEND_RESTART_MSG=true BACKUP_KEEP=20
.venv/bin/python bot_settings.py run bot              # run the bot in the foreground (debugging)
```

After `set`, restart the service: `sudo systemctl restart forexfactory-bot.service` (or `forexfactory-api.service`).

## Domain Setup Later

If you skip the domain during installation, you can add it later.

### 1. Point DNS to your server

Create an `A` record for your domain or subdomain and point it to your server IP.

Example:

- `news.example.com` → `185.28.119.231`

### 2. Install Nginx and Certbot

```bash
sudo apt update
sudo apt install -y nginx certbot python3-certbot-nginx
```

### 3. Create Nginx config
Replace `news.example.com` with your real domain and `45869` with your Flask port if different.
```bash
cd /opt/forexfactory-api
sudo sed -e "s|__DOMAIN__|news.example.com|g" \
         -e "s|__ROOT__|/opt/forexfactory-api/website/content|g" \
         -e "s|__FLASK_PORT__|45869|g" \
         website/nginx/site.conf.template | sudo tee /etc/nginx/sites-available/forexfactory-api > /dev/null
sudo cp website/nginx/ratelimit.conf /etc/nginx/conf.d/forexfactory-ratelimit.conf
```
Enable it:
```bash
sudo ln -sf /etc/nginx/sites-available/forexfactory-api /etc/nginx/sites-enabled/forexfactory-api
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl restart nginx
```
(Or simply re-run the installer and enter the domain.)
### 4. Get SSL certificate

If the certificate is not created yet, run:

```bash
sudo certbot --nginx -d news.example.com
```

After successful issuance, your API should be available at:

```text
https://news.example.com/api/forex/today
```

### 5. Optional: update `.env`

If you want to store the public base URL in the app config:

```bash
cd /opt/forexfactory-api
.venv/bin/python bot_settings.py set PUBLIC_BASE_URL=https://news.example.com
```

Then restart Flask:

```bash
sudo systemctl restart forexfactory-api.service
```

## Troubleshooting

### Flask is not starting

Check logs:

```bash
sudo journalctl -u forexfactory-api.service -n 100 --no-pager
```

### Telegram bot is running but news is not received

Make sure Flask is active first, because the bot depends on the API.

### HTTPS does not open

Check these items:

- Domain DNS is pointed to the correct server IP
- Port 443 is open
- Nginx is listening on 443
- No other service is using port 443
- SSL certificate files exist in `/etc/letsencrypt/live/news.example.com/`

Then test again:

```bash
sudo ss -tlnp | grep ':443'
curl -A Mozilla -I https://news.example.com/api/forex/today
```

## Uninstall

To completely remove the application and all installed services:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Mahersaber2024/ForexFactoryNews/main/scripts/uninstall.sh)
```

The uninstall script will:
- Offer to save a final full backup to `/root` first

- Remove `forexfactory-api.service`
- Remove `forexfactory-bot.service` (if installed)
- Remove Nginx configuration
- Remove project files and virtual environment
- Optionally remove Nginx, Certbot, Git and Python packages

After removal, verify:

```bash
systemctl status forexfactory-api.service
systemctl status forexfactory-bot.service
```

Expected output:

```text
Unit forexfactory-api.service could not be found.
Unit forexfactory-bot.service could not be found.
```

## Notes

- This setup uses HTTPS only on port 443.
- Do not paste Markdown links inside Nginx config. Use plain text only.
- If you do not need the Telegram bot, choose Flask only during install.
- If Flask runs on a different server than the bot, choose Telegram Bot only during install and point it at the remote API URL — see "Running Flask and the Bot on Separate Servers" above.
