#!/usr/bin/env bash
# Wayflix Trucking Email Sender — one-command install / update for Ubuntu 22.04 / 24.04 (Oracle Cloud Always Free VM).
#
#   sudo ./install.sh
#
# Safe to run again: it updates the code and keeps your settings, passwords and data.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then echo "Please run with sudo:  sudo ./install.sh"; exit 1; fi

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR=/opt/driver-outreach
DATA_DIR=/var/lib/driver-outreach
ETC_DIR=/etc/driver-outreach
ENV_FILE=$ETC_DIR/outreach.env
export DEBIAN_FRONTEND=noninteractive

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

# ---------------------------------------------------------------- 1. swap (1 GB VMs need it)
say "Checking swap"
if ! swapon --show | grep -q '/swapfile'; then
  fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "2 GB swap file created."
else
  echo "Swap already present."
fi

# ---------------------------------------------------------------- 2. packages
say "Installing system packages (this can take a few minutes)"
apt-get update -y
apt-get install -y curl gnupg debian-keyring debian-archive-keyring apt-transport-https ca-certificates \
  iptables-persistent netfilter-persistent cron

PY=""
for v in python3.12 python3.11; do
  if command -v "$v" >/dev/null 2>&1; then PY=$v; break; fi
done
if [ -z "$PY" ]; then
  apt-get install -y python3.11 python3.11-venv || { add-apt-repository -y universe && apt-get update -y && apt-get install -y python3.11 python3.11-venv; }
  PY=python3.11
fi
apt-get install -y "${PY}-venv" || true
echo "Using $($PY --version)"

if ! command -v caddy >/dev/null 2>&1; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
  apt-get install -y caddy
fi

# ---------------------------------------------------------------- 3. user + folders
say "Creating service user and folders"
id outreach >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin outreach
mkdir -p "$APP_DIR" "$DATA_DIR" "$ETC_DIR"
chown -R outreach:outreach "$DATA_DIR"
chmod 750 "$DATA_DIR"
# Database, lists, sent-mail copies and backups: readable by the app user only
find "$DATA_DIR" -mindepth 1 -type d -exec chmod 700 {} +
find "$DATA_DIR" -type f -exec chmod 600 {} +
chmod 750 "$ETC_DIR"
chown root:outreach "$ETC_DIR"

# ---------------------------------------------------------------- 4. code + Python packages
say "Installing the application"
rm -rf "$APP_DIR/app" "$APP_DIR/core" "$APP_DIR/worker"
cp -r "$SRC_DIR/app" "$SRC_DIR/core" "$SRC_DIR/worker" "$SRC_DIR/requirements.txt" "$APP_DIR/"
find "$APP_DIR" -name '__pycache__' -prune -exec rm -rf {} +
if [ ! -x "$APP_DIR/venv/bin/python" ]; then "$PY" -m venv "$APP_DIR/venv"; fi
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
chown -R root:root "$APP_DIR"

# ---------------------------------------------------------------- 5. secrets (first install only)
if [ ! -f "$ENV_FILE" ]; then
  say "First-time setup"
  read -rp "Your DuckDNS address (e.g. abclogistics.duckdns.org): " DOMAIN
  DOMAIN=${DOMAIN#https://}; DOMAIN=${DOMAIN#http://}; DOMAIN=${DOMAIN%/}
  read -rp "Choose a login username [admin]: " ADMIN_USER
  ADMIN_USER=${ADMIN_USER:-admin}
  while true; do
    read -rsp "Choose a login password (at least 10 characters): " P1; echo
    read -rsp "Type it again: " P2; echo
    if [ "$P1" != "$P2" ]; then echo "They don't match. Try again."; continue; fi
    if [ ${#P1} -lt 10 ]; then echo "Too short. Try again."; continue; fi
    break
  done
  HASH=$(cd "$APP_DIR" && OUTREACH_PW="$P1" "$APP_DIR/venv/bin/python" -c \
    "import os; from core.crypto import hash_password; print(hash_password(os.environ['OUTREACH_PW']))")
  unset P1 P2
  APP_SECRET=$("$APP_DIR/venv/bin/python" -c "import secrets; print(secrets.token_urlsafe(48))")
  FERNET_KEY=$("$APP_DIR/venv/bin/python" -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
  umask 077
  cat > "$ENV_FILE" <<EOF
# Wayflix Trucking Email Sender secrets. Keep private. Created $(date -u +%F).
PUBLIC_BASE_URL=https://$DOMAIN
ADMIN_USER=$ADMIN_USER
ADMIN_PASSWORD_HASH=$HASH
APP_SECRET=$APP_SECRET
FERNET_KEY=$FERNET_KEY
DATA_DIR=$DATA_DIR
GOOGLE_SA_FILE=$ETC_DIR/google-service-account.json
# 1 = save emails to $DATA_DIR/outbox instead of sending them
DRY_RUN=0
EOF
  umask 022
  read -rp "DuckDNS token (optional, keeps the address pointing at this VM; press Enter to skip): " DDNS_TOKEN
  if [ -n "$DDNS_TOKEN" ]; then
    SUB=${DOMAIN%.duckdns.org}
    printf 'DDNS_SUB=%s\nDDNS_TOKEN=%s\n' "$SUB" "$DDNS_TOKEN" > "$ETC_DIR/duckdns.env"
    chmod 600 "$ETC_DIR/duckdns.env"
    cat > /etc/cron.d/duckdns <<'EOF'
*/10 * * * * root . /etc/driver-outreach/duckdns.env && curl -fsS "https://www.duckdns.org/update?domains=$DDNS_SUB&token=$DDNS_TOKEN&ip=" >/dev/null 2>&1
EOF
  fi
else
  say "Keeping existing settings in $ENV_FILE"
  DOMAIN=$(grep '^PUBLIC_BASE_URL=' "$ENV_FILE" | cut -d/ -f3)
fi
chown root:outreach "$ENV_FILE"
chmod 640 "$ENV_FILE"
if [ -f "$ETC_DIR/google-service-account.json" ]; then
  chown root:outreach "$ETC_DIR/google-service-account.json"
  chmod 640 "$ETC_DIR/google-service-account.json"
fi

# ---------------------------------------------------------------- 6. firewall (Oracle images block 80/443)
say "Opening ports 80 and 443 in the VM firewall"
for port in 80 443; do
  iptables -C INPUT -p tcp -m state --state NEW --dport "$port" -j ACCEPT 2>/dev/null || \
    iptables -I INPUT 1 -p tcp -m state --state NEW --dport "$port" -j ACCEPT
done
netfilter-persistent save >/dev/null 2>&1 || true
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q 'Status: active'; then ufw allow 80/tcp; ufw allow 443/tcp; fi

# ---------------------------------------------------------------- 6b. server hardening
say "Hardening SSH and switching off unused services"
# Key-only SSH as 'ubuntu'; no root login, no X11. 01- sorts before cloud-init's files, and sshd uses the first value.
HARDEN=/etc/ssh/sshd_config.d/01-driver-outreach.conf
printf '%s\n' "# Added by Wayflix Trucking Email Sender install.sh" "PermitRootLogin no" "PasswordAuthentication no" \
  "KbdInteractiveAuthentication no" "X11Forwarding no" > "$HARDEN"
chmod 644 "$HARDEN"
if sshd -t 2>/dev/null; then
  systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
else
  echo "sshd rejected the hardening file; removing it (SSH unchanged)."; rm -f "$HARDEN"
fi
# rpcbind (NFS helper) is not needed: stop it and keep it from starting again
if systemctl list-unit-files rpcbind.service >/dev/null 2>&1; then
  systemctl disable --now rpcbind.socket rpcbind.service >/dev/null 2>&1 || true
  systemctl mask rpcbind.socket rpcbind.service >/dev/null 2>&1 || true
fi

# ---------------------------------------------------------------- 7. Caddy (HTTPS)
say "Configuring HTTPS for $DOMAIN"
cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
    encode gzip
    request_body {
        max_size 20MB
    }
    header Strict-Transport-Security "max-age=31536000"
    reverse_proxy 127.0.0.1:8000
}
EOF
systemctl enable caddy >/dev/null
systemctl reload caddy 2>/dev/null || systemctl restart caddy

# ---------------------------------------------------------------- 8. services + backups
say "Starting services"
cp "$SRC_DIR/deploy/outreach-web.service" "$SRC_DIR/deploy/outreach-worker.service" /etc/systemd/system/
install -m 755 "$SRC_DIR/deploy/backup.sh" /etc/cron.daily/driver-outreach-backup
systemctl daemon-reload
systemctl enable outreach-web outreach-worker >/dev/null
systemctl restart outreach-web outreach-worker

sleep 3
if systemctl is-active --quiet outreach-web && systemctl is-active --quiet outreach-worker; then
  say "Done!  Open  https://$DOMAIN  in your browser."
  echo "(The first visit can take a minute while the HTTPS certificate is issued.)"
else
  echo "Something did not start. Show the log with:  sudo journalctl -u outreach-web -u outreach-worker -n 50"
  exit 1
fi
