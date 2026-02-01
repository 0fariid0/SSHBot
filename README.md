# SSHBot

Easily access your servers using **SSH over a Telegram Bot**.  
Written in **Python**, designed for fast, interactive, and secure remote server access.

---

## 🚀 What is SSHBot?

**SSHBot** allows you to open and control SSH sessions directly from Telegram.  
No terminal, no VPN hopping — just your Telegram app.

It supports interactive shells, keyboard shortcuts, and command combinations, making it suitable for real server administration.

---

## ✨ Features

- 🔐 SSH access over Telegram
- ⌨️ Interactive terminal rendering
- 🧠 Keyboard combinations via commands:
  - `/ctrl c` → Ctrl + C
  - `/alt a` → Alt + A
  - `/shift x` → Shift + X
  - `/ctrl alt c` → Ctrl + Alt + C
- 🛑 Stop SSH sessions instantly
- 🧹 Automatically removes sensitive messages (like passwords)
- 🌍 English & Persian friendly
- ⚙️ Runs as a systemd service
- 🐧 Optimized for Linux servers

---

## 🤖 Sample Bot

Try the demo bot here:  
👉 **https://t.me/ssh4ccess_bot**

> ⚠️ This is a demo bot. Sessions may be limited or reset at any time.

---

## 📥 Installation (One-Line)

Run this command on your server:

```bash
curl -fsSL https://github.com/0fariid0/SSHBot/raw/refs/heads/main/install.sh | sudo bash
