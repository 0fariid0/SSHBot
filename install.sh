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
  echo -e "${YELLOW}Try:${RESET} curl -fsSL https://raw.githubusercontent.com/0fariid0/SSHBot/main/install.sh | sudo bash"
  exit 1
fi

clear || true
echo -e "${CYAN}${BOLD}"
echo "========================================="
echo "        SSHBot Installer (Pro - Keychain + Upload)"
echo "========================================="
echo -e "${RESET}"

# ================= REPO =================
REPO_OWNER="${REPO_OWNER:-0fariid0}"
REPO_NAME="${REPO_NAME:-SSHBot}"
REPO_BRANCH="${REPO_BRANCH:-main}"
REPO_URL="https://github.com/${REPO_OWNER}/${REPO_NAME}"
RAW_BASE="https://raw.githubusercontent.com/${REPO_OWNER}/${REPO_NAME}/${REPO_BRANCH}"

# ================= ASK BOT TOKEN =================
# Supports both:
#   BOT_TOKEN="123:ABC" bash install.sh
#   curl -fsSL .../install.sh | sudo bash
BOT_TOKEN="${BOT_TOKEN:-}"
if [[ -z "${BOT_TOKEN}" ]]; then
  if [[ -r /dev/tty ]]; then
    read -rp "$(echo -e ${YELLOW}'🤖 Enter your Telegram Bot Token: '${RESET})" BOT_TOKEN </dev/tty
  else
    echo -e "${RED}❌ Bot token cannot be empty${RESET}"
    echo -e "${YELLOW}Run like this:${RESET}"
    echo -e "  ${BOLD}curl -fsSL ${RAW_BASE}/install.sh | sudo BOT_TOKEN='YOUR_BOT_TOKEN' bash${RESET}"
    exit 1
  fi
fi
BOT_TOKEN="$(printf '%s' "${BOT_TOKEN}" | tr -d '\r\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
if [[ -z "${BOT_TOKEN}" ]]; then
  echo -e "${RED}❌ Bot token cannot be empty${RESET}"
  echo -e "${YELLOW}Example:${RESET} curl -fsSL ${RAW_BASE}/install.sh | sudo BOT_TOKEN='YOUR_BOT_TOKEN' bash"
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

run_as_bot() {
  if command -v runuser >/dev/null 2>&1; then
    env HOME="${INSTALL_DIR}" runuser -u "${BOT_USER}" -- "$@"
  else
    sudo -u "${BOT_USER}" -H "$@"
  fi
}

echo -e "${BLUE}👤 Ensuring service user exists...${RESET}"
if ! getent group "${BOT_GROUP}" >/dev/null 2>&1; then
  groupadd --system "${BOT_GROUP}"
fi
if ! id -u "${BOT_USER}" >/dev/null 2>&1; then
  useradd --system --home "${INSTALL_DIR}" --shell /usr/sbin/nologin --gid "${BOT_GROUP}" "${BOT_USER}"
fi

# ================= INSTALL DEPS =================
echo -e "${BLUE}📦 Installing dependencies...${RESET}"
apt update -y >/dev/null 2>&1
apt install -y python3 python3-venv python3-pip openssh-client curl ca-certificates sudo >/dev/null 2>&1

# ================= DIRS =================
echo -e "${BLUE}📁 Creating directories...${RESET}"
mkdir -p "${INSTALL_DIR}" "${DATA_DIR}" "${KEYS_DIR}" "${LOG_DIR}"
chown -R "${BOT_USER}:${BOT_GROUP}" "${INSTALL_DIR}" "${LOG_DIR}"
chmod 700 "${KEYS_DIR}" || true

# ================= PYTHON VENV =================
echo -e "${BLUE}🐍 Creating virtualenv (as ${BOT_USER})...${RESET}"
if [[ -d "${VENV_DIR}" ]]; then
  echo -e "${YELLOW}ℹ️ venv already exists: ${VENV_DIR}${RESET}"
else
  run_as_bot python3 -m venv "${VENV_DIR}"
fi

echo -e "${BLUE}📦 Installing Python packages (as ${BOT_USER})...${RESET}"
# Python 3.12 no longer includes setuptools/pkg_resources in venv by default.
# python-telegram-bot 13.x imports APScheduler, and APScheduler 3.x still needs pkg_resources.
run_as_bot "${VENV_DIR}/bin/python" -m ensurepip --upgrade >/dev/null 2>&1 || true
run_as_bot "${VENV_DIR}/bin/python" -m pip install -U --force-reinstall pip wheel "setuptools<81" >/dev/null

# IMPORTANT: urllib3<2 is required for python-telegram-bot 13.x compatibility.
# APScheduler/tzlocal are pinned to versions known to work with python-telegram-bot 13.15.
run_as_bot "${VENV_DIR}/bin/python" -m pip install -U \
  "setuptools<81" \
  "python-telegram-bot==13.15" \
  "APScheduler==3.6.3" \
  "tzlocal<3" \
  "urllib3<2" \
  certifi \
  paramiko \
  cryptography \
  pyte >/dev/null

# Fail early with a clear error if the old Telegram stack cannot import.
run_as_bot "${VENV_DIR}/bin/python" - <<'PY'
import pkg_resources
import telegram
import telegram.ext
import paramiko
import cryptography
import pyte
PY

# ================= DEPLOY BOT FILE =================
echo -e "${BLUE}⬇️  Deploying SSHBot...${RESET}"
if [[ -f "./ssh-bot.py" ]]; then
  cp -f "./ssh-bot.py" "${BOT_FILE}"
else
  echo -e "${YELLOW}ℹ️ Local ssh-bot.py not found; downloading from GitHub...${RESET}"
  curl -fsSL "${RAW_BASE}/ssh-bot.py" -o "${BOT_FILE}"
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
REPO_URL=${REPO_URL}

# Keychain: encrypted local password store for quick SSH login
KEYCHAIN_ENABLED=1
KEYCHAIN_KEY_FILE=${DATA_DIR}/keychain.key

# Uploads: temporary local downloads before SFTP upload to the server
UPLOAD_TMP_DIR=${DATA_DIR}/uploads
UPLOAD_CREATE_DIR=1
UPLOAD_OVERWRITE=1
MAX_UPLOAD_BYTES=0
DELETE_UPLOADED_TG_MESSAGE=0

# Terminal rendering:
TERM_COLS=120
TERM_LINES=200
UPDATE_INTERVAL=1.0
MAX_TG_CHARS=3900
EOF

chmod 600 "${ENV_FILE}"
chown root:root "${ENV_FILE}"

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

# ================= START =================
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
