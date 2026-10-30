# Forex Factory

Flask API for ForexFactory economic calendar news, with an optional Telegram bot and optional domain + HTTPS (Nginx + Let's Encrypt).

Installed to `/opt/forexfactory-api`. Live data is stored in PostgreSQL (installed and set up automatically by the installer).

## Install

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Mahersaber2024/ForexFactoryNews/main/scripts/install.sh)
```

Modes:

1. **Flask only**: the API on this server, no bot.
2. **Flask + Telegram Bot**: both on this server.
3. **Telegram Bot only**: the bot on this server, pointed at a Flask API running on another server.

The installer asks for the Flask port, an optional domain and SSL email (modes 1 and 2), the bot token and admin chat ID (modes 2 and 3), and the remote API URL (mode 3). Without a domain, the site and API are served over HTTP on the server IP.

For the bot it also asks about PostgreSQL: option 1 installs and configures it automatically, option 2 uses an existing server.

## Manage

Status, conflict check, install, update and uninstall in one tool:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Mahersaber2024/ForexFactoryNews/main/manager.sh)
```

Or directly: `bash manager.sh status | doctor | install forex | update forex | uninstall forex`.

Services:

```bash
sudo systemctl status forexfactory-api.service forexfactory-bot.service
sudo systemctl restart forexfactory-api.service forexfactory-bot.service
sudo journalctl -u forexfactory-bot.service -n 100 --no-pager
```

Settings (`.env`):

```bash
cd /opt/forexfactory-api
.venv/bin/python bot_settings.py            # interactive wizard
.venv/bin/python bot_settings.py show
.venv/bin/python bot_settings.py set KEY=VALUE
```

Re-check the database: `.venv/bin/python -m db.setup_db`

## Uninstall

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Mahersaber2024/ForexFactoryNews/main/scripts/uninstall.sh)
```

It offers a final backup to `/root`, then removes the services, the Nginx config, and the project files.
