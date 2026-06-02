# SSHBot Pro v5 - Bottom Terminal Fix

Version: SSHBot Pro v5.0.0 - Real Keychain + Bottom Terminal Fix

Fixes:
- After successful SSH/Keychain connection, the bot no longer sends a separate success/menu message below the terminal.
- The terminal message stays as the latest message after connection.
- When showing upload/status/server helper messages during an active session, the terminal is recreated below them so it returns to the bottom.
- Keeps Real SSH Keychain support and multi-file upload.

Install:

```bash
curl -fsSL https://raw.githubusercontent.com/0fariid0/SSHBot/main/install.sh | sudo bash
```

Or:

```bash
curl -fsSL https://raw.githubusercontent.com/0fariid0/SSHBot/main/install.sh | sudo BOT_TOKEN='YOUR_TOKEN' bash
```

Check:

```bash
grep BOT_VERSION /opt/sshbot/ssh-bot.py
journalctl -u sshbot -n 80 --no-pager
```

Telegram:

```text
/version
```
