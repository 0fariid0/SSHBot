#!/usr/bin/env bash
# SSHBot Web v6: Ubuntu 22.04/24.04; run the installer from the extracted package.
set -Eeuo pipefail
umask 077
[[ $EUID -eq 0 ]] || { echo 'Run: sudo bash install.sh'; exit 1; }
SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[[ "$SOURCE" != /opt/sshbot-web ]] || { echo "Run from the extracted download directory, not /opt/sshbot-web"; exit 1; }
[[ -f "$SOURCE/web/app.py" && -f "$SOURCE/requirements.txt" ]] || { echo 'Extract the complete ZIP first; do not pipe this installer into bash.'; exit 1; }
. /etc/os-release
[[ "$ID" == ubuntu && ( "$VERSION_ID" == 22.04 || "$VERSION_ID" == 24.04 ) ]] || { echo 'Supported: Ubuntu 22.04 / 24.04'; exit 1; }
APP=/opt/sshbot-web
DATA=/var/lib/sshbot-web
CONF=/etc/sshbot-web
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP=/var/backups/sshbot-web/$STAMP
OLD_STOPPED=0
WEB_WAS_ACTIVE=0
DEPLOY_STARTED=0
SUCCESS=0
systemctl is-active --quiet sshbot-web && WEB_WAS_ACTIVE=1
fail(){ echo "ERROR: $*" >&2; exit 1; }
cleanup(){
  result=$?
  if (( result != 0 && SUCCESS == 0 )); then
    echo "Install failed. Backup (if created): $BACKUP"
    if (( DEPLOY_STARTED == 1 )); then
      systemctl stop sshbot-web 2>/dev/null || true
      for entry in /etc/sshbot-web/config.json /etc/nginx/sites-available/sshbot-web /etc/systemd/system/sshbot-web.service; do
        if [[ -f "$BACKUP$entry" ]]; then cp -a "$BACKUP$entry" "$entry"; fi
      done
      if [[ -d "$BACKUP/web" ]]; then
        rm -rf "$APP/web"
        cp -a "$BACKUP/web" "$APP/"
        cp "$BACKUP/requirements.txt" "$APP/requirements.txt"
        "$APP/venv/bin/pip" install -r "$APP/requirements.txt" || true
      else
        rm -f /etc/nginx/sites-enabled/sshbot-web
      fi
      systemctl daemon-reload
      nginx -t && systemctl reload nginx || true
      if (( WEB_WAS_ACTIVE == 1 )); then systemctl start sshbot-web || true; fi
    fi
    if (( OLD_STOPPED == 1 )); then
      systemctl stop sshbot-web 2>/dev/null || true
      systemctl start sshbot 2>/dev/null || true
      echo 'Previous sshbot service restarted.'
    fi
  fi
}
trap cleanup EXIT
read -r -p 'Domain (e.g. ssh.example.com, no https): ' DOMAIN
read -r -p 'Email for SSL renewal notices: ' EMAIL
read -r -p 'Your numeric Telegram ID (owner): ' OWNER_ID
read -r -s -p 'Telegram bot token: ' BOT_TOKEN; echo
[[ "$DOMAIN" =~ ^([a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}$ && ${#DOMAIN} -le 253 ]] || fail 'Invalid domain'
[[ "$EMAIL" =~ ^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$ ]] || fail 'Invalid email'
[[ "$OWNER_ID" =~ ^[1-9][0-9]{0,15}$ ]] || fail 'Invalid numeric owner ID'
[[ "$BOT_TOKEN" =~ ^[0-9]+:[a-zA-Z0-9_-]+$ ]] || fail 'Invalid bot token'
export DOMAIN EMAIL OWNER_ID BOT_TOKEN
# Never stop Apache, Caddy, or an unrelated listener to claim ports.
if command -v ss >/dev/null; then
  for port in 80 443; do
    listeners=$(ss -H -ltnp "sport = :$port" || true)
    if [[ -n "$listeners" && "$listeners" != *'"nginx"'* ]]; then
      fail "Port $port is occupied by another service. Use a separate VPS; existing services were not changed."
    fi
  done
  if [[ -n $(ss -H -ltn 'sport = :8765') ]] && ! systemctl is-active --quiet sshbot-web; then
    fail 'Port 8765 is occupied; use a separate VPS.'
  fi
fi
if [[ -d /etc/nginx ]] && grep -RFl --include='*' "server_name $DOMAIN" /etc/nginx/sites-enabled 2>/dev/null | grep -v '/sshbot-web$' >/dev/null; then
  fail 'This domain already has an Nginx site. Choose a dedicated subdomain.'
fi
python3 - <<'PY'
import os, socket, sys
try:
    addresses = sorted({r[4][0] for r in socket.getaddrinfo(os.environ['DOMAIN'], 80)})
    print('DNS addresses: ' + ', '.join(addresses))
except socket.gaierror:
    sys.exit('Domain does not resolve. Create DNS A/AAAA first.')
PY
echo 'DNS must point to THIS VPS, proxy OFF, inbound TCP 80 and 443 open.'
read -r -p 'Continue with this domain and bot? [yes/no]: ' ANSWER
[[ "$ANSWER" == yes ]] || fail 'Cancelled'
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip nginx certbot curl ca-certificates unzip
mkdir -p "$BACKUP" "$CONF" "$DATA" "$APP" /var/www/sshbot-acme
chmod 755 "$APP" /var/www/sshbot-acme
# Keep root-only backups; SQLite online backup creates a consistent snapshot.
for entry in /etc/sshbot.env /etc/sshbot-web/config.json /etc/nginx/sites-available/sshbot-web /etc/systemd/system/sshbot-web.service; do
  if [[ -f "$entry" ]]; then cp --parents "$entry" "$BACKUP/"; fi
done
if [[ -d /opt/sshbot/data ]]; then cp -a /opt/sshbot/data "$BACKUP/legacy-data"; fi
if [[ -f "$DATA/vault.sqlite3" ]]; then
  python3 - "$DATA" "$BACKUP" <<'PY'
import sqlite3,sys,shutil
from pathlib import Path
source,target=map(Path,sys.argv[1:])
with sqlite3.connect(source/'vault.sqlite3') as src, sqlite3.connect(target/'vault.sqlite3') as dst: src.backup(dst)
shutil.copy2(source/'vault.key', target/'vault.key')
PY
fi
if [[ -d "$APP/web" ]]; then cp -a "$APP/web" "$BACKUP/web"; cp "$APP/requirements.txt" "$BACKUP/"; fi
id sshbot-web &>/dev/null || useradd --system --home "$DATA" --shell /usr/sbin/nologin sshbot-web
DEPLOY_STARTED=1
systemctl stop sshbot-web 2>/dev/null || true
cp -a "$SOURCE/web" "$APP/"
cp "$SOURCE/requirements.txt" "$APP/requirements.txt"
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install --disable-pip-version-check -r "$APP/requirements.txt"
# Program files contain no secrets and must be readable by the service account.
chmod -R a+rX "$APP"
python3 - <<'PY'
import json,os
from pathlib import Path
p=Path('/etc/sshbot-web/config.json')
p.write_text(json.dumps({'bot_token':os.environ['BOT_TOKEN'],'owner_id':int(os.environ['OWNER_ID']),'public_url':'https://'+os.environ['DOMAIN'],'data_dir':'/var/lib/sshbot-web'},indent=2))
PY
chown root:sshbot-web "$CONF" "$CONF/config.json"
chmod 750 "$CONF"
chmod 640 "$CONF/config.json"
chown -R sshbot-web:sshbot-web "$DATA"
chmod 700 "$DATA"
cd "$APP"
# Pause legacy writes before making the migration snapshot.
if systemctl is-active --quiet sshbot; then systemctl stop sshbot; OLD_STOPPED=1; fi
if [[ -f /opt/sshbot/data/servers.json && ! -f "$DATA/migration-v5.done" ]]; then
  cp -a /opt/sshbot/data "$BACKUP/legacy-at-migration"
  "$APP/venv/bin/python" -m web.migrate --source /opt/sshbot/data/servers.json --key /opt/sshbot/data/keychain.key --target "$DATA"
  chown -R sshbot-web:sshbot-web "$DATA"
fi
cat > /etc/systemd/system/sshbot-web.service <<'UNIT'
[Unit]
Description=SSHBot Web and Telegram Mini App
After=network-online.target
Wants=network-online.target
[Service]
User=sshbot-web
Group=sshbot-web
WorkingDirectory=/opt/sshbot-web
Environment=SSHBOT_CONFIG=/etc/sshbot-web/config.json
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/opt/sshbot-web/venv/bin/uvicorn web.app:app --host 127.0.0.1 --port 8765 --workers 1 --ws-max-size 131072 --no-access-log
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/sshbot-web
[Install]
WantedBy=multi-user.target
UNIT
# HTTP challenge webroot; existing Nginx sites remain intact.
cat > /etc/nginx/sites-available/sshbot-web <<NGINX
server {
  listen 80;
  listen [::]:80;
  server_name $DOMAIN;
  location /.well-known/acme-challenge/ { root /var/www/sshbot-acme; }
  location / { return 503; }
}
NGINX
ln -sfn /etc/nginx/sites-available/sshbot-web /etc/nginx/sites-enabled/sshbot-web
nginx -t
systemctl enable --now nginx
systemctl reload nginx
certbot certonly --webroot -w /var/www/sshbot-acme --non-interactive --agree-tos --email "$EMAIL" --cert-name "$DOMAIN" -d "$DOMAIN" --keep-until-expiring
cat > /etc/nginx/sites-available/sshbot-web <<NGINX
server {
  listen 80;
  listen [::]:80;
  server_name $DOMAIN;
  location /.well-known/acme-challenge/ { root /var/www/sshbot-acme; }
  location / { return 301 https://\$host\$request_uri; }
}
server {
  listen 443 ssl;
  listen [::]:443 ssl;
  server_name $DOMAIN;
  ssl_certificate /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
  ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;
  ssl_protocols TLSv1.2 TLSv1.3;
  add_header Strict-Transport-Security "max-age=31536000" always;
  client_max_body_size 51m;
  location / {
    proxy_pass http://127.0.0.1:8765;
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
    proxy_buffering off;
  }
}
NGINX
mkdir -p /etc/letsencrypt/renewal-hooks/deploy
printf '#!/bin/sh\nnginx -t && systemctl reload nginx\n' > /etc/letsencrypt/renewal-hooks/deploy/sshbot-nginx.sh
chmod 755 /etc/letsencrypt/renewal-hooks/deploy/sshbot-nginx.sh
systemctl enable --now certbot.timer
nginx -t
systemctl reload nginx
systemctl daemon-reload
# Configure only the provided Telegram bot; fail visibly if token/network is invalid.
runuser -u sshbot-web -- "$APP/venv/bin/python" -m web.setup_bot
systemctl enable --now sshbot-web
for attempt in {1..15}; do
  if curl -fsS http://127.0.0.1:8765/health >/dev/null; then break; fi
  sleep 1
done
curl -fsS http://127.0.0.1:8765/health >/dev/null || fail 'App health check failed; run journalctl -u sshbot-web -n 80'
curl -fsS "https://$DOMAIN/health" >/dev/null || fail 'Public HTTPS health check failed; check DNS/firewall'
if (( OLD_STOPPED == 1 )); then systemctl disable sshbot; fi
SUCCESS=1
unset BOT_TOKEN
printf '\nInstalled: https://%s\nOpen the bot, send /start, then open SSHBot.\nOwner ID: %s\nBackup: %s\nLogs: journalctl -u sshbot-web -n 80 --no-pager\n' "$DOMAIN" "$OWNER_ID" "$BACKUP"
