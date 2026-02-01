
#!/usr/bin/env bash

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

# ================= ASK BOT TOKEN =================
read -rp "$(echo -e ${YELLOW}'🤖 Enter your Telegram Bot Token: '${RESET})" BOT_TOKEN

if [[ -z "$BOT_TOKEN" ]]; then
  echo -e "${RED}❌ Bot token cannot be empty${RESET}"
  exit 1
fi

# ================= PATHS =================
INSTALL_DIR="/opt/sshbot"
BOT_FILE="$INSTALL_DIR/ssh-bot.py"
SERVICE_FILE="/etc/systemd/system/sshbot.service"

# ================= INSTALL DEPS =================
echo -e "${BLUE}📦 Installing dependencies...${RESET}"
apt update -y >/dev/null 2>&1
apt install -y python3 python3-pip openssh-client >/dev/null 2>&1

pip3 install --upgrade pip >/dev/null 2>&1
pip3 install python-telegram-bot==13.15 paramiko pyte >/dev/null 2>&1

# ================= DOWNLOAD BOT =================
echo -e "${BLUE}⬇️  Downloading SSHBot...${RESET}"
mkdir -p "$INSTALL_DIR"

curl -fsSL \
  https://github.com/0fariid0/SSHBot/raw/refs/heads/main/ssh-bot.py \
  -o "$BOT_FILE"

if [[ ! -f "$BOT_FILE" ]]; then
  echo -e "${RED}❌ Failed to download bot file${RESET}"
  exit 1
fi

chmod +x "$BOT_FILE"

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
Environment=BOT_TOKEN=$BOT_TOKEN
ExecStart=/usr/bin/python3 $BOT_FILE
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# ================= START SERVICE =================
echo -e "${BLUE}🚀 Starting bot service...${RESET}"
systemctl daemon-reexec
systemctl daemon-reload
systemctl enable sshbot >/dev/null 2>&1
systemctl restart sshbot

sleep 1

# ================= STATUS =================
if systemctl is-active --quiet sshbot; then
  echo -e "${GREEN}${BOLD}✅ SSHBot installed and running!${RESET}"
else
  echo -e "${RED}${BOLD}❌ SSHBot failed to start${RESET}"
  echo -e "${YELLOW}Check logs with:${RESET} journalctl -u sshbot -f"
  exit 1
fi

# ================= DONE =================
echo
echo -e "${CYAN}📌 Commands:${RESET}"
echo -e "  ${BOLD}systemctl status sshbot${RESET}"
echo -e "  ${BOLD}journalctl -u sshbot -f${RESET}"
echo
echo -e "${GREEN}🎉 Done! Enjoy your SSH Telegram bot.${RESET}"
