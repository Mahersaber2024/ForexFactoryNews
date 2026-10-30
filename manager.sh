#!/usr/bin/env bash
# =============================================================
# Unified Projects Manager
#   - JadeTunnel sales   (sell-bot, sub-api)
#   - Terminal Bot       (terminal-bot)
#   - Forex Factory       (forexfactory-api, forexfactory-bot, nginx site)
#
# One identical copy of this file lives in all three repositories.
# It never replaces the projects' own installers: it checks the
# server first (ports, service names, nginx, certificates, bot
# tokens), serialises installs with a lock, and then hands over to
# the project's installer.
#
# Usage:
#   bash manager.sh                 # interactive menu
#   bash manager.sh status          # what is installed / running
#   bash manager.sh audit           # conflict check (ports, units, nginx, tokens)
#   bash manager.sh doctor          # audit + status + certificates
#   bash manager.sh install   jade|term|forex
#   bash manager.sh update    jade|term|forex
#   bash manager.sh uninstall jade|term|forex
#   bash manager.sh nginx           # nginx-only check + safe reload
#   bash manager.sh fix-renewal     # make certbot renewals nginx-safe
# =============================================================
set -uo pipefail

HSM_VERSION="1.0.0"
SYSTEMD_DIR="${HSM_SYSTEMD_DIR:-/etc/systemd/system}"
LOCK_FILE="${HSM_LOCK_FILE:-/run/hsm-install.lock}"
RAW_BASE="https://raw.githubusercontent.com/Mahersaber2024"
PROJECTS=(jade term forex)

C_G=$'\033[0;32m'; C_R=$'\033[0;31m'; C_Y=$'\033[1;33m'; C_C=$'\033[0;36m'; C_N=$'\033[0m'
N_OK=0; N_WARN=0; N_FAIL=0

info(){ echo -e "${C_C}[i]${C_N} $*"; }
ok(){   echo -e "${C_G}[OK]${C_N} $*";   N_OK=$((N_OK+1)); }
warn(){ echo -e "${C_Y}[!!]${C_N} $*";   N_WARN=$((N_WARN+1)); }
bad(){  echo -e "${C_R}[XX]${C_N} $*";   N_FAIL=$((N_FAIL+1)); }
die(){  echo -e "${C_R}$*${C_N}" >&2; exit 1; }
press_enter(){ read -rp "Press Enter to continue..." _ || true; }

need_root(){
  [[ ${EUID:-$(id -u)} -eq 0 ]] || die "Run as root (or prefix with sudo)."
}

need_os(){
  command -v apt-get >/dev/null 2>&1 || die "Only Debian/Ubuntu servers (apt) are supported."
}

# ---------------------------------------------------------------
# Project registry
# ---------------------------------------------------------------
meta(){
  P_ID="$1"
  P_LEGACY_SERVICES=""
  case "$1" in
    jade)
      P_TITLE="JadeTunnel sales"
      P_SERVICES="sell-bot sub-api"
      P_STATE_FILE="/etc/sell-bot.install_dir"
      P_DEFAULT_DIR="/opt/sell-bot"
      P_INSTALLER_URL="$RAW_BASE/JadeTunnel-sales/main/install.sh"
      P_INSTALLER_REL="install.sh"
      P_PORTS="2053 2096"
      ;;
    term)
      P_TITLE="Terminal Bot"
      P_SERVICES="terminal-bot"
      P_STATE_FILE="/etc/terminal-bot.install_dir"
      P_DEFAULT_DIR="/opt/terminal-bot"
      P_INSTALLER_URL="$RAW_BASE/Terminal_Bot/main/install.sh"
      P_INSTALLER_REL="install.sh"
      P_PORTS=""
      ;;
    forex)
      P_TITLE="Forex Factory (API + Bot)"
      P_SERVICES="forexfactory-api forexfactory-bot"
      P_LEGACY_SERVICES="flask telegram-bot"
      P_STATE_FILE=""
      P_DEFAULT_DIR="/opt/forexfactory-api"
      P_INSTALLER_URL="$RAW_BASE/ForexFactoryNews/main/scripts/install.sh"
      P_INSTALLER_REL="scripts/install.sh"
      P_PORTS="45869"
      ;;
    *) die "Unknown project '$1'. Use: jade | term | forex" ;;
  esac
  if [[ -n "$P_STATE_FILE" && -s "$P_STATE_FILE" ]]; then
    P_DIR="$(cat "$P_STATE_FILE")"
  else
    P_DIR="$P_DEFAULT_DIR"
  fi
}

unit_file(){ echo "${SYSTEMD_DIR}/$1.service"; }

# A unit only "belongs" to a project if its WorkingDirectory is the project's folder.
# (Older Forex Factory installs used the generic names flask / telegram-bot; update renames them.)
unit_belongs(){
  local f; f="$(unit_file "$1")"
  [[ -f "$f" ]] && grep -qE "^WorkingDirectory=${2}/?$" "$f"
}

is_installed(){
  meta "$1"
  local s
  for s in $P_SERVICES ${P_LEGACY_SERVICES:-}; do
    unit_belongs "$s" "$P_DIR" && return 0
  done
  return 1
}

svc_state(){ systemctl is-active "$1" 2>/dev/null || true; }

# ---------------------------------------------------------------
# Ports
# ---------------------------------------------------------------
port_proc(){
  ss -H -ltnp "sport = :$1" 2>/dev/null | head -1 | sed -n 's/.*users:(("\([^"]*\)".*/\1/p'
}
port_busy(){ [[ -n "$(ss -H -ltn "sport = :$1" 2>/dev/null | head -1)" ]]; }

env_value(){ # env_value FILE KEY
  [[ -f "$1" ]] || return 0
  grep -E "^${2}=" "$1" | tail -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'
}

forex_port(){
  local p; p="$(env_value "$P_DIR/.env" FLASK_PORT)"
  echo "${p:-45869}"
}

# ---------------------------------------------------------------
# Locking: only one installer at a time (apt, nginx, certbot are shared)
# ---------------------------------------------------------------
take_lock(){
  [[ "${HSM_LOCK_HELD:-0}" == "1" ]] && return 0
  mkdir -p "$(dirname "$LOCK_FILE")"
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    info "Another installer is running - waiting up to 10 minutes..."
    flock -w 600 9 || die "Could not get the installer lock ($LOCK_FILE)."
  fi
  export HSM_LOCK_HELD=1
}

wait_apt(){
  local i=0
  while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || fuser /var/lib/dpkg/lock >/dev/null 2>&1; do
    [[ $i -eq 0 ]] && info "apt/dpkg is busy (unattended-upgrades?) - waiting..."
    sleep 5; i=$((i+1))
    [[ $i -gt 120 ]] && die "apt stayed locked for 10 minutes."
  done
}

# ---------------------------------------------------------------
# AUDIT
# ---------------------------------------------------------------
audit_basics(){
  echo -e "\n${C_C}== System ==${C_N}"
  local free_mb; free_mb=$(df -Pm / | awk 'NR==2{print $4}')
  if   [[ ${free_mb:-0} -lt 800  ]]; then bad  "Only ${free_mb} MB free on / (need ~1 GB for venvs + packages)"
  elif [[ ${free_mb:-0} -lt 2000 ]]; then warn "${free_mb} MB free on /"
  else ok "${free_mb} MB free on /"; fi
  local mem_mb; mem_mb=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo 2>/dev/null)
  if [[ ${mem_mb:-0} -lt 300 ]]; then warn "Only ${mem_mb:-?} MB RAM available (three Python bots + PostgreSQL are tight; add swap)"; else ok "${mem_mb} MB RAM available"; fi
  if curl -fsI --max-time 8 https://github.com >/dev/null 2>&1; then ok "github.com reachable"; else warn "github.com not reachable (installs/updates need it)"; fi
  if systemctl is-active --quiet postgresql 2>/dev/null; then ok "PostgreSQL running (shared by Jade + Terminal Bot - each uses its own database)"; else info "PostgreSQL not running (installed on demand by Jade/Terminal Bot)"; fi
}

audit_units(){
  echo -e "\n${C_C}== systemd units ==${C_N}"
  local p s f
  for p in "${PROJECTS[@]}"; do
    meta "$p"
    for s in $P_SERVICES; do
      f="$(unit_file "$s")"
      if [[ ! -f "$f" ]]; then
        continue
      elif unit_belongs "$s" "$P_DIR"; then
        ok "$s.service belongs to $P_TITLE ($(svc_state "$s"))"
      else
        bad "$s.service exists but does NOT point at $P_DIR - another application owns that unit name"
      fi
    done
  done
}

audit_ports(){
  echo -e "\n${C_C}== Listening ports ==${C_N}"
  local p proc port s
  for port in 80 443; do
    if port_busy "$port"; then
      proc="$(port_proc "$port")"
      case "$proc" in
        nginx) ok "port $port -> nginx (shared web front for the Forex site and Terminal Bot web terminal)";;
        *)     warn "port $port -> ${proc:-unknown}  (Forex's nginx site cannot use it; web terminal may bind it directly)";;
      esac
    else
      info "port $port is free"
    fi
  done
  for p in "${PROJECTS[@]}"; do
    meta "$p"
    local ports="$P_PORTS"
    [[ "$p" == forex ]] && is_installed forex && ports="$(forex_port)"
    for port in $ports; do
      if port_busy "$port"; then
        proc="$(port_proc "$port")"
        if is_installed "$p"; then ok "port $port -> ${proc:-?} ($P_TITLE)"
        else bad "port $port needed by $P_TITLE is already used by ${proc:-another program}"; fi
      fi
    done
  done
}

audit_nginx(){
  echo -e "\n${C_C}== nginx ==${C_N}"
  if ! command -v nginx >/dev/null 2>&1; then
    info "nginx is not installed (the Forex installer adds it when needed)"
    return
  fi
  if nginx -t >/dev/null 2>&1; then ok "nginx -t passes"; else bad "nginx -t FAILS - fix before installing anything that touches nginx:"; nginx -t 2>&1 | sed 's/^/     /'; fi
  if systemctl is-active --quiet nginx; then ok "nginx is running"; else warn "nginx is installed but not running"; fi

  local dump; dump="$(nginx -T 2>/dev/null || true)"
  local defaults; defaults=$(echo "$dump" | grep -cE '^\s*listen[^;]*\b80\b[^;]*default_server' || true)
  if [[ ${defaults:-0} -gt 1 ]]; then bad "$defaults default_server blocks on port 80 (nginx will refuse to start)"; fi

  if [[ -L /etc/nginx/sites-enabled/default ]]; then
    if grep -q "Welcome to nginx" /etc/nginx/sites-available/default 2>/dev/null; then
      info "stock 'default' site is enabled (left untouched unless the Forex site is installed without a domain)"
    else
      info "'default' site is enabled and customised - the installers will not remove it"
    fi
  fi

  if [[ -f /etc/nginx/sites-enabled/forexfactory-api ]]; then
    local d dup
    for d in $(grep -E '^\s*server_name' /etc/nginx/sites-enabled/forexfactory-api | sed -E 's/server_name|;//g' | tr -s ' ' '\n' | grep -vE '^(_|)$' | sort -u); do
      dup=$(grep -rlE "server_name[^;]*[[:space:]]${d}[[:space:];]" /etc/nginx/sites-enabled /etc/nginx/conf.d 2>/dev/null | grep -v forexfactory-api | head -3 || true)
      if [[ -n "$dup" ]]; then bad "server_name $d is also declared in: $dup"; else ok "server_name $d is unique"; fi
    done
    [[ -f /etc/nginx/conf.d/forexfactory-ratelimit.conf ]] && ok "Forex rate-limit/map file present" || warn "forexfactory-ratelimit.conf missing - the site config will not load"
  fi
  [[ -f /etc/nginx/conf.d/terminal-bot-webterminal.conf || -n "$(ls /etc/nginx/conf.d 2>/dev/null | grep -i webterm || true)" ]] && ok "Terminal Bot web-terminal vhost present (separate file, no overlap)"
}

audit_certs(){
  echo -e "\n${C_C}== Certificates ==${C_N}"
  local live=/etc/letsencrypt/live d end days
  if [[ ! -d $live ]]; then info "no Let's Encrypt certificates on this server"; return; fi
  for d in "$live"/*/; do
    d="${d%/}"; [[ -f "$d/cert.pem" ]] || continue
    end=$(openssl x509 -enddate -noout -in "$d/cert.pem" 2>/dev/null | cut -d= -f2)
    days=$(( ( $(date -d "$end" +%s 2>/dev/null || echo 0) - $(date +%s) ) / 86400 ))
    if   [[ $days -lt 7  ]]; then bad  "$(basename "$d"): expires in $days days"
    elif [[ $days -lt 20 ]]; then warn "$(basename "$d"): expires in $days days"
    else ok "$(basename "$d"): $days days left"; fi
  done
  local conf
  for conf in /etc/letsencrypt/renewal/*.conf; do
    [[ -f "$conf" ]] || continue
    if grep -qE '^\s*authenticator\s*=\s*standalone' "$conf" && command -v nginx >/dev/null 2>&1 && ! grep -qE '^\s*pre_hook' "$conf"; then
      bad "$(basename "$conf" .conf): renews with 'standalone' but nginx holds port 80 and no pre_hook is set -> renewal will FAIL. Run: bash manager.sh fix-renewal"
    fi
  done
}

collect_token(){ # prints the bot token of a project (nothing if unknown)
  meta "$1"
  case "$1" in
    jade)  [[ -f "$P_DIR/config.json" ]] && sed -n 's/.*"bot_token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$P_DIR/config.json" | head -1 ;;
    term)  env_value "$P_DIR/.env" BOT_TOKEN ;;
    forex) env_value "$P_DIR/.env" TELEGRAM_BOT_TOKEN ;;
  esac
}

audit_tokens(){
  echo -e "\n${C_C}== Telegram bot tokens ==${C_N}"
  local p t h seen=""; declare -A owner=()
  local dup=0
  for p in "${PROJECTS[@]}"; do
    t="$(collect_token "$p")"; [[ -n "$t" ]] || continue
    h=$(printf '%s' "$t" | sha256sum | cut -c1-16)
    if [[ -n "${owner[$h]:-}" ]]; then
      bad "$p and ${owner[$h]} use the SAME bot token - Telegram allows only one polling process per token (409 Conflict)"
      dup=1
    else owner[$h]="$p"; fi
  done
  [[ $dup -eq 0 ]] && ok "no token is shared between projects"
}

audit_all(){
  N_OK=0; N_WARN=0; N_FAIL=0
  audit_basics; audit_units; audit_ports; audit_nginx; audit_certs; audit_tokens
  echo
  echo -e "${C_C}Summary:${C_N} ${C_G}${N_OK} ok${C_N}, ${C_Y}${N_WARN} warnings${C_N}, ${C_R}${N_FAIL} problems${C_N}"
  [[ $N_FAIL -eq 0 ]]
}

# ---------------------------------------------------------------
# STATUS
# ---------------------------------------------------------------
cmd_status(){
  echo -e "${C_C}Project               Installed   Services${C_N}"
  local p s line
  for p in "${PROJECTS[@]}"; do
    meta "$p"
    if is_installed "$p"; then
      line=""
      for s in $P_SERVICES; do
        unit_belongs "$s" "$P_DIR" && line+="$s=$(svc_state "$s") "
      done
      printf "%-21s %-11s %s\n   dir: %s\n" "$P_TITLE" "yes" "$line" "$P_DIR"
    else
      printf "%-21s %-11s\n" "$P_TITLE" "no"
    fi
  done
  if is_installed forex && [[ -f /etc/nginx/sites-enabled/forexfactory-api ]]; then
    echo
    echo "Forex nginx site: enabled, domain(s): $(grep -E '^\s*server_name' /etc/nginx/sites-enabled/forexfactory-api | head -1 | sed -E 's/\s*server_name\s*//; s/;//')"
  fi
}

# ---------------------------------------------------------------
# Pre-flight for one project (only what could go wrong for THAT project)
# ---------------------------------------------------------------
preflight(){
  local p="$1"; meta "$p"
  N_OK=0; N_WARN=0; N_FAIL=0
  info "Pre-flight for $P_TITLE"
  local free_mb; free_mb=$(df -Pm / | awk 'NR==2{print $4}')
  [[ ${free_mb:-0} -ge 800 ]] || bad "less than 800 MB free on /"
  nginx_ok_or_absent || true

  if ! is_installed "$p"; then
    local s port proc
    for s in $P_SERVICES; do
      if [[ -f "$(unit_file "$s")" ]] && ! unit_belongs "$s" "$P_DIR"; then
        bad "$s.service already exists and belongs to something else"
      fi
    done
    for port in $P_PORTS; do
      if port_busy "$port"; then bad "port $port is already used by $(port_proc "$port" || echo another program)"; fi
    done
    if [[ "$p" == forex ]]; then
      if port_busy 80; then
        proc="$(port_proc 80)"
        [[ "$proc" == nginx ]] || warn "port 80 is held by '${proc:-unknown}' - the Forex nginx site cannot start there; choose 'Telegram Bot only' or free port 80"
      fi
    fi
  fi
  [[ $N_FAIL -eq 0 ]] || { echo -e "${C_R}Pre-flight failed - nothing was changed.${C_N}"; return 1; }
  ok "pre-flight passed"
}

nginx_ok_or_absent(){
  command -v nginx >/dev/null 2>&1 || return 0
  if ! nginx -t >/dev/null 2>&1; then bad "current nginx config is already broken (nginx -t fails) - fix it first"; return 1; fi
}

# ---------------------------------------------------------------
# Run a project's own installer
# ---------------------------------------------------------------
fetch_installer(){ # fetch_installer REL URL -> prints path
  local rel="$1" url="$2" local_copy
  local_copy="$P_DIR/$rel"
  if [[ -f "$local_copy" ]] && bash -n "$local_copy" 2>/dev/null; then
    echo "$local_copy"; return 0
  fi
  local tmp; tmp="$(mktemp /tmp/hsm-installer.XXXXXX.sh)"
  curl -fsSL --max-time 60 "$url" -o "$tmp" || { rm -f "$tmp"; return 1; }
  head -1 "$tmp" | grep -q '^#!' || { rm -f "$tmp"; return 1; }
  bash -n "$tmp" 2>/dev/null || { rm -f "$tmp"; return 1; }
  echo "$tmp"
}

run_installer(){
  local p="$1"; meta "$p"
  local f
  f="$(fetch_installer "$P_INSTALLER_REL" "$P_INSTALLER_URL")" || die "Could not obtain the installer for $P_TITLE."
  take_lock
  export HSM_MANAGED=1
  wait_apt
  bash "$f"
  local rc=$?
  [[ "$f" == /tmp/hsm-installer.* ]] && rm -f "$f"
  return $rc
}

cmd_install(){
  local p="${1:-}"; [[ -n "$p" ]] || die "Usage: manager.sh install jade|term|forex"
  meta "$p"
  if is_installed "$p"; then
    warn "$P_TITLE is already installed in $P_DIR - use 'update' instead (or uninstall first)."
    return 0
  fi
  preflight "$p" || return 1
  case "$p" in
    jade|term) info "In the installer menu choose 1) Full installation." ;;
  esac
  run_installer "$p"
  echo
  audit_nginx; audit_ports
}

cmd_update(){
  local p="${1:-}"; [[ -n "$p" ]] || die "Usage: manager.sh update jade|term|forex"
  meta "$p"
  is_installed "$p" || die "$P_TITLE is not installed."
  take_lock
  case "$p" in
    forex)
      info "Updating Forex Factory (git pull, pip, restart)..."
      git -C "$P_DIR" pull --ff-only || die "git pull failed (local changes?)."
      "$P_DIR/.venv/bin/pip" install -q -r "$P_DIR/requirements.txt"
      if unit_belongs "forexfactory-bot.service" "$P_DIR"; then
        if [[ -z "$("$P_DIR/.venv/bin/python" "$P_DIR/bot_settings.py" get DB_PASSWORD 2>/dev/null)" ]]; then
          info "No PostgreSQL configured yet - installing and setting it up automatically..."
          bash "$P_DIR/db/provision_postgres.sh" "$P_DIR" || die "Automatic PostgreSQL setup failed. Existing bot was not restarted."
        fi
        systemctl stop forexfactory-bot.service 2>/dev/null || true
        if ! (cd "$P_DIR" && .venv/bin/python -m db.setup_db); then
          die "Database setup failed. The bot was not restarted. See README."
        fi
      fi
      [[ -f "$P_DIR/scripts/rename-services.sh" ]] && bash "$P_DIR/scripts/rename-services.sh" "$P_DIR"
      local s
      for s in $P_SERVICES; do unit_belongs "$s" "$P_DIR" && systemctl restart "$s"; done
      nginx_reload_safe
      ;;
    *)
      info "In the installer menu choose 2) Update bot."
      run_installer "$p"
      ;;
  esac
}

cmd_uninstall(){
  local p="${1:-}"; [[ -n "$p" ]] || die "Usage: manager.sh uninstall jade|term|forex"
  meta "$p"
  is_installed "$p" || die "$P_TITLE is not installed."
  echo -e "${C_Y}This removes $P_TITLE only. PostgreSQL, nginx, certbot, git and Python are shared and stay installed.${C_N}"
  read -rp "Type YES to continue: " a; [[ "$a" == "YES" ]] || { info "Cancelled."; return 0; }
  take_lock
  export HSM_MANAGED=1
  if [[ "$p" == forex ]]; then
    local u="$P_DIR/scripts/uninstall.sh"
    [[ -f "$u" ]] || die "Missing $u"
    bash "$u"
  else
    [[ "$p" == jade ]] && info "In the installer menu choose 7) Complete uninstall." || info "In the installer menu choose 6) Complete uninstall."
    run_installer "$p"
  fi
}

# ---------------------------------------------------------------
# nginx helpers
# ---------------------------------------------------------------
nginx_reload_safe(){
  command -v nginx >/dev/null 2>&1 || return 0
  if nginx -t >/dev/null 2>&1; then
    if systemctl is-active --quiet nginx; then systemctl reload nginx && ok "nginx reloaded (connections kept)"; else systemctl start nginx; fi
  else
    bad "nginx -t failed - NOT reloading; the running config stays active:"
    nginx -t 2>&1 | sed 's/^/     /'
    return 1
  fi
}

cmd_nginx(){
  N_OK=0; N_WARN=0; N_FAIL=0
  audit_nginx
  nginx_reload_safe
}

# ---------------------------------------------------------------
# Make existing 'standalone' renewals compatible with a running nginx
# and make sub-api pick up renewed certificates.
# ---------------------------------------------------------------
cmd_fix_renewal(){
  command -v certbot >/dev/null 2>&1 || { info "certbot is not installed - nothing to do."; return 0; }
  local conf name changed=0 cert_dom
  cert_dom=""
  if [[ -f "$(unit_file sub-api)" ]]; then
    cert_dom=$(sed -n 's#.*SSL_CERT_FILE=/etc/letsencrypt/live/\([^/]*\)/.*#\1#p' "$(unit_file sub-api)" | head -1)
  fi
  for conf in /etc/letsencrypt/renewal/*.conf; do
    [[ -f "$conf" ]] || continue
    name="$(basename "$conf" .conf)"
    cp -a "$conf" "${conf}.hsm-bak"
    if grep -qE '^\s*authenticator\s*=\s*standalone' "$conf" && command -v nginx >/dev/null 2>&1; then
      if ! grep -qE '^\s*pre_hook' "$conf"; then
        sed -i '/^\[renewalparams\]/a pre_hook = systemctl stop nginx\npost_hook = systemctl start nginx' "$conf"
        ok "$name: nginx is now stopped/started around standalone renewals"; changed=1
      fi
    fi
    if [[ -n "$cert_dom" && "$name" == "$cert_dom" ]] && ! grep -qE '^\s*renew_hook' "$conf"; then
      sed -i '/^\[renewalparams\]/a renew_hook = systemctl restart sub-api' "$conf"
      ok "$name: sub-api restarts after renewal (it loads the certificate only at start)"; changed=1
    fi
  done
  [[ $changed -eq 1 ]] || info "Nothing needed fixing."
  info "Test with: certbot renew --dry-run"
}

# ---------------------------------------------------------------
# Menu
# ---------------------------------------------------------------
pick_project(){
  # menu text goes to stderr: callers capture stdout with $(pick_project)
  {
    echo "  1) JadeTunnel sales"
    echo "  2) Terminal Bot"
    echo "  3) Forex Factory (API + Bot)"
  } >&2
  read -rp "Project [1-3]: " n
  case "$n" in 1) echo jade;; 2) echo term;; 3) echo forex;; *) echo "";; esac
}

main_menu(){
  while true; do
    echo
    echo "======================================"
    echo " Unified Projects Manager v${HSM_VERSION}"
    echo "======================================"
    echo "1) Status"
    echo "2) Conflict audit (ports / units / nginx / tokens)"
    echo "3) Install a project"
    echo "4) Update a project"
    echo "5) Uninstall a project"
    echo "6) nginx check + safe reload"
    echo "7) Fix certificate renewals"
    echo "8) Doctor (everything above)"
    echo "0) Exit"
    echo "======================================"
    read -rp "Enter option number: " c
    case "$c" in
      1) cmd_status; press_enter ;;
      2) audit_all; press_enter ;;
      3) p="$(pick_project)"; [[ -n "$p" ]] && cmd_install "$p"; press_enter ;;
      4) p="$(pick_project)"; [[ -n "$p" ]] && cmd_update "$p"; press_enter ;;
      5) p="$(pick_project)"; [[ -n "$p" ]] && cmd_uninstall "$p"; press_enter ;;
      6) cmd_nginx; press_enter ;;
      7) cmd_fix_renewal; press_enter ;;
      8) cmd_status; audit_all; press_enter ;;
      0) exit 0 ;;
      *) warn "Invalid option." ;;
    esac
  done
}

main(){
  local cmd="${1:-menu}"
  case "$cmd" in
    help|-h|--help) sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'; return 0 ;;
  esac
  need_root; need_os
  case "$cmd" in
    menu)        main_menu ;;
    status)      cmd_status ;;
    audit)       audit_all ;;
    doctor)      cmd_status; audit_all ;;
    install)     cmd_install "${2:-}" ;;
    update)      cmd_update "${2:-}" ;;
    uninstall)   cmd_uninstall "${2:-}" ;;
    nginx)       cmd_nginx ;;
    fix-renewal) cmd_fix_renewal ;;
    *) die "Unknown command '$cmd'. Try: bash manager.sh help" ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
