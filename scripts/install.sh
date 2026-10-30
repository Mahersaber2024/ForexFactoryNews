#!/usr/bin/env bash
set -e

APP_DIR="/opt/forexfactory-api"
REPO_URL="https://github.com/Mahersaber2024/ForexFactoryNews.git"
SERVICE_USER="root"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo "1) Flask only  (run the API on this server, no bot)"
echo "2) Flask + Telegram Bot  (both on this same server)"
echo "3) Telegram Bot only  (bot on this server, Flask API runs elsewhere)"
echo "4) Uninstall  (remove ForexFactory API / Bot from this server)"
read -p "Choose install mode [1/2/3/4]: " INSTALL_MODE

if [ "$INSTALL_MODE" = "4" ]; then
  UNINSTALL_SCRIPT="$APP_DIR/scripts/uninstall.sh"
  if [ -f "$UNINSTALL_SCRIPT" ]; then
    bash "$UNINSTALL_SCRIPT"
  else
    bash <(curl -fsSL "https://raw.githubusercontent.com/Mahersaber2024/ForexFactoryNews/main/scripts/uninstall.sh")
  fi
  exit $?
fi

RUN_FLASK=true
RUN_BOT=false
FLASK_PORT="45869"
DOMAIN_NAME=""
CERTBOT_EMAIL=""
TELEGRAM_BOT_TOKEN=""
ADMIN_CHAT_ID="0"
SEND_RESTART_MSG="false"
REMOTE_API_BASE_URL=""

if [ "$INSTALL_MODE" = "3" ]; then
  RUN_FLASK=false
fi

# ---------------- Flask questions (modes 1 and 2) ----------------
if [ "$RUN_FLASK" = "true" ]; then
  read -p "Enter Flask port [default: 45869]: " FLASK_PORT
  FLASK_PORT=${FLASK_PORT:-45869}

  # Port must be free (or already used by this project's own forexfactory-api.service).
  if command -v ss >/dev/null 2>&1 && ss -H -ltn "sport = :$FLASK_PORT" 2>/dev/null | grep -q .; then
    if ! grep -qs "^WorkingDirectory=$APP_DIR/\?$" /etc/systemd/system/forexfactory-api.service /etc/systemd/system/flask.service; then
      echo -e "${RED}Port $FLASK_PORT is already used by another program. Nothing was changed.${NC}"
      echo "Re-run and choose a different Flask port (or free this one)."
      exit 1
    fi
  fi
  # Never overwrite a unit that belongs to another app.
  if [ -f /etc/systemd/system/forexfactory-api.service ] && ! grep -qs "^WorkingDirectory=$APP_DIR/\?$" /etc/systemd/system/forexfactory-api.service; then
    echo -e "${RED}/etc/systemd/system/forexfactory-api.service already belongs to another application. Nothing was changed.${NC}"
    exit 1
  fi

  echo -e "${CYAN}The website (website/content) and the API are served together on one domain:${NC}"
  echo "  https://DOMAIN/            -> website"
  echo "  https://DOMAIN/api/forex/  -> API"
  read -p "Enter domain for the site + API (press Enter to skip, e.g. heysolo.online): " DOMAIN_NAME
  DOMAIN_NAME=${DOMAIN_NAME:-}
  DOMAIN_NAME=${DOMAIN_NAME#https://}; DOMAIN_NAME=${DOMAIN_NAME#http://}; DOMAIN_NAME=${DOMAIN_NAME%%/*}

  if [ -n "$DOMAIN_NAME" ]; then
    read -p "Enter email for SSL certificate [default: admin@$DOMAIN_NAME]: " CERTBOT_EMAIL
    CERTBOT_EMAIL=${CERTBOT_EMAIL:-admin@$DOMAIN_NAME}
  fi
fi

# ---------------- Bot questions (modes 2 and 3) ----------------
if [ "$INSTALL_MODE" = "2" ] || [ "$INSTALL_MODE" = "3" ]; then
  RUN_BOT=true
  read -s -p "Enter Telegram bot token: " TELEGRAM_BOT_TOKEN
  echo
  read -p "Enter Admin chat ID [default: 0]: " ADMIN_CHAT_ID
  ADMIN_CHAT_ID=${ADMIN_CHAT_ID:-0}

  if [ "$INSTALL_MODE" = "3" ]; then
    # Bot-only mode: ask for the remote Flask API base URL
    echo
    echo -e "${CYAN}This bot will NOT run its own Flask API — it needs the full API base URL of a Flask server running elsewhere.${NC}"
    echo "Example: https://iran.heysolo.ir/api/forex  (or http://SERVER_IP:45869/api/forex)"
    read -p "Enter the remote Flask API base URL: " REMOTE_API_BASE_URL
    while [ -z "$REMOTE_API_BASE_URL" ]; do
      read -p "This is required. Enter the remote Flask API base URL: " REMOTE_API_BASE_URL
    done
    # strip trailing slash, if any
    REMOTE_API_BASE_URL=${REMOTE_API_BASE_URL%/}
  fi

  # ============================================
  # سوال جدید: آیا پیام آنلاین شدن مجدد ارسال شود؟
  # ============================================
  echo
  echo -e "${CYAN}Do you want to send restart notification to all users when bot restarts?${NC}"
  echo "This will send a message to all users every time the bot service restarts."
  read -p "Enable restart notification? [y/N]: " SEND_RESTART_MSG_INPUT
  SEND_RESTART_MSG_INPUT=${SEND_RESTART_MSG_INPUT:-n}

  if [[ $SEND_RESTART_MSG_INPUT =~ ^[Yy]$ ]]; then
    SEND_RESTART_MSG="true"
    echo -e "${GREEN}✓ Restart notification will be sent to users.${NC}"
  else
    SEND_RESTART_MSG="false"
    echo -e "${YELLOW}✗ Restart notification disabled.${NC}"
  fi
fi


export DEBIAN_FRONTEND=noninteractive
unset GIT_ASKPASS
unset SSH_ASKPASS
export GIT_TERMINAL_PROMPT=0

apt update
# Fonts for the pictures (Persian needs a real Persian font + libraqm for shaping); optional ones never abort the install
apt install -y fonts-liberation fonts-dejavu-core fonts-noto-core libraqm0 libfribidi0 libharfbuzz0b || true
apt install -y fonts-vazirmatn || true
apt install -y fonts-crosextra-carlito || true
if [ "$RUN_FLASK" = "true" ]; then
  apt install -y git python3 python3-venv python3-pip curl nginx
else
  # No nginx needed on a bot-only server (bot uses polling, not a webhook)
  apt install -y git python3 python3-venv python3-pip curl
fi


if [ ! -d "$APP_DIR/.git" ]; then
  if [ -e "$APP_DIR" ]; then
    echo -e "${RED}$APP_DIR already exists but is not a Git checkout. Refusing to remove it.${NC}"
    echo "Back up the existing installation and follow the manual migration guide."
    exit 1
  fi
  git clone --depth 1 "$REPO_URL" "$APP_DIR" || {
    echo -e "${RED}Git clone failed. Check repository URL.${NC}"
    exit 1
  }
else
  cd "$APP_DIR"
  git pull || {
    echo -e "${RED}Git pull failed.${NC}"
    exit 1
  }
fi

cd "$APP_DIR"

# Old layout leftovers (files used to live in the project root)
rm -f "$APP_DIR/Dockerfile" "$APP_DIR/envshowrun"

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
# Persian font for the pictures (Vazirmatn into bot/fonts/); a failure here never aborts the install
(cd "$APP_DIR" && "$APP_DIR/.venv/bin/python" -m bot.font_setup) || true

PUBLIC_BASE_URL=""
if [ -n "$DOMAIN_NAME" ]; then
  PUBLIC_BASE_URL="https://$DOMAIN_NAME"
elif [ "$RUN_FLASK" = "true" ]; then
  PUBLIC_BASE_URL="http://$(curl -4 -s --max-time 10 ifconfig.me/ip || echo YOUR_SERVER_IP)"
fi

# Local API_BASE_URL if Flask runs here, otherwise the remote URL the admin entered
if [ "$RUN_FLASK" = "true" ]; then
  API_BASE_URL="http://127.0.0.1:$FLASK_PORT/api/forex"
else
  API_BASE_URL="$REMOTE_API_BASE_URL"
fi

# .env is built by bot_settings.py
"$APP_DIR/.venv/bin/python" "$APP_DIR/bot_settings.py" set \
  "RUN_FLASK=$RUN_FLASK" \
  "FLASK_HOST=0.0.0.0" \
  "FLASK_PORT=$FLASK_PORT" \
  "FLASK_DEBUG=false" \
  "PUBLIC_BASE_URL=$PUBLIC_BASE_URL" \
  "RUN_BOT=$RUN_BOT" \
  "TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN" \
  "ADMIN_CHAT_ID=$ADMIN_CHAT_ID" \
  "API_BASE_URL=$API_BASE_URL" \
  "SEND_RESTART_MSG=$SEND_RESTART_MSG"

if [ "$RUN_BOT" = "true" ]; then
  echo
  echo -e "${CYAN}Database (PostgreSQL)${NC}"
  echo "1) Install PostgreSQL on this server and set it up automatically (recommended)"
  echo "2) Use an existing PostgreSQL server (you enter its details)"
  read -r -p "Choose [1/2, default 1]: " DB_MODE
  DB_MODE=${DB_MODE:-1}

  if [ "$DB_MODE" = "2" ]; then
    echo "Configure an existing, dedicated PostgreSQL database and user for this bot."
    for key in DB_HOST DB_PORT DB_NAME DB_USER DB_SSLMODE; do
      current=$("$APP_DIR/.venv/bin/python" "$APP_DIR/bot_settings.py" get "$key")
      read -r -p "$key [$current]: " value
      "$APP_DIR/.venv/bin/python" "$APP_DIR/bot_settings.py" set "$key=${value:-$current}"
    done
    read -rs -p "DB_PASSWORD (Enter keeps existing): " FFX_INSTALL_DB_PASSWORD
    echo
    if [ -n "$FFX_INSTALL_DB_PASSWORD" ]; then
      export FFX_INSTALL_DB_PASSWORD
      "$APP_DIR/.venv/bin/python" -c 'import os, bot_settings as c; v=c.read_env(); v["DB_PASSWORD"]=c.validate("DB_PASSWORD", os.environ["FFX_INSTALL_DB_PASSWORD"]); c.write_env(v)'
      unset FFX_INSTALL_DB_PASSWORD
    fi
  else
    if ! bash "$APP_DIR/db/provision_postgres.sh" "$APP_DIR"; then
      echo -e "${RED}Automatic PostgreSQL setup failed. Nothing else was changed.${NC}"
      echo "Check: systemctl status postgresql   and   pg_lsclusters"
      exit 1
    fi
  fi

  systemctl stop forexfactory-bot.service 2>/dev/null || true

  if ! (cd "$APP_DIR" && .venv/bin/python -m db.setup_db); then
    echo -e "${RED}Database setup failed. The bot was not restarted.${NC}"
    echo "Fix the problem above, then run: cd $APP_DIR && .venv/bin/python -m db.setup_db"
    exit 1
  fi
fi

# Older installs used the generic names flask.service / telegram-bot.service: rename them (only this project's own units)
if [ -f "$APP_DIR/scripts/rename-services.sh" ]; then
  bash "$APP_DIR/scripts/rename-services.sh" "$APP_DIR" || true
fi

if [ "$RUN_FLASK" = "true" ]; then
cat > /etc/systemd/system/forexfactory-api.service <<EOF
[Unit]
Description=Forex Factory API (website + Flask)
After=network.target

[Service]
User=$SERVICE_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/api/flask_server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable forexfactory-api.service
systemctl restart forexfactory-api.service
fi

if [ "$RUN_BOT" = "true" ]; then
AFTER_UNITS="network.target postgresql.service"
if [ "$RUN_FLASK" = "true" ]; then
  AFTER_UNITS="network.target postgresql.service forexfactory-api.service"
fi

cat > /etc/systemd/system/forexfactory-bot.service <<EOF
[Unit]
Description=Forex Factory Telegram Bot
After=$AFTER_UNITS

[Service]
User=$SERVICE_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/bot/telegram_bot.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable forexfactory-bot.service
systemctl restart forexfactory-bot.service
fi

if [ "$RUN_FLASK" = "true" ]; then
# ---------------------------------------------------------------
# Website + API on one nginx site (template: website/nginx/site.conf.template)
#
# This block LAYERS on top of whatever nginx already serves (Jade, Terminal
# Bot web terminal, other sites). It never edits nginx.conf, never deletes a
# customised default site, never restarts a running nginx (reload only), and
# if the new config does not pass `nginx -t` it rolls back to exactly what
# was there before, so the running nginx is never left broken.
# ---------------------------------------------------------------
SITE_DIR="$APP_DIR/website/content"
SITE_AVAIL="/etc/nginx/sites-available/forexfactory-api"
SITE_ENABLED="/etc/nginx/sites-enabled/forexfactory-api"
RL_CONF="/etc/nginx/conf.d/forexfactory-ratelimit.conf"
NGX_BACKUP="$(mktemp -d /tmp/ffx-nginx-backup.XXXXXX)"

mkdir -p "$SITE_DIR/assets/downloads" /etc/nginx/sites-available /etc/nginx/sites-enabled /etc/nginx/conf.d
chmod 755 "$APP_DIR" "$APP_DIR/website" "$SITE_DIR"   # nginx (www-data) must reach the site folder

# nginx must be healthy BEFORE we touch it; otherwise we could not tell who broke it.
if ! nginx -t >/dev/null 2>&1; then
  echo -e "${RED}The existing nginx configuration is already broken (nginx -t fails):${NC}"
  nginx -t 2>&1 | sed 's/^/   /' || true
  echo -e "${YELLOW}Forex website NOT installed (nginx left untouched). Fix nginx, then re-run this installer.${NC}"
  NGINX_SKIPPED=true
else
  NGINX_SKIPPED=false
fi

# A different site already using this domain would make nginx ignore/shadow one of them.
if [ "$NGINX_SKIPPED" = "false" ] && [ -n "$DOMAIN_NAME" ]; then
  DUP="$(grep -rlE "server_name[^;]*[[:space:]]${DOMAIN_NAME}[[:space:];]" /etc/nginx/sites-enabled /etc/nginx/conf.d 2>/dev/null | grep -v 'forexfactory-' | head -3 || true)"
  if [ -n "$DUP" ]; then
    echo -e "${RED}Domain $DOMAIN_NAME is already served by another nginx site:${NC}"
    echo "$DUP" | sed 's/^/   /'
    echo -e "${YELLOW}Forex website NOT installed. Use a different domain/subdomain.${NC}"
    NGINX_SKIPPED=true
  fi
fi

if [ "$NGINX_SKIPPED" = "false" ]; then
  # Remember the previous state so we can roll back.
  [ -f "$SITE_AVAIL" ] && cp -a "$SITE_AVAIL" "$NGX_BACKUP/site-available" || true
  [ -f "$RL_CONF" ]    && cp -a "$RL_CONF"    "$NGX_BACKUP/ratelimit.conf" || true
  [ -L "$SITE_ENABLED" ] && touch "$NGX_BACKUP/was-enabled" || true

  cp "$APP_DIR/website/nginx/ratelimit.conf" "$RL_CONF"
  sed -e "s|__DOMAIN__|${DOMAIN_NAME:-_}|g" \
      -e "s|__ROOT__|$SITE_DIR|g" \
      -e "s|__FLASK_PORT__|$FLASK_PORT|g" \
      "$APP_DIR/website/nginx/site.conf.template" > "$SITE_AVAIL"
  ln -sf "$SITE_AVAIL" "$SITE_ENABLED"

  # Only the untouched stock "Welcome to nginx" page is removed, and only when the
  # site runs without a domain (otherwise it would catch IP requests meant for us).
  # A customised default site is never deleted.
  if [ -z "$DOMAIN_NAME" ] && [ -L /etc/nginx/sites-enabled/default ] \
     && grep -q "Welcome to nginx" /etc/nginx/sites-available/default 2>/dev/null; then
    rm -f /etc/nginx/sites-enabled/default
    touch "$NGX_BACKUP/default-removed"
    echo -e "${YELLOW}Removed the stock nginx 'default' site (IP access now shows the Forex site).${NC}"
  fi

  if nginx -t; then
    systemctl enable nginx >/dev/null 2>&1 || true
    if systemctl is-active --quiet nginx; then
      systemctl reload nginx          # reload keeps the other sites' connections alive
    else
      systemctl start nginx
    fi
    echo -e "${GREEN}✓ nginx: Forex site added (existing sites untouched).${NC}"
  else
    echo -e "${RED}nginx -t failed with the Forex config - rolling back to the previous nginx state.${NC}"
    rm -f "$SITE_ENABLED"
    if [ -f "$NGX_BACKUP/site-available" ]; then cp -a "$NGX_BACKUP/site-available" "$SITE_AVAIL"; else rm -f "$SITE_AVAIL"; fi
    if [ -f "$NGX_BACKUP/ratelimit.conf" ]; then cp -a "$NGX_BACKUP/ratelimit.conf" "$RL_CONF"; else rm -f "$RL_CONF"; fi
    [ -f "$NGX_BACKUP/was-enabled" ] && ln -sf "$SITE_AVAIL" "$SITE_ENABLED" || true
    [ -f "$NGX_BACKUP/default-removed" ] && ln -sf /etc/nginx/sites-available/default /etc/nginx/sites-enabled/default || true
    nginx -t >/dev/null 2>&1 && systemctl reload nginx 2>/dev/null || true
    echo -e "${YELLOW}Flask API and bot are running; only the website part was skipped.${NC}"
    NGINX_SKIPPED=true
  fi

  # HTTPS: certbot --nginx edits only OUR server block. Skipped if nginx part failed.
  if [ "$NGINX_SKIPPED" = "false" ] && [ -n "$DOMAIN_NAME" ]; then
    apt install -y certbot python3-certbot-nginx
    certbot --nginx --non-interactive --agree-tos -m "$CERTBOT_EMAIL" -d "$DOMAIN_NAME" --redirect \
      || echo -e "${YELLOW}certbot failed (DNS not pointing here yet?). Re-run later: certbot --nginx -d $DOMAIN_NAME --redirect${NC}"
    nginx -t >/dev/null 2>&1 && systemctl reload nginx 2>/dev/null || true
  fi
fi
rm -rf "$NGX_BACKUP"
fi

# ============================================
# بخش جدید: نمایش وضعیت و آموزش مدیریت سرویس
# ============================================

FLASK_STATUS="disabled"
if [ "$RUN_FLASK" = "true" ]; then
  FLASK_STATUS=$(systemctl is-active forexfactory-api.service || true)
fi

BOT_STATUS="disabled"
if [ "$RUN_BOT" = "true" ]; then
  BOT_STATUS=$(systemctl is-active forexfactory-bot.service || true)
fi

SERVER_IP=$(curl -4 -s --max-time 10 ifconfig.me/ip || true)
if [ -z "$SERVER_IP" ]; then
  SERVER_IP=$(curl -4 -s --max-time 10 api.ipify.org || true)
fi
if [ -z "$SERVER_IP" ]; then
  SERVER_IP="YOUR_SERVER_IP"
fi

echo
echo -e "${CYAN}_________________________________${NC}"
echo -e "${CYAN}_________________________________${NC}"

if [ "$RUN_FLASK" = "true" ]; then
  if [ "$FLASK_STATUS" = "active" ]; then
    echo -e "${GREEN}Flask API: ACTIVE${NC}"
  else
    echo -e "${RED}Flask API: FAILED${NC}"
  fi
else
  echo -e "${YELLOW}Flask API: NOT INSTALLED (using remote: $REMOTE_API_BASE_URL)${NC}"
fi

if [ "$RUN_BOT" = "true" ]; then
  if [ "$BOT_STATUS" = "active" ]; then
    echo -e "${GREEN}Telegram Bot: ACTIVE${NC}"
  else
    echo -e "${RED}Telegram Bot: FAILED${NC}"
  fi
else
  echo -e "${YELLOW}Telegram Bot: DISABLED${NC}"
fi

# نمایش وضعیت ارسال پیام آنلاین شدن
if [ "$RUN_BOT" = "true" ]; then
  if [ "$SEND_RESTART_MSG" = "true" ]; then
    echo -e "${GREEN}Restart Notifications: ENABLED${NC}"
  else
    echo -e "${YELLOW}Restart Notifications: DISABLED${NC}"
  fi
fi


if [ "$RUN_FLASK" = "true" ]; then
  echo -e "${GREEN}Local API:${NC} http://127.0.0.1:$FLASK_PORT/api/forex/today"
  echo -e "${GREEN}Public IP API:${NC} http://$SERVER_IP/api/forex/today"

  if [ -n "$DOMAIN_NAME" ]; then
    echo -e "${GREEN}Domain API:${NC} https://$DOMAIN_NAME/api/forex/today"
  else
    echo -e "${YELLOW}Domain API:${NC} Not configured"
  fi
fi

echo -e "${CYAN}_________________________________${NC}"
echo -e "${CYAN}_________________________________${NC}"

# ============================================
# راهنمای مدیریت سرویس‌ها
# ============================================
echo -e "\n${CYAN}================== MANAGEMENT GUIDE ==================${NC}"
echo -e "${YELLOW}To check service status:${NC}"
if [ "$RUN_FLASK" = "true" ]; then
  echo "  systemctl status forexfactory-api.service"
fi
if [ "$RUN_BOT" = "true" ]; then
  echo "  systemctl status forexfactory-bot.service"
fi

echo -e "\n${YELLOW}To stop a service:${NC}"
if [ "$RUN_FLASK" = "true" ]; then
  echo "  systemctl stop forexfactory-api.service"
fi
if [ "$RUN_BOT" = "true" ]; then
  echo "  systemctl stop forexfactory-bot.service"
fi

echo -e "\n${YELLOW}To restart a service:${NC}"
if [ "$RUN_FLASK" = "true" ]; then
  echo "  systemctl restart forexfactory-api.service"
fi
if [ "$RUN_BOT" = "true" ]; then
  echo "  systemctl restart forexfactory-bot.service"
fi

echo -e "\n${YELLOW}To view service logs:${NC}"
if [ "$RUN_FLASK" = "true" ]; then
  echo "  journalctl -u forexfactory-api.service -f"
fi
if [ "$RUN_BOT" = "true" ]; then
  echo "  journalctl -u forexfactory-bot.service -f"
fi

# ============================================
# نمایش تنظیمات مربوط به پیام آنلاین شدن
# ============================================
if [ "$RUN_BOT" = "true" ]; then
  echo -e "\n${CYAN}================== RESTART NOTIFICATION SETTINGS ==================${NC}"
  echo -e "${YELLOW}To enable/disable restart notifications later:${NC}"
  echo "  $APP_DIR/.venv/bin/python $APP_DIR/bot_settings.py set SEND_RESTART_MSG=true   # or false"
  echo "  systemctl restart forexfactory-bot.service"
fi

echo -e "\n${CYAN}================== SETTINGS (bot_settings.py) ==================${NC}"
echo "  cd $APP_DIR && .venv/bin/python bot_settings.py        # interactive wizard"
echo "  cd $APP_DIR && .venv/bin/python bot_settings.py show   # show current config"

# Bot commands for switching the active Flask server
if [ "$RUN_BOT" = "true" ]; then
  echo -e "\n${CYAN}================== SWITCHING FLASK SERVERS FROM THE BOT ==================${NC}"
  echo -e "${YELLOW}As the admin (ADMIN_CHAT_ID), you can change which Flask API the bot uses at any time, without reinstalling:${NC}"
  echo "  /admin   → button panel: switch/add/delete Flask servers, 💾 full backup, ♻️ restore, 📦 EA versions"
  if [ "$RUN_FLASK" = "true" ]; then
    echo "  EA builds: send a .ex5 to the bot, it goes straight to the site's download page"
  fi
  echo "  /myid    → shows your chat ID (useful for setting ADMIN_CHAT_ID)"
  echo -e "${YELLOW}Current API base URL for this bot:${NC} $API_BASE_URL"
fi

echo -e "\n${GREEN}All services are installed and configured.${NC}"
