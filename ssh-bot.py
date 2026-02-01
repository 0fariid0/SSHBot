#!/usr/bin/env python3
import os
import sys
import time
import json
import threading
import logging
import logging.handlers
import re
import html
from dataclasses import dataclass, field
from typing import Dict, Tuple, List, Optional, Any
from urllib.parse import quote as urlquote, unquote as urlunquote

import paramiko
import pyte

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ParseMode,
    ForceReply,
)
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    Filters,
    CallbackQueryHandler,
    CallbackContext,
)

# ================= CONFIG =================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

TERM_COLS = int(os.environ.get("TERM_COLS", "120"))
TERM_LINES = int(os.environ.get("TERM_LINES", "200"))
UPDATE_INTERVAL = float(os.environ.get("UPDATE_INTERVAL", "1.0"))
MAX_TG_CHARS = int(os.environ.get("MAX_TG_CHARS", "3900"))

# Session & security
SESSION_TIMEOUT = int(os.environ.get("SESSION_TIMEOUT", "0"))  # seconds; 0=disabled
KEEPALIVE_SEC = int(os.environ.get("KEEPALIVE_SEC", "30"))
PRIVATE_ONLY = os.environ.get("PRIVATE_ONLY", "0").strip() in ("1", "true", "yes", "on")
STRICT_HOST_KEY = os.environ.get("STRICT_HOST_KEY", "0").strip() in ("1", "true", "yes", "on")

# Access control (recommended)
def _parse_csv_ints(val: str) -> List[int]:
    out: List[int] = []
    for x in (val or "").split(","):
        x = x.strip()
        if not x:
            continue
        try:
            out.append(int(x))
        except Exception:
            pass
    return out

ALLOWED_USERS = set(_parse_csv_ints(os.environ.get("ALLOWED_USERS", "")))
ALLOWED_CHATS = set(_parse_csv_ints(os.environ.get("ALLOWED_CHATS", "")))

# Paths
INSTALL_DIR = os.environ.get("INSTALL_DIR", "/opt/sshbot")
DATA_DIR = os.environ.get("DATA_DIR", f"{INSTALL_DIR}/data")
SERVER_DB = os.environ.get("SERVER_DB", f"{DATA_DIR}/servers.json")

LOG_DIR = os.environ.get("LOG_DIR", "/var/log/ssh-bot")
LOG_FILE = os.environ.get("LOG_FILE", f"{LOG_DIR}/ssh-bot.log")

REPO_URL = os.environ.get("REPO_URL", "https://github.com/ItzGlace/SSHBot")

# ================= LOGGING =================
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

logger = logging.getLogger("ssh-bot")
logger.setLevel(logging.INFO)

fmt = logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s")
fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3)
fh.setFormatter(fmt)
logger.addHandler(fh)

sh = logging.StreamHandler(sys.stdout)
sh.setFormatter(fmt)
logger.addHandler(sh)

# ================= TYPES / STATE =================
SessionKey = Tuple[int, int]  # (chat_id, user_id)

SSH_RE = re.compile(r"([^@]+)@([^:]+)(?::(\d+))?$")

KEYS = {
    "TAB": "\t",
    "ENTER": "\r",
    "ESC": "\x1b",
    "BS": "\x7f",
    "UP": "\x1b[A",
    "DOWN": "\x1b[B",
    "LEFT": "\x1b[D",
    "RIGHT": "\x1b[C",
    "PGUP": "\x1b[5~",
    "PGDN": "\x1b[6~",
    "NANO_EXIT": "\x18",  # CTRL+X
}

MACROS = {
    "CTRL_C": "\x03",
    "CTRL_Z": "\x1a",
    "CTRL_D": "\x04",
    "CTRL_L": "\x0c",
    "CTRL_R": "\x12",
    "CTRL_U": "\x15",
    "CTRL_W": "\x17",
    "CTRL_A": "\x01",
    "CTRL_E": "\x05",
}

QUICK_CMDS = {
    "UPTIME": "uptime\n",
    "DF": "df -h\n",
    "FREE": "free -h\n",
    "WHOAMI": "whoami\n",
    "PWD": "pwd\n",
    "LS": "ls -lah\n",
    "CLEAR": "clear\n",
}

@dataclass
class PendingConn:
    user: str
    host: str
    port: int = 22
    server_name: str = ""
    created_at: float = field(default_factory=time.time)

@dataclass
class WizardState:
    step: str
    data: Dict[str, Any] = field(default_factory=dict)
    prompt_msg_id: int = 0
    created_at: float = field(default_factory=time.time)

STATE_LOCK = threading.Lock()
DATA_LOCK = threading.Lock()

SESSIONS: Dict[SessionKey, "SSHSession"] = {}
PENDING: Dict[SessionKey, PendingConn] = {}
WIZARD: Dict[SessionKey, WizardState] = {}
MENUS: Dict[SessionKey, int] = {}  # last menu message id


# ================= UTIL =================
def now_ts() -> float:
    return time.time()

def html_pre(text: str) -> str:
    # Escape as HTML for Telegram <pre>
    return f"<pre>{html.escape(text)}</pre>"

def clamp_tg(text: str) -> str:
    # clamp by Telegram message char limit, from bottom
    lines = text.splitlines()
    out: List[str] = []
    for line in reversed(lines):
        out.insert(0, line)
        if len("\n".join(out)) > MAX_TG_CHARS:
            out.pop(0)
            break
    return "\n".join(out)

def is_private_chat(update: Update) -> bool:
    try:
        return update.effective_chat and update.effective_chat.type == "private"
    except Exception:
        return False

def is_authorized(update: Update) -> bool:
    chat_id = update.effective_chat.id if update.effective_chat else 0
    user_id = update.effective_user.id if update.effective_user else 0

    if PRIVATE_ONLY and not is_private_chat(update):
        return False

    # if allowlists are empty -> open (keeps backward compatibility)
    if ALLOWED_USERS:
        if user_id not in ALLOWED_USERS:
            return False
    if ALLOWED_CHATS:
        if chat_id not in ALLOWED_CHATS:
            return False
    return True

def session_key_from_update(update: Update) -> SessionKey:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    return (chat_id, user_id)

def session_key_from_query(update: Update) -> SessionKey:
    q = update.callback_query
    chat_id = q.message.chat_id
    user_id = q.from_user.id
    return (chat_id, user_id)

def load_server_db() -> Dict[str, Any]:
    with DATA_LOCK:
        try:
            if not os.path.exists(SERVER_DB):
                return {"users": {}}
            with open(SERVER_DB, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {"users": {}}
            if "users" not in data or not isinstance(data["users"], dict):
                data["users"] = {}
            return data
        except Exception:
            logger.exception("Failed to load server db")
            return {"users": {}}

def save_server_db(db: Dict[str, Any]) -> None:
    with DATA_LOCK:
        try:
            tmp = SERVER_DB + ".tmp"
            os.makedirs(os.path.dirname(SERVER_DB), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(db, f, ensure_ascii=False, indent=2)
            os.replace(tmp, SERVER_DB)
        except Exception:
            logger.exception("Failed to save server db")

def get_user_servers(user_id: int) -> Dict[str, Any]:
    db = load_server_db()
    u = db["users"].get(str(user_id), {})
    servers = u.get("servers", {})
    if not isinstance(servers, dict):
        servers = {}
    return servers

def set_user_servers(user_id: int, servers: Dict[str, Any], default: str = "") -> None:
    db = load_server_db()
    u = db["users"].get(str(user_id), {})
    if not isinstance(u, dict):
        u = {}
    u["servers"] = servers
    if default:
        u["default"] = default
    db["users"][str(user_id)] = u
    save_server_db(db)

def get_user_default_server(user_id: int) -> str:
    db = load_server_db()
    u = db["users"].get(str(user_id), {})
    if isinstance(u, dict):
        d = u.get("default", "")
        if isinstance(d, str):
            return d
    return ""

def set_user_default_server(user_id: int, name: str) -> None:
    db = load_server_db()
    u = db["users"].get(str(user_id), {})
    if not isinstance(u, dict):
        u = {}
    u["default"] = name
    if "servers" not in u or not isinstance(u["servers"], dict):
        u["servers"] = {}
    db["users"][str(user_id)] = u
    save_server_db(db)

def validate_server_name(name: str) -> Optional[str]:
    name = (name or "").strip()
    if not name:
        return "Server name cannot be empty."
    if len(name) > 32:
        return "Server name is too long (max 32)."
    if any(c in name for c in [":", "|", "\n", "\r"]):
        return "Server name contains invalid characters."
    return None

def parse_target(text: str) -> Optional[Tuple[str, str, int]]:
    text = (text or "").strip()
    m = SSH_RE.match(text)
    if not m:
        return None
    user, host, port = m.group(1), m.group(2), int(m.group(3) or 22)
    return user, host, port


# ================= UI (INLINE "GLASS" BUTTONS) =================
def keyboard_main(user_id: int) -> InlineKeyboardMarkup:
    # Main menu (inline buttons)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔌 اتصال", callback_data="M:CONNECT"),
                InlineKeyboardButton("📚 سرورها", callback_data="M:SERVERS"),
            ],
            [
                InlineKeyboardButton("📊 وضعیت", callback_data="M:STATUS"),
                InlineKeyboardButton("🛑 قطع", callback_data="M:STOP"),
            ],
            [
                InlineKeyboardButton("➕ افزودن سرور", callback_data="M:ADD_SERVER"),
                InlineKeyboardButton("❓ راهنما", callback_data="M:HELP"),
            ],
        ]
    )

def keyboard_servers_list(user_id: int) -> InlineKeyboardMarkup:
    servers = get_user_servers(user_id)
    default = get_user_default_server(user_id)

    rows: List[List[InlineKeyboardButton]] = []
    # server buttons
    for name in sorted(servers.keys(), key=lambda s: s.lower()):
        star = "⭐ " if name == default else ""
        rows.append([InlineKeyboardButton(f"{star}🖥 {name}", callback_data=f"SV:OPEN:{urlquote(name)}")])

    # footer
    rows.append(
        [
            InlineKeyboardButton("➕ اضافه", callback_data="M:ADD_SERVER"),
            InlineKeyboardButton("⬅️ منو", callback_data="M:MENU"),
        ]
    )
    return InlineKeyboardMarkup(rows)

def keyboard_server_actions(name: str) -> InlineKeyboardMarkup:
    qname = urlquote(name)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔌 اتصال", callback_data=f"SV:CONNECT:{qname}"),
                InlineKeyboardButton("⭐ پیش‌فرض", callback_data=f"SV:DEFAULT:{qname}"),
            ],
            [
                InlineKeyboardButton("🗑 حذف", callback_data=f"SV:DELETE:{qname}"),
                InlineKeyboardButton("⬅️ لیست", callback_data="M:SERVERS"),
            ],
        ]
    )

def keyboard_wizard_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="W:CANCEL")]])

# ================= SSH SESSION =================
class SSHSession:
    def __init__(self, key: SessionKey, bot):
        self.key = key
        self.chat_id, self.user_id = key
        self.bot = bot

        self.client: Optional[paramiko.SSHClient] = None
        self.chan = None
        self.stop = threading.Event()
        self.thread: Optional[threading.Thread] = None

        self.screen = pyte.Screen(TERM_COLS, TERM_LINES)
        self.stream = pyte.Stream(self.screen)

        self.message_id: Optional[int] = None
        self.last_render = ""
        self.last_sent = ""

        self.connected_at = now_ts()
        self.last_activity = now_ts()
        self.target = ""  # user@host:port
        self.kb_page = 0

    # ---------- CONNECT ----------
    def start(self, user: str, host: str, port: int, password: str) -> Tuple[bool, Optional[str]]:
        try:
            self.target = f"{user}@{host}:{port}"
            self.client = paramiko.SSHClient()

            if STRICT_HOST_KEY:
                # Strict host key checking
                self.client.load_system_host_keys()
                self.client.set_missing_host_key_policy(paramiko.RejectPolicy())
            else:
                self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            self.client.connect(
                host,
                port=port,
                username=user,
                password=password,
                look_for_keys=False,
                allow_agent=False,
                timeout=15,
                banner_timeout=15,
                auth_timeout=15,
            )

            try:
                tr = self.client.get_transport()
                if tr:
                    tr.set_keepalive(max(5, KEEPALIVE_SEC))
            except Exception:
                pass

            self.chan = self.client.invoke_shell(width=TERM_COLS, height=TERM_LINES)
            self.chan.settimeout(0)

            msg = self.bot.send_message(
                self.chat_id,
                text=html_pre("Connecting..."),
                parse_mode=ParseMode.HTML,
                reply_markup=self.keyboard(),
                disable_web_page_preview=True,
            )
            self.message_id = msg.message_id

            self.thread = threading.Thread(target=self.loop, daemon=True)
            self.thread.start()
            return True, None

        except Exception as e:
            logger.exception("SSH connect failed")
            return False, str(e)

    # ---------- LOOP ----------
    def loop(self):
        last_update = 0.0
        while not self.stop.is_set():
            try:
                # inactivity timeout
                if SESSION_TIMEOUT > 0 and (now_ts() - self.last_activity) > SESSION_TIMEOUT:
                    logger.info("Session timeout: %s", self.target)
                    break

                if self.chan and self.chan.recv_ready():
                    data = self.chan.recv(4096)
                    if not data:
                        break
                    self.last_activity = now_ts()
                    self.stream.feed(data.decode(errors="replace"))

                now = now_ts()
                if now - last_update >= UPDATE_INTERVAL:
                    self.render_and_update()
                    last_update = now

                time.sleep(0.05)
            except Exception:
                logger.exception("Reader loop error")
                break

        # ensure close is called
        try:
            self.close()
        except Exception:
            pass

    # ---------- RENDER ----------
    def render_and_update(self):
        text = "\n".join(self.screen.display).rstrip()

        if text == self.last_render:
            return

        self.last_render = text
        safe = clamp_tg(text)

        if safe == self.last_sent:
            return

        try:
            self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.message_id,
                text=html_pre(safe),
                parse_mode=ParseMode.HTML,
                reply_markup=self.keyboard(),
                disable_web_page_preview=True,
            )
            self.last_sent = safe
        except Exception as e:
            # ignore frequent "message is not modified" or edit race
            logger.debug("Edit failed: %s", e)

    # ---------- INPUT ----------
    def send(self, text: str):
        try:
            if self.chan and not self.stop.is_set():
                self.chan.send(text)
                self.last_activity = now_ts()
        except Exception:
            logger.exception("Send failed")

    # ---------- KEYBOARD ----------
    def keyboard(self) -> InlineKeyboardMarkup:
        # Multi-page keyboard (glass buttons): keys + macros + quick cmds + menu
        if self.kb_page == 0:
            rows = [
                [
                    InlineKeyboardButton("TAB", callback_data="K:TAB"),
                    InlineKeyboardButton("ENTER", callback_data="K:ENTER"),
                    InlineKeyboardButton("ESC", callback_data="K:ESC"),
                    InlineKeyboardButton("BS", callback_data="K:BS"),
                ],
                [
                    InlineKeyboardButton("↑", callback_data="K:UP"),
                    InlineKeyboardButton("↓", callback_data="K:DOWN"),
                    InlineKeyboardButton("←", callback_data="K:LEFT"),
                    InlineKeyboardButton("→", callback_data="K:RIGHT"),
                ],
                [
                    InlineKeyboardButton("PGUP", callback_data="K:PGUP"),
                    InlineKeyboardButton("PGDN", callback_data="K:PGDN"),
                    InlineKeyboardButton("Ctrl+C", callback_data="MC:CTRL_C"),
                    InlineKeyboardButton("Ctrl+Z", callback_data="MC:CTRL_Z"),
                ],
                [
                    InlineKeyboardButton("Nano Exit (Ctrl+X)", callback_data="K:NANO_EXIT"),
                ],
                [
                    InlineKeyboardButton("⚡ سریع", callback_data="KB:PAGE:1"),
                    InlineKeyboardButton("📚 سرورها", callback_data="A:SERVERS"),
                    InlineKeyboardButton("🛑 قطع", callback_data="A:STOP"),
                ],
            ]
            return InlineKeyboardMarkup(rows)

        if self.kb_page == 1:
            rows = [
                [
                    InlineKeyboardButton("uptime", callback_data="QC:UPTIME"),
                    InlineKeyboardButton("df -h", callback_data="QC:DF"),
                    InlineKeyboardButton("free -h", callback_data="QC:FREE"),
                ],
                [
                    InlineKeyboardButton("whoami", callback_data="QC:WHOAMI"),
                    InlineKeyboardButton("pwd", callback_data="QC:PWD"),
                    InlineKeyboardButton("ls", callback_data="QC:LS"),
                ],
                [
                    InlineKeyboardButton("clear", callback_data="QC:CLEAR"),
                    InlineKeyboardButton("Ctrl+L", callback_data="MC:CTRL_L"),
                    InlineKeyboardButton("Ctrl+D", callback_data="MC:CTRL_D"),
                ],
                [
                    InlineKeyboardButton("🔙 بازگشت", callback_data="KB:PAGE:0"),
                    InlineKeyboardButton("📚 سرورها", callback_data="A:SERVERS"),
                    InlineKeyboardButton("🛑 قطع", callback_data="A:STOP"),
                ],
            ]
            return InlineKeyboardMarkup(rows)

        # fallback
        self.kb_page = 0
        return self.keyboard()

    # ---------- CLOSE ----------
    def close(self):
        self.stop.set()
        try:
            if self.chan:
                try:
                    self.chan.close()
                except Exception:
                    pass
            if self.client:
                try:
                    self.client.close()
                except Exception:
                    pass
        except Exception:
            pass

        try:
            if self.message_id:
                self.bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=self.message_id,
                    text=html_pre("Session closed"),
                    parse_mode=ParseMode.HTML,
                    reply_markup=None,
                    disable_web_page_preview=True,
                )
        except Exception as e:
            logger.debug("Could not update closed message: %s", e)

        logger.info("Session closed %s", self.target or str(self.key))


# ================= SESSION HELPERS =================
def get_session(key: SessionKey) -> Optional[SSHSession]:
    with STATE_LOCK:
        return SESSIONS.get(key)

def stop_session(key: SessionKey) -> bool:
    with STATE_LOCK:
        s = SESSIONS.pop(key, None)
    if s:
        try:
            s.close()
        except Exception:
            logger.exception("Error closing session")
        return True
    return False

def clear_wizard(key: SessionKey) -> None:
    with STATE_LOCK:
        WIZARD.pop(key, None)
        PENDING.pop(key, None)

def set_wizard(key: SessionKey, st: WizardState) -> None:
    with STATE_LOCK:
        WIZARD[key] = st

def get_wizard(key: SessionKey) -> Optional[WizardState]:
    with STATE_LOCK:
        return WIZARD.get(key)

def set_pending(key: SessionKey, p: PendingConn) -> None:
    with STATE_LOCK:
        PENDING[key] = p

def get_pending(key: SessionKey) -> Optional[PendingConn]:
    with STATE_LOCK:
        return PENDING.get(key)

# ================= MODIFIER HELPERS (kept) =================
def parse_combo_tokens(tokens: List[str]) -> Tuple[List[str], str]:
    merged: List[str] = []
    for t in tokens:
        parts = re.split(r"[+]", t)
        for p in parts:
            p = p.strip()
            if p:
                merged.append(p.lower())

    mods = []
    key = ""
    for tok in merged:
        if tok in ("ctrl", "control"):
            if "CTRL" not in mods:
                mods.append("CTRL")
        elif tok in ("alt", "meta"):
            if "ALT" not in mods:
                mods.append("ALT")
        elif tok in ("shift",):
            if "SHIFT" not in mods:
                mods.append("SHIFT")
        else:
            key = tok
    return mods, key

def build_sequence_from_mods_and_key(mods: List[str], key_token: str) -> str:
    if not key_token:
        return ""

    ukey = key_token.upper()
    if ukey in KEYS:
        seq = KEYS[ukey]
        if "ALT" in mods:
            return "\x1b" + seq
        return seq

    ch = key_token
    if len(ch) == 0:
        return ""
    ch0 = ch[0]

    seq = ""
    if "ALT" in mods:
        seq += "\x1b"

    if "CTRL" in mods:
        c = ch0.lower()
        if "a" <= c <= "z":
            ctrl_char = chr(ord(c) - 96)
            seq += ctrl_char
            return seq
        try:
            seq += chr(ord(ch0) & 0x1f)
            return seq
        except Exception:
            seq += ch0
            return seq
    else:
        if "SHIFT" in mods:
            seq += ch0.upper()
        else:
            seq += ch0
        return seq

# ================= MENU / WIZARD =================
def send_menu(update: Update, ctx: CallbackContext, text: str = ""):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if not text:
        text = (
            "SSHBot آماده است ✅\n\n"
            "با دکمه‌ها کار کن یا دستورها رو تایپ کن.\n"
            f"<a href=\"{REPO_URL}\">source code</a> - by @EmptyPoll"
        )

    msg = update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard_main(user_id),
        disable_web_page_preview=True,
    )
    try:
        key = session_key_from_update(update)
        with STATE_LOCK:
            MENUS[key] = msg.message_id
    except Exception:
        pass

def wizard_ask_target(update: Update, ctx: CallbackContext):
    key = session_key_from_update(update)
    chat_id, user_id = key

    prompt = ctx.bot.send_message(
        chat_id,
        "🔌 لطفاً مقصد SSH رو بفرست:\n"
        "<code>user@host</code> یا <code>user@host:port</code>\n\n"
        "مثال: <code>root@1.2.3.4:22</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard_wizard_cancel(),
        reply_to_message_id=update.effective_message.message_id,
    )
    set_wizard(key, WizardState(step="AWAIT_TARGET", prompt_msg_id=prompt.message_id))

def wizard_ask_password(ctx: CallbackContext, key: SessionKey):
    chat_id, user_id = key
    p = get_pending(key)
    if not p:
        return

    prompt = ctx.bot.send_message(
        chat_id,
        f"🔐 پسورد برای <b>{html.escape(p.user)}@{html.escape(p.host)}:{p.port}</b> رو بفرست.\n"
        "پیام پسورد تا جای ممکن حذف میشه ✅",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard_wizard_cancel(),
    )
    st = get_wizard(key) or WizardState(step="AWAIT_PASSWORD")
    st.step = "AWAIT_PASSWORD"
    st.prompt_msg_id = prompt.message_id
    st.data.update({"user": p.user, "host": p.host, "port": p.port, "server_name": p.server_name})
    set_wizard(key, st)

def wizard_start_add_server(update: Update, ctx: CallbackContext):
    key = session_key_from_update(update)
    chat_id, user_id = key
    prompt = ctx.bot.send_message(
        chat_id,
        "➕ اسم سرور رو بفرست (حداکثر ۳۲ کاراکتر):",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard_wizard_cancel(),
    )
    set_wizard(key, WizardState(step="ADD_SERVER_NAME", prompt_msg_id=prompt.message_id))

def wizard_process_text(update: Update, ctx: CallbackContext) -> bool:
    """
    Return True if message was consumed by wizard.
    """
    key = session_key_from_update(update)
    st = get_wizard(key)
    if not st:
        return False

    chat_id, user_id = key
    msg = update.message
    if not msg:
        return False

    text = (msg.text or "").strip()

    # In groups: require reply-to prompt for safety; in private accept anyway.
    if update.effective_chat.type != "private":
        if not msg.reply_to_message or msg.reply_to_message.message_id != st.prompt_msg_id:
            return False

    def _try_delete():
        try:
            msg.delete()
        except Exception:
            try:
                ctx.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
            except Exception:
                pass

    if st.step == "AWAIT_TARGET":
        target = parse_target(text)
        if not target:
            ctx.bot.send_message(
                chat_id,
                "❌ فرمت اشتباهه. باید مثل این باشه:\n<code>user@host</code> یا <code>user@host:22</code>",
                parse_mode=ParseMode.HTML,
            )
            return True
        user, host, port = target
        _try_delete()  # hide target in chat (optional privacy)

        set_pending(key, PendingConn(user=user, host=host, port=port))
        wizard_ask_password(ctx, key)
        return True

    if st.step == "AWAIT_PASSWORD":
        pwd = text
        _try_delete()  # password privacy

        p = get_pending(key)
        if not p:
            clear_wizard(key)
            ctx.bot.send_message(chat_id, "❌ درخواست اتصال پیدا نشد. دوباره تلاش کن.", parse_mode=ParseMode.HTML)
            return True

        # connect
        # stop any existing session for that user
        stop_session(key)

        sess = SSHSession(key, ctx.bot)
        with STATE_LOCK:
            SESSIONS[key] = sess

        ok, err = sess.start(p.user, p.host, p.port, pwd)
        clear_wizard(key)  # clears pending too
        if not ok:
            with STATE_LOCK:
                SESSIONS.pop(key, None)
            ctx.bot.send_message(
                chat_id,
                f"❌ اتصال ناموفق بود:\n<code>{html.escape(str(err))}</code>",
                parse_mode=ParseMode.HTML,
            )
        else:
            ctx.bot.send_message(
                chat_id,
                f"✅ وصل شدی به <b>{html.escape(sess.target)}</b>\n"
                "ترمینال بالاست. پیام‌های متنی رو به عنوان ورودی می‌فرستم (و پاک می‌کنم).",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard_main(user_id),
            )
        return True

    if st.step == "ADD_SERVER_NAME":
        err = validate_server_name(text)
        if err:
            ctx.bot.send_message(chat_id, f"❌ {html.escape(err)}", parse_mode=ParseMode.HTML)
            return True
        name = text.strip()
        _try_delete()

        st.step = "ADD_SERVER_TARGET"
        st.data["name"] = name
        # ask for target
        prompt = ctx.bot.send_message(
            chat_id,
            f"حالا مقصد SSH برای <b>{html.escape(name)}</b> رو بفرست:\n"
            "<code>user@host</code> یا <code>user@host:port</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard_wizard_cancel(),
        )
        st.prompt_msg_id = prompt.message_id
        set_wizard(key, st)
        return True

    if st.step == "ADD_SERVER_TARGET":
        name = str(st.data.get("name", "")).strip()
        target = parse_target(text)
        if not target:
            ctx.bot.send_message(chat_id, "❌ فرمت اشتباهه. مثال: <code>root@1.2.3.4:22</code>", parse_mode=ParseMode.HTML)
            return True
        user, host, port = target
        _try_delete()

        servers = get_user_servers(user_id)
        servers[name] = {"user": user, "host": host, "port": port, "created_at": int(now_ts()), "last_used": int(now_ts())}
        set_user_servers(user_id, servers)
        clear_wizard(key)

        ctx.bot.send_message(
            chat_id,
            f"✅ سرور <b>{html.escape(name)}</b> ذخیره شد.",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard_servers_list(user_id),
        )
        return True

    # unknown step -> clear
    clear_wizard(key)
    return False


# ================= COMMAND HANDLERS =================
def guard(update: Update) -> bool:
    if not is_authorized(update):
        try:
            update.effective_message.reply_text("⛔ دسترسی نداری یا فقط باید در چت خصوصی استفاده بشه.")
        except Exception:
            pass
        return False
    return True

def start_cmd(update: Update, ctx: CallbackContext):
    if not guard(update):
        return
    send_menu(update, ctx)

def menu_cmd(update: Update, ctx: CallbackContext):
    if not guard(update):
        return
    send_menu(update, ctx)

def help_cmd(update: Update, ctx: CallbackContext):
    if not guard(update):
        return
    text = (
        "HELP — Commands / راهنما — دستورات\n\n"
        "✅ حالت دکمه‌ای (پیشنهادی):\n"
        " /start یا /menu رو بزن و از دکمه‌ها برای اتصال/مدیریت سرورها استفاده کن.\n\n"
        "Core flow / نحوه استفاده اصلی (دستورها همچنان کار می‌کنن):\n"
        "1) `/ssh user@host[:port]` — آماده‌سازی اتصال (بعدش پسورد رو بفرست).\n"
        "   Example: `/ssh alice@example.com:22`\n"
        "2) `/pass <password>` — ارسال پسورد (بات پیام رو برای حریم خصوصی حذف می‌کنه).\n"
        "3) وقتی سشن فعال شد، هر پیام متنی به عنوان ورودی به SSH ارسال میشه و پیام از چت حذف میشه.\n\n"
        "Stopping / قطع سشن:\n"
        "`/stop` — stop the current SSH session.\n"
        "`/ssh` بدون آرگومان — قطع سشن فعلی.\n\n"
        "Multi-server / چند سرور:\n"
        "`/servers` — لیست سرورهای ذخیره شده.\n"
        "`/addserver <name> user@host[:port]` — اضافه کردن سریع.\n"
        "`/delserver <name>` — حذف.\n"
        "یا از دکمه «سرورها» استفاده کن.\n\n"
        "Special buttons / دکمه‌های ترمینال:\n"
        "TAB, ENTER, ESC, BS, ↑ ↓ ← →, PGUP, PGDN, Ctrl+C, Ctrl+Z, ...\n\n"
        "Modifiers / ترکیب‌ها:\n"
        " - `/ctrl <combo>` — مثال: `/ctrl c` -> Ctrl+C\n"
        " - `/alt <combo>`\n"
        " - `/shift <combo>`\n"
        " - `/keys <combo>` — مثال: `/keys ctrl+alt+c`\n\n"
        "Security / امنیت:\n"
        " - فقط روی سرورهایی استفاده کن که مجوزش رو داری.\n"
        " - پیشنهاد: `ALLOWED_USERS` رو توی env تنظیم کن تا فقط خودت بتونی استفاده کنی.\n"
    )
    update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

def status_cmd(update: Update, ctx: CallbackContext):
    if not guard(update):
        return
    key = session_key_from_update(update)
    s = get_session(key)
    if not s:
        update.message.reply_text("ℹ️ سشن فعالی نداری. از /start یا دکمه اتصال استفاده کن.")
        return
    uptime = int(now_ts() - s.connected_at)
    idle = int(now_ts() - s.last_activity)
    update.message.reply_text(
        f"📊 وضعیت سشن:\n"
        f"Target: {s.target}\n"
        f"Uptime: {uptime}s\n"
        f"Idle: {idle}s\n"
        f"Keyboard page: {s.kb_page}",
    )

def servers_cmd(update: Update, ctx: CallbackContext):
    if not guard(update):
        return
    user_id = update.effective_user.id
    update.message.reply_text("📚 سرورهای ذخیره شده:", reply_markup=keyboard_servers_list(user_id))

def addserver_cmd(update: Update, ctx: CallbackContext):
    if not guard(update):
        return
    user_id = update.effective_user.id
    if len(ctx.args) < 2:
        update.message.reply_text("Usage: /addserver <name> user@host[:port]")
        return
    name = ctx.args[0].strip()
    err = validate_server_name(name)
    if err:
        update.message.reply_text(f"❌ {err}")
        return
    target_str = " ".join(ctx.args[1:]).strip()
    target = parse_target(target_str)
    if not target:
        update.message.reply_text("❌ Target format is invalid. Example: root@1.2.3.4:22")
        return
    user, host, port = target

    servers = get_user_servers(user_id)
    servers[name] = {"user": user, "host": host, "port": port, "created_at": int(now_ts()), "last_used": int(now_ts())}
    set_user_servers(user_id, servers)

    update.message.reply_text("✅ ذخیره شد.", reply_markup=keyboard_servers_list(user_id))

def delserver_cmd(update: Update, ctx: CallbackContext):
    if not guard(update):
        return
    user_id = update.effective_user.id
    if not ctx.args:
        update.message.reply_text("Usage: /delserver <name>")
        return
    name = " ".join(ctx.args).strip()
    servers = get_user_servers(user_id)
    if name not in servers:
        update.message.reply_text("❌ چنین سروری پیدا نشد.")
        return
    servers.pop(name, None)
    set_user_servers(user_id, servers)
    # if was default, clear default
    if get_user_default_server(user_id) == name:
        set_user_default_server(user_id, "")
    update.message.reply_text("🗑 حذف شد.", reply_markup=keyboard_servers_list(user_id))

def ssh_cmd(update: Update, ctx: CallbackContext):
    if not guard(update):
        return
    key = session_key_from_update(update)
    chat_id, user_id = key

    # /ssh بدون آرگومان => قطع
    if not ctx.args:
        stopped = stop_session(key)
        if stopped:
            update.message.reply_text("Stopped existing SSH session. / سشن قطع شد.")
        else:
            update.message.reply_text("No active SSH session to stop. / سشن فعالی وجود ندارد.")
        return

    # اگر سشن هست، برای همین کاربر قطع کن (پشتیبانی گروه هم بهتر میشه)
    stop_session(key)

    target = parse_target(ctx.args[0])
    if not target:
        update.message.reply_text("Usage: /ssh user@host[:port]  /  نحوه استفاده: /ssh user@host[:port]")
        return

    user, host, port = target
    set_pending(key, PendingConn(user=user, host=host, port=port))
    # keep old behavior: ask to send /pass
    update.message.reply_text(
        "Send password using /pass <password> (message will be deleted). / برای ارسال رمز از /pass استفاده کنید (پیام حذف خواهد شد)."
    )

def pass_cmd(update: Update, ctx: CallbackContext):
    if not guard(update):
        return
    key = session_key_from_update(update)
    chat_id, user_id = key

    p = get_pending(key)
    if not p:
        update.message.reply_text("No pending SSH request. Use /ssh first. / ابتدا از /ssh استفاده کنید.")
        return

    if not ctx.args:
        update.message.reply_text("Usage: /pass <password> / نحوه استفاده: /pass <رمز>")
        return

    pwd = " ".join(ctx.args)

    # delete the message that contained the password if possible
    try:
        update.message.delete()
    except Exception:
        try:
            ctx.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
        except Exception:
            logger.debug("Couldn't delete password message")

    # stop any previous session for this user
    stop_session(key)

    sess = SSHSession(key, ctx.bot)
    with STATE_LOCK:
        SESSIONS[key] = sess

    ok, err = sess.start(p.user, p.host, p.port, pwd)
    with STATE_LOCK:
        PENDING.pop(key, None)

    if not ok:
        with STATE_LOCK:
            SESSIONS.pop(key, None)
        update.message.reply_text(f"Connection failed: {err} / اتصال ناموفق: {err}")
    else:
        update.message.reply_text("Connected. Terminal is shown above. / متصل شد.")

def stop_cmd(update: Update, ctx: CallbackContext):
    if not guard(update):
        return
    key = session_key_from_update(update)
    s_existed = stop_session(key)
    if s_existed:
        update.message.reply_text("Stopped SSH session. / سشن قطع شد.")
    else:
        update.message.reply_text("No active SSH session found. / سشن فعالی یافت نشد.")

def text_msg(update: Update, ctx: CallbackContext):
    if not guard(update):
        return
    # First: wizard flow
    if wizard_process_text(update, ctx):
        return

    key = session_key_from_update(update)
    s = get_session(key)
    if not s:
        return

    # remove user's message for privacy
    try:
        ctx.bot.delete_message(update.effective_chat.id, update.message.message_id)
    except Exception:
        pass

    text = update.message.text or ""
    s.send(text + "\n")

def cb(update: Update, ctx: CallbackContext):
    q = update.callback_query
    if not q:
        return

    chat_id = q.message.chat_id
    user_id = q.from_user.id
    if PRIVATE_ONLY:
        try:
            if q.message.chat.type != "private":
                q.answer("⛔ فقط در چت خصوصی.", show_alert=True)
                return
        except Exception:
            pass
    if ALLOWED_USERS and user_id not in ALLOWED_USERS:
        q.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    if ALLOWED_CHATS and chat_id not in ALLOWED_CHATS:
        q.answer("⛔ دسترسی نداری.", show_alert=True)
        return

    key = session_key_from_query(update)
    data = q.data or ""

    # WIZARD actions
    if data == "W:CANCEL":
        clear_wizard(key)
        try:
            q.answer("لغو شد.")
        except Exception:
            pass
        try:
            ctx.bot.send_message(chat_id, "❌ عملیات لغو شد.", reply_markup=keyboard_main(user_id))
        except Exception:
            pass
        return

    # MENU actions
    if data == "M:MENU":
        try:
            q.edit_message_text(
                "منو اصلی:",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard_main(user_id),
                disable_web_page_preview=True,
            )
        except Exception:
            pass
        try:
            q.answer()
        except Exception:
            pass
        return

    if data == "M:HELP":
        try:
            q.answer()
        except Exception:
            pass
        ctx.bot.send_message(chat_id, "برای راهنما /help رو بزن ✅")
        return

    if data == "M:STATUS":
        s = get_session(key)
        if not s:
            q.answer("سشن فعال نیست.")
            return
        uptime = int(now_ts() - s.connected_at)
        idle = int(now_ts() - s.last_activity)
        q.answer("OK")
        ctx.bot.send_message(
            chat_id,
            f"📊 وضعیت:\nTarget: {s.target}\nUptime: {uptime}s\nIdle: {idle}s",
        )
        return

    if data == "M:STOP":
        stopped = stop_session(key)
        clear_wizard(key)
        try:
            q.answer("قطع شد." if stopped else "سشن فعالی نیست.")
        except Exception:
            pass
        return

    if data == "M:CONNECT":
        try:
            q.answer()
        except Exception:
            pass
        wizard_ask_target(update, ctx)
        return

    if data == "M:ADD_SERVER":
        try:
            q.answer()
        except Exception:
            pass
        wizard_start_add_server(update, ctx)
        return

    if data == "M:SERVERS":
        try:
            q.edit_message_text(
                "📚 سرورهای شما:",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard_servers_list(user_id),
                disable_web_page_preview=True,
            )
        except Exception:
            ctx.bot.send_message(chat_id, "📚 سرورهای شما:", reply_markup=keyboard_servers_list(user_id))
        try:
            q.answer()
        except Exception:
            pass
        return

    # Server profiles actions
    if data.startswith("SV:OPEN:"):
        name = urlunquote(data.split("SV:OPEN:", 1)[1])
        servers = get_user_servers(user_id)
        if name not in servers:
            q.answer("پیدا نشد.", show_alert=True)
            return
        info = servers[name]
        user = info.get("user", "")
        host = info.get("host", "")
        port = info.get("port", 22)
        default = get_user_default_server(user_id)
        star = "⭐ " if name == default else ""
        text = (
            f"{star}<b>{html.escape(name)}</b>\n"
            f"<code>{html.escape(str(user))}@{html.escape(str(host))}:{int(port)}</code>"
        )
        try:
            q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard_server_actions(name))
        except Exception:
            ctx.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=keyboard_server_actions(name))
        try:
            q.answer()
        except Exception:
            pass
        return

    if data.startswith("SV:CONNECT:"):
        name = urlunquote(data.split("SV:CONNECT:", 1)[1])
        servers = get_user_servers(user_id)
        if name not in servers:
            q.answer("پیدا نشد.", show_alert=True)
            return
        info = servers[name]
        user = str(info.get("user", ""))
        host = str(info.get("host", ""))
        port = int(info.get("port", 22))

        # mark last used
        info["last_used"] = int(now_ts())
        servers[name] = info
        set_user_servers(user_id, servers)

        set_pending(key, PendingConn(user=user, host=host, port=port, server_name=name))
        try:
            q.answer("پسورد رو بفرست…")
        except Exception:
            pass
        wizard_ask_password(ctx, key)
        return

    if data.startswith("SV:DELETE:"):
        name = urlunquote(data.split("SV:DELETE:", 1)[1])
        servers = get_user_servers(user_id)
        if name not in servers:
            q.answer("پیدا نشد.", show_alert=True)
            return
        servers.pop(name, None)
        set_user_servers(user_id, servers)
        if get_user_default_server(user_id) == name:
            set_user_default_server(user_id, "")
        try:
            q.answer("حذف شد.")
        except Exception:
            pass
        try:
            q.edit_message_text("🗑 حذف شد. لیست سرورها:", parse_mode=ParseMode.HTML, reply_markup=keyboard_servers_list(user_id))
        except Exception:
            pass
        return

    if data.startswith("SV:DEFAULT:"):
        name = urlunquote(data.split("SV:DEFAULT:", 1)[1])
        servers = get_user_servers(user_id)
        if name not in servers:
            q.answer("پیدا نشد.", show_alert=True)
            return
        set_user_default_server(user_id, name)
        try:
            q.answer("پیش‌فرض شد ⭐")
        except Exception:
            pass
        try:
            q.edit_message_text("📚 سرورهای شما:", parse_mode=ParseMode.HTML, reply_markup=keyboard_servers_list(user_id))
        except Exception:
            pass
        return

    # Terminal actions
    s = get_session(key)

    if data.startswith("KB:PAGE:"):
        if not s:
            q.answer("سشن فعال نیست.")
            return
        try:
            page = int(data.split(":", 2)[2])
        except Exception:
            page = 0
        s.kb_page = page
        try:
            ctx.bot.edit_message_reply_markup(chat_id=chat_id, message_id=q.message.message_id, reply_markup=s.keyboard())
        except Exception:
            pass
        try:
            q.answer()
        except Exception:
            pass
        return

    if data.startswith("A:STOP"):
        stopped = stop_session(key)
        try:
            q.answer("قطع شد." if stopped else "سشن فعال نیست.")
        except Exception:
            pass
        return

    if data.startswith("A:SERVERS"):
        try:
            q.answer()
        except Exception:
            pass
        ctx.bot.send_message(chat_id, "📚 سرورهای شما:", reply_markup=keyboard_servers_list(user_id))
        return

    if data.startswith("K:"):
        if not s:
            try:
                q.answer("No active session. / سشن فعال نیست.")
            except Exception:
                pass
            try:
                ctx.bot.edit_message_reply_markup(chat_id=chat_id, message_id=q.message.message_id, reply_markup=None)
            except Exception:
                pass
            return

        keyname = data[2:]
        val = KEYS.get(keyname)
        if val is not None:
            s.send(val)
        try:
            q.answer()
        except Exception:
            pass
        return

    if data.startswith("MC:"):
        if not s:
            q.answer("سشن فعال نیست.")
            return
        mname = data.split("MC:", 1)[1]
        seq = MACROS.get(mname, "")
        if seq:
            s.send(seq)
        try:
            q.answer()
        except Exception:
            pass
        return

    if data.startswith("QC:"):
        if not s:
            q.answer("سشن فعال نیست.")
            return
        cname = data.split("QC:", 1)[1]
        cmd = QUICK_CMDS.get(cname, "")
        if cmd:
            s.send(cmd)
        try:
            q.answer()
        except Exception:
            pass
        return

    try:
        q.answer()
    except Exception:
        pass


# ---------- modifier commands kept ----------
def process_modifier_command(primary_mod: str, update: Update, ctx: CallbackContext):
    key = session_key_from_update(update)
    chat_id, user_id = key
    s = get_session(key)

    # try to delete the command message for privacy
    try:
        update.message.delete()
    except Exception:
        pass

    if not s:
        update.message.reply_text("No active session. / سشن فعالی وجود ندارد.")
        return

    tokens = ctx.args or []
    merged_tokens = [primary_mod.lower()] + tokens
    mods, keytok = parse_combo_tokens(merged_tokens)
    seq = build_sequence_from_mods_and_key(mods, keytok)
    if not seq:
        update.message.reply_text("Could not parse key. Usage: /ctrl c   or /ctrl alt c   or /keys ctrl+alt+c\n/ نتوانست کلید را پردازش کند.")
        return

    s.send(seq)
    try:
        if s.message_id:
            ctx.bot.edit_message_reply_markup(chat_id=chat_id, message_id=s.message_id, reply_markup=s.keyboard())
    except Exception:
        pass

def ctrl_cmd(update: Update, ctx: CallbackContext):
    if not guard(update):
        return
    process_modifier_command("CTRL", update, ctx)

def alt_cmd(update: Update, ctx: CallbackContext):
    if not guard(update):
        return
    process_modifier_command("ALT", update, ctx)

def shift_cmd(update: Update, ctx: CallbackContext):
    if not guard(update):
        return
    process_modifier_command("SHIFT", update, ctx)

def keys_cmd(update: Update, ctx: CallbackContext):
    if not guard(update):
        return
    key = session_key_from_update(update)
    chat_id, user_id = key
    s = get_session(key)

    # delete command message for privacy
    try:
        update.message.delete()
    except Exception:
        pass

    if not s:
        update.message.reply_text("No active session. / سشن فعالی وجود ندارد.")
        return

    tokens = ctx.args or []
    if not tokens:
        update.message.reply_text("Usage: /keys ctrl+alt+c or /keys ctrl alt c\n/ نحوه استفاده: /keys ctrl+alt+c")
        return

    mods, keytok = parse_combo_tokens(tokens)
    seq = build_sequence_from_mods_and_key(mods, keytok)
    if not seq:
        update.message.reply_text("Could not parse key. / نتوانست کلید را پردازش کند.")
        return

    s.send(seq)
    try:
        if s.message_id:
            ctx.bot.edit_message_reply_markup(chat_id=chat_id, message_id=s.message_id, reply_markup=s.keyboard())
    except Exception:
        pass


# ================= MAIN =================
def main():
    if not BOT_TOKEN:
        print("BOT_TOKEN missing")
        return

    up = Updater(BOT_TOKEN, use_context=True)
    dp = up.dispatcher

    dp.add_handler(CommandHandler("start", start_cmd))
    dp.add_handler(CommandHandler("menu", menu_cmd))
    dp.add_handler(CommandHandler("help", help_cmd))
    dp.add_handler(CommandHandler("status", status_cmd))

    dp.add_handler(CommandHandler("servers", servers_cmd))
    dp.add_handler(CommandHandler("addserver", addserver_cmd))
    dp.add_handler(CommandHandler("delserver", delserver_cmd))

    # legacy commands
    dp.add_handler(CommandHandler("ssh", ssh_cmd))
    dp.add_handler(CommandHandler("pass", pass_cmd))
    dp.add_handler(CommandHandler("stop", stop_cmd))

    # modifier commands
    dp.add_handler(CommandHandler("ctrl", ctrl_cmd))
    dp.add_handler(CommandHandler("alt", alt_cmd))
    dp.add_handler(CommandHandler("shift", shift_cmd))
    dp.add_handler(CommandHandler("keys", keys_cmd))

    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, text_msg))
    dp.add_handler(CallbackQueryHandler(cb))

    logger.info("SSH bot started")
    up.start_polling()
    up.idle()

if __name__ == "__main__":
    main()
