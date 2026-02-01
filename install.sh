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
if [[ $EUID -ne 0 ]]; then
  echo -e "${RED}${BOLD}❌ Please run this script as root${RESET}"
  exit 1
fi

clear
echo -e "${CYAN}${BOLD}"
echo "========================================="
echo "        SSHBot Installer"
echo "========================================="
echo -e "${RESET}"

# ================= PATHS =================
INSTALL_DIR="/opt/sshbot"
BOT_FILE="$INSTALL_DIR/ssh-bot.py"
ENV_FILE="$INSTALL_DIR/sshbot.env"
SERVICE_FILE="/etc/systemd/system/sshbot.service"

REPO_RAW_URL="https://github.com/0fariid0/SSHBot/raw/refs/heads/main/ssh-bot.py"

# ================= GET BOT TOKEN =================
# Prefer existing env BOT_TOKEN if provided
BOT_TOKEN="${BOT_TOKEN:-}"

trim() {
  # trims leading/trailing whitespace
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf "%s" "$s"
}

if [[ -z "$(trim "$BOT_TOKEN")" ]]; then
  # interactive prompt with retries
  for i in 1 2 3; do
    read -r -p "$(echo -e "${YELLOW}🤖 Enter your Telegram Bot Token: ${RESET}")" BOT_TOKEN || true
    BOT_TOKEN="$(trim "$BOT_TOKEN")"
    if [[ -n "$BOT_TOKEN" ]]; then
      break
    fi
    echo -e "${RED}❌ Bot token cannot be empty${RESET}"
  done
fi

BOT_TOKEN="$(trim "$BOT_TOKEN")"
if [[ -z "$BOT_TOKEN" ]]; then
  echo -e "${RED}${BOLD}❌ Bot token still empty. Aborting.${RESET}"
  echo -e "${YELLOW}Tip:${RESET} You can also run like:"
  echo -e "  ${BOLD}BOT_TOKEN=123:ABC ./install.sh${RESET}"
  exit 1
fi

# ================= INSTALL DEPS =================
echo -e "${BLUE}📦 Installing dependencies...${RESET}"
apt update -y >/dev/null 2>&1
apt install -y python3 python3-pip openssh-client curl ca-certificates >/dev/null 2>&1

pip3 install --upgrade pip >/dev/null 2>&1
pip3 install python-telegram-bot==13.15 paramiko pyte >/dev/null 2>&1

# ================= DOWNLOAD BOT =================
echo -e "${BLUE}⬇️  Downloading SSHBot...${RESET}"
mkdir -p "$INSTALL_DIR"

# Download & validate
curl -fsSL "$REPO_RAW_URL" -o "$BOT_FILE"

if [[ ! -s "$BOT_FILE" ]]; then
  echo -e "${RED}❌ Failed to download bot file (empty file)${RESET}"
  exit 1
fi

chmod +x "$BOT_FILE"

# ================= WRITE ENV FILE =================
# Safer than embedding token in unit file
echo -e "${BLUE}🔐 Writing environment file...${RESET}"
cat > "$ENV_FILE" <<EOF
BOT_TOKEN=$BOT_TOKEN
EOF
chmod 600 "$ENV_FILE"

# ================= CREATE SERVICE =================
echo -e "${BLUE}⚙️  Creating systemd service...${RESET}"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Telegram SSH Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=/usr/bin/python3 $BOT_FILE
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# ================= START SERVICE =================
echo -e "${BLUE}🚀 Starting bot service...${RESET}"
systemctl daemon-reload
systemctl enable sshbot >/dev/null 2>&1
systemctl restart sshbot

sleep 1

# ================= STATUS =================
if systemctl is-active --quiet sshbot; then
  echo -e "${GREEN}${BOLD}✅ SSHBot installed and running!${RESET}"
else
  echo -e "${RED}${BOLD}❌ SSHBot failed to start${RESET}"
  echo -e "${YELLOW}Check logs with:${RESET} journalctl -u sshbot -n 100 --no-pager"
  exit 1
fi

# ================= DONE =================
echo
echo -e "${CYAN}📌 Commands:${RESET}"
echo -e "  ${BOLD}systemctl status sshbot${RESET}"
echo -e "  ${BOLD}journalctl -u sshbot -f${RESET}"
echo
echo -e "${GREEN}🎉 Done! Enjoy your SSH Telegram bot.${RESET}"
