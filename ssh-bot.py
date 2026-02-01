#!/usr/bin/env python3
import os, sys, json, time, threading, logging, re
import paramiko, pyte
from typing import Dict, Tuple

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ParseMode
)
from telegram.ext import (
    Updater, CommandHandler, MessageHandler, Filters,
    CallbackQueryHandler, CallbackContext
)

# ================= CONFIG =================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
BASE_DIR = "/opt/sshbot"
HOSTS_FILE = f"{BASE_DIR}/hosts.json"

TERM_COLS = 120
TERM_LINES = 200
MAX_TG = 3900

SSH_RE = re.compile(r"([^@]+)@([^:]+)(?::(\d+))?$")

# ================= STATE =================
SESSIONS: Dict[int, "SSHSession"] = {}
PENDING: Dict[int, Tuple[str, str, int]] = {}
LAST_CONN: Dict[int, Tuple[str, str, int, str]] = {}

# ================= UTIL =================
def load_hosts():
    if not os.path.exists(HOSTS_FILE):
        return {}
    with open(HOSTS_FILE) as f:
        return json.load(f)

def save_hosts(data):
    with open(HOSTS_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ================= KEYBOARDS =================
def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔌 Connect", callback_data="M:CONNECT")],
        [
            InlineKeyboardButton("🛑 Disconnect", callback_data="M:STOP"),
            InlineKeyboardButton("🔁 Reconnect", callback_data="M:RECONNECT"),
        ],
        [
            InlineKeyboardButton("🧹 Clear", callback_data="M:CLEAR"),
            InlineKeyboardButton("💾 Hosts", callback_data="M:HOSTS"),
        ],
        [
            InlineKeyboardButton("⌨️ Keys", callback_data="M:KEYS"),
            InlineKeyboardButton("❓ Help", callback_data="M:HELP"),
        ],
    ])

def keys_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("CTRL+C", callback_data="K:CTRL_C"),
            InlineKeyboardButton("CTRL+Z", callback_data="K:CTRL_Z"),
        ],
        [
            InlineKeyboardButton("↑", callback_data="K:UP"),
            InlineKeyboardButton("↓", callback_data="K:DOWN"),
            InlineKeyboardButton("←", callback_data="K:LEFT"),
            InlineKeyboardButton("→", callback_data="K:RIGHT"),
        ],
        [
            InlineKeyboardButton("ENTER", callback_data="K:ENTER"),
            InlineKeyboardButton("TAB", callback_data="K:TAB"),
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="M:BACK")]
    ])

# ================= SSH SESSION =================
class SSHSession:
    def __init__(self, chat, bot):
        self.chat = chat
        self.bot = bot
        self.client = None
        self.chan = None
        self.screen = pyte.Screen(TERM_COLS, TERM_LINES)
        self.stream = pyte.Stream(self.screen)
        self.stop = threading.Event()
        self.msg_id = None

    def start(self, user, host, port, pwd):
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            host, port=port, username=user,
            password=pwd, timeout=10,
            look_for_keys=False, allow_agent=False
        )
        self.chan = self.client.invoke_shell()
        self.chan.settimeout(0)

        msg = self.bot.send_message(
            self.chat, "```Connecting...```",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keys_menu()
        )
        self.msg_id = msg.message_id
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while not self.stop.is_set():
            if self.chan.recv_ready():
                data = self.chan.recv(4096)
                self.stream.feed(data.decode(errors="ignore"))
                self.render()
            time.sleep(0.05)

    def render(self):
        text = "\n".join(self.screen.display)
        text = text[-MAX_TG:]
        try:
            self.bot.edit_message_text(
                chat_id=self.chat,
                message_id=self.msg_id,
                text=f"```{text}```",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keys_menu()
            )
        except:
            pass

    def send(self, data):
        if self.chan:
            self.chan.send(data)

    def close(self):
        self.stop.set()
        try:
            self.chan.close()
            self.client.close()
        except:
            pass

# ================= COMMANDS =================
def start_cmd(update: Update, ctx: CallbackContext):
    update.message.reply_text(
        "🤖 SSHBot Ready",
        reply_markup=main_menu()
    )

def ssh_cmd(update: Update, ctx: CallbackContext):
    chat = update.effective_chat.id
    if not ctx.args:
        update.message.reply_text("Usage: /ssh user@host[:port]")
        return

    m = SSH_RE.match(ctx.args[0])
    if not m:
        update.message.reply_text("Invalid format")
        return

    user, host, port = m.group(1), m.group(2), int(m.group(3) or 22)
    PENDING[chat] = (user, host, port)
    update.message.reply_text("Send password with /pass <password> (deleted)")

def pass_cmd(update: Update, ctx: CallbackContext):
    chat = update.effective_chat.id
    if chat not in PENDING:
        return

    pwd = " ".join(ctx.args)
    user, host, port = PENDING.pop(chat)

    try: update.message.delete()
    except: pass

    sess = SSHSession(chat, ctx.bot)
    SESSIONS[chat] = sess
    LAST_CONN[chat] = (user, host, port, pwd)
    sess.start(user, host, port, pwd)

def text_msg(update: Update, ctx: CallbackContext):
    chat = update.effective_chat.id
    if chat in SESSIONS:
        try: ctx.bot.delete_message(chat, update.message.message_id)
        except: pass
        SESSIONS[chat].send(update.message.text + "\n")

# ================= CALLBACK =================
KEY_MAP = {
    "CTRL_C": "\x03", "CTRL_Z": "\x1a",
    "UP": "\x1b[A", "DOWN": "\x1b[B",
    "LEFT": "\x1b[D", "RIGHT": "\x1b[C",
    "ENTER": "\r", "TAB": "\t"
}

def cb(update: Update, ctx: CallbackContext):
    q = update.callback_query
    chat = q.message.chat_id
    data = q.data

    if data == "M:STOP":
        if chat in SESSIONS:
            SESSIONS[chat].close()
            del SESSIONS[chat]
        q.answer("Stopped")

    elif data == "M:CLEAR":
        if chat in SESSIONS:
            SESSIONS[chat].send("\x1b[2J\x1b[H")
        q.answer()

    elif data == "M:RECONNECT":
        if chat in LAST_CONN:
            u,h,p,pw = LAST_CONN[chat]
            sess = SSHSession(chat, ctx.bot)
            SESSIONS[chat] = sess
            sess.start(u,h,p,pw)
        q.answer("Reconnected")

    elif data == "M:KEYS":
        q.message.edit_reply_markup(keys_menu())

    elif data == "M:BACK":
        q.message.edit_reply_markup(main_menu())

    elif data.startswith("K:"):
        key = data[2:]
        if chat in SESSIONS and key in KEY_MAP:
            SESSIONS[chat].send(KEY_MAP[key])
        q.answer()

# ================= MAIN =================
def main():
    if not BOT_TOKEN:
        print("BOT_TOKEN missing")
        return

    os.makedirs(BASE_DIR, exist_ok=True)
    up = Updater(BOT_TOKEN, use_context=True)
    dp = up.dispatcher

    dp.add_handler(CommandHandler("start", start_cmd))
    dp.add_handler(CommandHandler("ssh", ssh_cmd))
    dp.add_handler(CommandHandler("pass", pass_cmd))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, text_msg))
    dp.add_handler(CallbackQueryHandler(cb))

    up.start_polling()
    up.idle()

if __name__ == "__main__":
    main()
