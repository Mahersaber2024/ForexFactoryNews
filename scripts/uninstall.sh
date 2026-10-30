#!/usr/bin/env bash
set -e

APP_DIR="/opt/forexfactory-api"
SERVICE_USER="root"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${RED}========================================${NC}"
echo -e "${RED}    UNINSTALL FOREXFACTORY API${NC}"
echo -e "${RED}========================================${NC}"
echo

# بررسی وجود پروژه
if [ ! -d "$APP_DIR" ]; then
    echo -e "${YELLOW}Project directory $APP_DIR does not exist.${NC}"
    echo -e "${YELLOW}Nothing to uninstall.${NC}"
    exit 0
fi

echo -e "${YELLOW}WARNING: This will permanently delete:${NC}"
echo "  - Flask API service"
if systemctl list-units --full -all | grep -q "forexfactory-bot.service"; then
    echo "  - Telegram Bot service"
fi
echo "  - Nginx configuration for forexfactory-api (website + API)"
if [ -f /etc/nginx/sites-available/forexfactory-api ]; then
    echo "  - Nginx site configuration"
fi
echo "  - All project files in $APP_DIR (website + uploaded EA builds)"
echo "  - Python virtual environment"
echo "  - Environment variables (.env file)"
echo

read -p "Are you sure you want to continue? [y/N]: " CONFIRM
CONFIRM=${CONFIRM:-N}

if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
    echo -e "${GREEN}Uninstall cancelled.${NC}"
    exit 0
fi

# Read domain from config (for SSL cleanup)
DOMAIN_NAME=""
if [ -f "$APP_DIR/.env" ]; then
    PUBLIC_BASE_URL=$(grep -E '^PUBLIC_BASE_URL=' "$APP_DIR/.env" | cut -d= -f2- || true)
    DOMAIN_NAME=${PUBLIC_BASE_URL#https://}
    DOMAIN_NAME=${DOMAIN_NAME#http://}
    DOMAIN_NAME=${DOMAIN_NAME%%/*}
fi

# Offer a final full backup (DB + .env) before everything is deleted
if [ -x "$APP_DIR/.venv/bin/python" ]; then
    read -p "Save a final backup of the bot data to /root before removing? [Y/n]: " DO_BACKUP
    DO_BACKUP=${DO_BACKUP:-Y}
    if [[ "$DO_BACKUP" =~ ^[Yy]$ ]]; then
        BK=$(cd "$APP_DIR" && .venv/bin/python -c "from bot import backup_manager as b; print(b.create_backup('uninstall'))" 2>/dev/null | tail -1 || true)
        if [ -n "$BK" ] && [ -f "$BK" ]; then
            cp "$BK" /root/
            echo -e "${GREEN}✓ Backup saved: /root/$(basename "$BK")${NC}"
            echo "  Restore it later from the bot: /admin → ♻️ Restore → send the file"
        else
            echo -e "${YELLOW}⚠ Could not create backup, continuing${NC}"
        fi
    fi
fi

echo -e "\n${CYAN}Starting uninstall process...${NC}"

# 0. Older installs: first give the old flask / telegram-bot units their new names so the steps below find them
[ -f "$APP_DIR/scripts/rename-services.sh" ] && bash "$APP_DIR/scripts/rename-services.sh" "$APP_DIR" >/dev/null 2>&1 || true

# 1. توقف و حذف سرویس Flask
echo -e "${YELLOW}Stopping and removing Flask service...${NC}"
if systemctl is-active --quiet forexfactory-api.service; then
    systemctl stop forexfactory-api.service
    echo -e "${GREEN}✓ Flask service stopped${NC}"
else
    echo -e "${YELLOW}⚠ Flask service was not running${NC}"
fi

if systemctl is-enabled --quiet forexfactory-api.service 2>/dev/null; then
    systemctl disable forexfactory-api.service
    echo -e "${GREEN}✓ Flask service disabled${NC}"
fi

if [ -f /etc/systemd/system/forexfactory-api.service ]; then
    rm -f /etc/systemd/system/forexfactory-api.service
    echo -e "${GREEN}✓ Flask service file removed${NC}"
fi

# 2. توقف و حذف سرویس Telegram Bot (در صورت وجود)
if systemctl list-units --full -all | grep -q "forexfactory-bot.service"; then
    echo -e "${YELLOW}Stopping and removing Telegram Bot service...${NC}"
    if systemctl is-active --quiet forexfactory-bot.service; then
        systemctl stop forexfactory-bot.service
        echo -e "${GREEN}✓ Telegram Bot service stopped${NC}"
    else
        echo -e "${YELLOW}⚠ Telegram Bot service was not running${NC}"
    fi
    
    if systemctl is-enabled --quiet forexfactory-bot.service 2>/dev/null; then
        systemctl disable forexfactory-bot.service
        echo -e "${GREEN}✓ Telegram Bot service disabled${NC}"
    fi
    
    if [ -f /etc/systemd/system/forexfactory-bot.service ]; then
        rm -f /etc/systemd/system/forexfactory-bot.service
        echo -e "${GREEN}✓ Telegram Bot service file removed${NC}"
    fi
fi

# 3. ری‌لود systemd
systemctl daemon-reload
echo -e "${GREEN}✓ Systemd reloaded${NC}"

# 4. حذف Nginx configuration
echo -e "${YELLOW}Removing Nginx configuration...${NC}"
if [ -f /etc/nginx/sites-available/forexfactory-api ]; then
    rm -f /etc/nginx/sites-available/forexfactory-api
    echo -e "${GREEN}✓ Nginx site config removed from sites-available${NC}"
fi

if [ -f /etc/nginx/sites-enabled/forexfactory-api ]; then
    rm -f /etc/nginx/sites-enabled/forexfactory-api
    echo -e "${GREEN}✓ Nginx site config removed from sites-enabled${NC}"
fi

# 4b. Rate-limit zone used by the website/API nginx config
rm -f /etc/nginx/conf.d/forexfactory-ratelimit.conf

# 5. ری‌استارت Nginx (در صورت نصب بودن)
if command -v nginx &> /dev/null; then
    # reload (not restart): other sites on this nginx (Jade / Terminal Bot web terminal) keep their connections
    if nginx -t 2>/dev/null; then
        systemctl is-active --quiet nginx && systemctl reload nginx || true
        echo -e "${GREEN}✓ Nginx reloaded${NC}"
    else
        echo -e "${YELLOW}⚠ Nginx configuration test failed, skipping reload${NC}"
    fi
fi

# 6. حذف SSL Certificate (در صورت وجود)
if [ -n "$DOMAIN_NAME" ] && [ -d "/etc/letsencrypt/live/$DOMAIN_NAME" ]; then
    echo -e "${YELLOW}Removing SSL certificates for $DOMAIN_NAME...${NC}"
    if command -v certbot &> /dev/null; then
        certbot delete --cert-name "$DOMAIN_NAME" --non-interactive 2>/dev/null || true
        echo -e "${GREEN}✓ SSL certificates removed${NC}"
    fi
fi

# 7. حذف دایرکتوری پروژه
echo -e "${YELLOW}Removing project directory...${NC}"
if [ -d "$APP_DIR" ]; then
    rm -rf "$APP_DIR"
    echo -e "${GREEN}✓ Project directory removed${NC}"
fi

# 8. حذف وابستگی‌های اختیاری (اختیاری)
echo -e "\n${YELLOW}Do you want to remove Python packages and system dependencies?${NC}"
echo "  This will remove: python3-venv, python3-pip, git, nginx, certbot"
echo "  (Recommended only if no other projects use them)"
read -p "Remove system packages? [y/N]: " REMOVE_PKGS
REMOVE_PKGS=${REMOVE_PKGS:-N}

if [[ "$REMOVE_PKGS" =~ ^[Yy]$ ]]; then
    echo -e "${YELLOW}Removing system packages...${NC}"
    apt remove -y python3-venv python3-pip git nginx certbot python3-certbot-nginx 2>/dev/null || true
    apt autoremove -y
    echo -e "${GREEN}✓ System packages removed${NC}"
fi

echo -e "\n${GREEN}========================================${NC}"
echo -e "${GREEN}    UNINSTALL COMPLETED SUCCESSFULLY${NC}"
echo -e "${GREEN}========================================${NC}"
echo
echo -e "${CYAN}Summary of removed items:${NC}"
echo -e "${GREEN}✓${NC} Flask API service"
if systemctl list-units --full -all | grep -q "forexfactory-bot.service"; then
    echo -e "${GREEN}✓${NC} Telegram Bot service"
fi
echo -e "${GREEN}✓${NC} Nginx configuration"
if [ -n "$DOMAIN_NAME" ]; then
    echo -e "${GREEN}✓${NC} SSL certificates (if any)"
fi
echo -e "${GREEN}✓${NC} Project files in $APP_DIR"
echo -e "${GREEN}✓${NC} Virtual environment"
echo -e "${GREEN}✓${NC} Environment variables"
if [[ "$REMOVE_PKGS" =~ ^[Yy]$ ]]; then
    echo -e "${GREEN}✓${NC} System packages (python3-venv, python3-pip, git, nginx, certbot)"
fi
echo
echo -e "${CYAN}To verify removal:${NC}"
echo "  systemctl status forexfactory-api.service  # should show 'not found'"
echo "  systemctl status forexfactory-bot.service  # should show 'not found'"
echo "  ls -la $APP_DIR  # should show 'No such file or directory'"
echo
