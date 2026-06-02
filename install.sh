#!/usr/bin/env bash
set -euo pipefail

# ================= COLORS =================
RED="\e[31m"
GREEN="\e[32m"
YELLOW="\e[33m"
BLUE="\e[34m"
CYAN="\e[36m"
BOLD="\e[1m"
RESET="\e[0m"

# ================= CHECK ROOT =================
if [[ ${EUID:-0} -ne 0 ]]; then
  echo -e "${RED}${BOLD}❌ Please run this script as root${RESET}"
  exit 1
fi

clear
echo -e "${CYAN}${BOLD}"
echo "========================================="
echo "        SSHBot Installer (Pro - Fixed)"
echo "========================================="
echo -e "${RESET}"

# ================= ASK BOT TOKEN =================
read -rp "$(echo -e ${YELLOW}'🤖 Enter your Telegram Bot Token: '${RESET})" BOT_TOKEN
if [[ -z "${BOT_TOKEN}" ]]; then
  echo -e "${RED}❌ Bot token cannot be empty${RESET}"
  exit 1
fi

# ================= PATHS =================
INSTALL_DIR="/opt/sshbot"
BOT_FILE="${INSTALL_DIR}/ssh-bot.py"
VENV_DIR="${INSTALL_DIR}/venv"
DATA_DIR="${INSTALL_DIR}/data"
KEYS_DIR="${INSTALL_DIR}/keys"
LOG_DIR="/var/log/ssh-bot"

ENV_FILE="/etc/sshbot.env"
SERVICE_FILE="/etc/systemd/system/sshbot.service"

# ================= USER =================
BOT_USER="sshbot"
BOT_GROUP="sshbot"

echo -e "${BLUE}👤 Ensuring service user exists...${RESET}"
if ! id -u "${BOT_USER}" >/dev/null 2>&1; then
  useradd --system --home "${INSTALL_DIR}" --shell /usr/sbin/nologin "${BOT_USER}"
fi

# ================= INSTALL DEPS =================
echo -e "${BLUE}📦 Installing dependencies...${RESET}"
apt update -y >/dev/null 2>&1
apt install -y python3 python3-venv python3-pip openssh-client curl >/dev/null 2>&1

# ================= DIRS =================
echo -e "${BLUE}📁 Creating directories...${RESET}"
mkdir -p "${INSTALL_DIR}" "${DATA_DIR}" "${KEYS_DIR}" "${LOG_DIR}"
chown -R "${BOT_USER}:${BOT_GROUP}" "${INSTALL_DIR}" "${LOG_DIR}"
chmod 700 "${KEYS_DIR}" || true

# ================= PYTHON VENV (IMPORTANT: create as sshbot) =================
echo -e "${BLUE}🐍 Creating virtualenv (as ${BOT_USER})...${RESET}"
if [[ -d "${VENV_DIR}" ]]; then
  echo -e "${YELLOW}ℹ️ venv already exists: ${VENV_DIR}${RESET}"
else
  sudo -u "${BOT_USER}" -H python3 -m venv "${VENV_DIR}"
fi

echo -e "${BLUE}📦 Installing Python packages (as ${BOT_USER})...${RESET}"
sudo -u "${BOT_USER}" -H "${VENV_DIR}/bin/pip" install -U pip wheel setuptools >/dev/null 2>&1

# IMPORTANT: urllib3<2 is required to avoid crashes when PTB falls back
sudo -u "${BOT_USER}" -H "${VENV_DIR}/bin/pip" install \
  "python-telegram-bot==13.15" \
  "urllib3<2" \
  certifi \
  paramiko \
  pyte >/dev/null 2>&1

# ================= DEPLOY BOT FILE =================
echo -e "${BLUE}⬇️  Deploying SSHBot...${RESET}"
if [[ -f "./ssh-bot.py" ]]; then
  cp -f "./ssh-bot.py" "${BOT_FILE}"
else
  echo -e "${RED}❌ ssh-bot.py not found in current directory.${RESET}"
  echo -e "${YELLOW}Put ssh-bot.py next to install.sh and rerun.${RESET}"
  exit 1
fi

chmod +x "${BOT_FILE}"
chown "${BOT_USER}:${BOT_GROUP}" "${BOT_FILE}"

# ================= ENV FILE =================
echo -e "${BLUE}🧾 Writing env file...${RESET}"
cat > "${ENV_FILE}" <<EOF
BOT_TOKEN=${BOT_TOKEN}

# Security (HIGHLY RECOMMENDED):
# Put your Telegram numeric user id(s) here, comma-separated:
ALLOWED_USERS=
ALLOWED_CHATS=
PRIVATE_ONLY=1

# Session behavior:
SESSION_TIMEOUT=0
KEEPALIVE_SEC=30
STRICT_HOST_KEY=0

# Paths:
INSTALL_DIR=${INSTALL_DIR}
DATA_DIR=${DATA_DIR}
SERVER_DB=${DATA_DIR}/servers.json
LOG_DIR=${LOG_DIR}
LOG_FILE=${LOG_DIR}/ssh-bot.log
REPO_URL=https://github.com/ItzGlace/SSHBot

# Terminal rendering:
TERM_COLS=120
TERM_LINES=200
UPDATE_INTERVAL=1.0
MAX_TG_CHARS=3900
EOF

chmod 600 "${ENV_FILE}"

# ================= SYSTEMD SERVICE =================
echo -e "${BLUE}⚙️  Creating systemd service...${RESET}"
cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=Telegram SSH Bot (SSHBot)
After=network.target

[Service]
Type=simple
User=${BOT_USER}
Group=${BOT_GROUP}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV_DIR}/bin/python ${BOT_FILE}
Restart=always
RestartSec=5

# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=full
ReadWritePaths=${INSTALL_DIR} ${LOG_DIR}

[Install]
WantedBy=multi-user.target
EOF

echo -e "${BLUE}🚀 Starting bot service...${RESET}"
systemctl daemon-reload
systemctl enable sshbot >/dev/null 2>&1
systemctl restart sshbot

sleep 1
if systemctl is-active --quiet sshbot; then
  echo -e "${GREEN}${BOLD}✅ SSHBot installed and running!${RESET}"
else
  echo -e "${RED}${BOLD}❌ SSHBot failed to start${RESET}"
  echo -e "${YELLOW}Check logs with:${RESET} journalctl -u sshbot -n 80 --no-pager"
  exit 1
fi

echo
echo -e "${CYAN}📌 Commands:${RESET}"
echo -e "  ${BOLD}systemctl status sshbot${RESET}"
echo -e "  ${BOLD}journalctl -u sshbot -f${RESET}"
echo -e "  ${BOLD}nano ${ENV_FILE}${RESET}"
echo
echo -e "${GREEN}🎉 Done!${RESET}"
