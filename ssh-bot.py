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
import secrets
import tempfile
import posixpath
from dataclasses import dataclass, field
from typing import Dict, Tuple, List, Optional, Any

import paramiko
import pyte

try:
    from cryptography.fernet import Fernet, InvalidToken
except Exception:  # cryptography is installed with paramiko, but keep a safe fallback
    Fernet = None
    class InvalidToken(Exception):
        pass

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ParseMode,
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

SESSION_TIMEOUT = int(os.environ.get("SESSION_TIMEOUT", "0"))  # seconds; 0=disabled
KEEPALIVE_SEC = int(os.environ.get("KEEPALIVE_SEC", "30"))
PRIVATE_ONLY = os.environ.get("PRIVATE_ONLY", "0").strip().lower() in ("1", "true", "yes", "on")
STRICT_HOST_KEY = os.environ.get("STRICT_HOST_KEY", "0").strip().lower() in ("1", "true", "yes", "on")

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

INSTALL_DIR = os.environ.get("INSTALL_DIR", "/opt/sshbot")
DATA_DIR = os.environ.get("DATA_DIR", f"{INSTALL_DIR}/data")
SERVER_DB = os.environ.get("SERVER_DB", f"{DATA_DIR}/servers.json")

# Internal encrypted keychain. The encryption key is stored locally with chmod 600.
KEYCHAIN_ENABLED = os.environ.get("KEYCHAIN_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
KEYCHAIN_KEY_FILE = os.environ.get("KEYCHAIN_KEY_FILE", f"{DATA_DIR}/keychain.key")

# Upload behavior. Telegram sends multiple selected files as multiple messages;
# the bot uploads each one to the currently selected remote directory.
UPLOAD_TMP_DIR = os.environ.get("UPLOAD_TMP_DIR", f"{DATA_DIR}/uploads")
UPLOAD_CREATE_DIR = os.environ.get("UPLOAD_CREATE_DIR", "1").strip().lower() in ("1", "true", "yes", "on")
UPLOAD_OVERWRITE = os.environ.get("UPLOAD_OVERWRITE", "1").strip().lower() in ("1", "true", "yes", "on")
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", "0"))  # 0 = Telegram/account limit only
DELETE_UPLOADED_TG_MESSAGE = os.environ.get("DELETE_UPLOADED_TG_MESSAGE", "0").strip().lower() in ("1", "true", "yes", "on")

LOG_DIR = os.environ.get("LOG_DIR", "/var/log/ssh-bot")
LOG_FILE = os.environ.get("LOG_FILE", f"{LOG_DIR}/ssh-bot.log")


# ================= LOGGING =================
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(UPLOAD_TMP_DIR, exist_ok=True)

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
    server_id: str = ""
    created_at: float = field(default_factory=time.time)

@dataclass
class WizardState:
    step: str
    data: Dict[str, Any] = field(default_factory=dict)
    prompt_msg_id: int = 0
    created_at: float = field(default_factory=time.time)

@dataclass
class UploadState:
    remote_dir: str = "."
    count: int = 0
    created_at: float = field(default_factory=time.time)

STATE_LOCK = threading.Lock()
DATA_LOCK = threading.Lock()

SESSIONS: Dict[SessionKey, "SSHSession"] = {}
PENDING: Dict[SessionKey, PendingConn] = {}
WIZARD: Dict[SessionKey, WizardState] = {}
UPLOADS: Dict[SessionKey, UploadState] = {}

# ================= UTIL =================
def now_ts() -> float:
    return time.time()

def html_pre(text: str) -> str:
    return f"<pre>{html.escape(text)}</pre>"

def clamp_tg(text: str) -> str:
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

    if ALLOWED_USERS and user_id not in ALLOWED_USERS:
        return False
    if ALLOWED_CHATS and chat_id not in ALLOWED_CHATS:
        return False
    return True

def guard(update: Update) -> bool:
    if not is_authorized(update):
        try:
            update.effective_message.reply_text("⛔ دسترسی نداری یا فقط باید در چت خصوصی استفاده بشه.")
        except Exception:
            pass
        return False
    return True

def session_key_from_update(update: Update) -> SessionKey:
    return (update.effective_chat.id, update.effective_user.id)

def session_key_from_query(update: Update) -> SessionKey:
    q = update.callback_query
    return (q.message.chat_id, q.from_user.id)

def parse_target(text: str) -> Optional[Tuple[str, str, int]]:
    text = (text or "").strip()
    m = SSH_RE.match(text)
    if not m:
        return None
    user, host, port = m.group(1), m.group(2), int(m.group(3) or 22)
    return user, host, port

def gen_server_id() -> str:
    # short stable id for callback_data (<=64 bytes)
    return secrets.token_hex(4)  # 8 chars

def validate_server_name(name: str) -> Optional[str]:
    name = (name or "").strip()
    if not name:
        return "اسم سرور خالیه."
    if len(name) > 32:
        return "اسم سرور طولانیه (حداکثر ۳۲ کاراکتر)."
    if any(c in name for c in ["\n", "\r", "\t"]):
        return "اسم سرور کاراکتر نامعتبر دارد."
    return None


def parse_host_port(text: str) -> Optional[Tuple[str, int]]:
    """Parse host[:port] for quick Keychain connections."""
    text = (text or "").strip()
    if not text:
        return None
    # Allow users to paste user@host too; username will be ignored in quick mode.
    if "@" in text:
        text = text.split("@", 1)[1]
    if ":" in text and not text.startswith("["):
        host, port_s = text.rsplit(":", 1)
        try:
            port = int(port_s)
        except Exception:
            return None
    else:
        host, port = text, 22
    host = host.strip().strip("[]")
    if not host or port < 1 or port > 65535:
        return None
    return host, port

def sanitize_filename(name: str) -> str:
    name = os.path.basename((name or "file").replace("\x00", "")).strip()
    name = name.replace("/", "_").replace("\\", "_")
    return name or "file"

# ================= SERVER DB (ID-BASED + MIGRATION) =================
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

def _ensure_user_record(db: Dict[str, Any], user_id: int) -> Dict[str, Any]:
    users = db.setdefault("users", {})
    rec = users.get(str(user_id))
    if not isinstance(rec, dict):
        rec = {}
    if "servers" not in rec or not isinstance(rec["servers"], dict):
        rec["servers"] = {}
    if "default" not in rec or not isinstance(rec["default"], str):
        rec["default"] = ""
    users[str(user_id)] = rec
    return rec

def _migrate_if_needed(db: Dict[str, Any], user_id: int) -> None:
    """
    Old format (name-based):
      servers = { "myserver": {"user":..., "host":..., "port":...}, ... }
      default = "myserver"
    New format (id-based):
      servers = { "a1b2c3d4": {"name":"myserver", "user":..., "host":..., "port":...}, ... }
      default = "a1b2c3d4"
    """
    rec = _ensure_user_record(db, user_id)
    servers = rec.get("servers", {})
    default = rec.get("default", "")

    if not servers:
        return

    # detect old format: values missing "name"
    old_format = False
    for k, v in servers.items():
        if isinstance(v, dict) and "name" not in v:
            # likely old
            old_format = True
            break

    if not old_format:
        return

    new_servers: Dict[str, Any] = {}
    name_to_id: Dict[str, str] = {}

    for name, info in list(servers.items()):
        if not isinstance(info, dict):
            continue
        sid = gen_server_id()
        while sid in new_servers:
            sid = gen_server_id()
        info2 = dict(info)
        info2["name"] = name
        new_servers[sid] = info2
        name_to_id[name] = sid

    # migrate default
    new_default = name_to_id.get(default, "")
    rec["servers"] = new_servers
    rec["default"] = new_default

def get_user_servers(user_id: int) -> Dict[str, Any]:
    db = load_server_db()
    _migrate_if_needed(db, user_id)
    rec = _ensure_user_record(db, user_id)
    # save if migrated
    save_server_db(db)
    servers = rec.get("servers", {})
    return servers if isinstance(servers, dict) else {}

def set_user_servers(user_id: int, servers: Dict[str, Any], default_id: Optional[str] = None) -> None:
    db = load_server_db()
    rec = _ensure_user_record(db, user_id)
    rec["servers"] = servers
    if default_id is not None:
        rec["default"] = default_id
    save_server_db(db)

def get_user_default_server_id(user_id: int) -> str:
    db = load_server_db()
    _migrate_if_needed(db, user_id)
    rec = _ensure_user_record(db, user_id)
    save_server_db(db)
    d = rec.get("default", "")
    return d if isinstance(d, str) else ""

def set_user_default_server_id(user_id: int, server_id: str) -> None:
    db = load_server_db()
    _migrate_if_needed(db, user_id)
    rec = _ensure_user_record(db, user_id)
    rec["default"] = server_id
    save_server_db(db)

def find_server_by_name(user_id: int, name: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    name = (name or "").strip()
    servers = get_user_servers(user_id)
    # exact match
    for sid, info in servers.items():
        if isinstance(info, dict) and info.get("name") == name:
            return sid, info
    # case-insensitive fallback for ascii names
    low = name.lower()
    for sid, info in servers.items():
        if isinstance(info, dict) and str(info.get("name", "")).lower() == low:
            return sid, info
    return None

# ================= KEYCHAIN HELPERS =================
_FERNET_CACHE = None

def _get_fernet():
    global _FERNET_CACHE
    if not KEYCHAIN_ENABLED:
        raise RuntimeError("Keychain is disabled")
    if Fernet is None:
        raise RuntimeError("cryptography/fernet is not available")
    if _FERNET_CACHE is not None:
        return _FERNET_CACHE
    os.makedirs(os.path.dirname(KEYCHAIN_KEY_FILE), exist_ok=True)
    if os.path.exists(KEYCHAIN_KEY_FILE):
        with open(KEYCHAIN_KEY_FILE, "rb") as f:
            key = f.read().strip()
    else:
        key = Fernet.generate_key()
        with open(KEYCHAIN_KEY_FILE, "wb") as f:
            f.write(key)
        try:
            os.chmod(KEYCHAIN_KEY_FILE, 0o600)
        except Exception:
            pass
    _FERNET_CACHE = Fernet(key)
    return _FERNET_CACHE

def encrypt_secret(secret: str) -> str:
    if not secret:
        return ""
    token = _get_fernet().encrypt(secret.encode("utf-8"))
    return "fernet:" + token.decode("ascii")

def decrypt_secret(value: str) -> str:
    value = value or ""
    if not value:
        return ""
    if value.startswith("fernet:"):
        token = value.split("fernet:", 1)[1].encode("ascii")
        return _get_fernet().decrypt(token).decode("utf-8")
    # Backward/fallback compatibility: never create plaintext secrets, but allow reading if an admin migrated manually.
    return value

def keychain_available() -> bool:
    return KEYCHAIN_ENABLED and Fernet is not None

def set_user_keychain_default(user_id: int, username: str, password: str) -> None:
    db = load_server_db()
    _migrate_if_needed(db, user_id)
    rec = _ensure_user_record(db, user_id)
    rec["keychain_default"] = {
        "user": username,
        "secret": encrypt_secret(password),
        "updated_at": int(now_ts()),
    }
    save_server_db(db)

def get_user_keychain_default(user_id: int) -> Optional[Tuple[str, str]]:
    db = load_server_db()
    _migrate_if_needed(db, user_id)
    rec = _ensure_user_record(db, user_id)
    kc = rec.get("keychain_default")
    save_server_db(db)
    if not isinstance(kc, dict):
        return None
    username = str(kc.get("user", "")).strip()
    secret = str(kc.get("secret", ""))
    if not username or not secret:
        return None
    try:
        return username, decrypt_secret(secret)
    except Exception:
        logger.exception("Could not decrypt default keychain")
        return None

def set_server_keychain_password(user_id: int, server_id: str, password: str) -> bool:
    db = load_server_db()
    _migrate_if_needed(db, user_id)
    rec = _ensure_user_record(db, user_id)
    servers = rec.get("servers", {})
    info = servers.get(server_id)
    if not isinstance(info, dict):
        return False
    info["auth"] = "keychain"
    info["secret"] = encrypt_secret(password)
    info["keychain_updated_at"] = int(now_ts())
    servers[server_id] = info
    rec["servers"] = servers
    save_server_db(db)
    return True

def clear_server_keychain_password(user_id: int, server_id: str) -> bool:
    db = load_server_db()
    _migrate_if_needed(db, user_id)
    rec = _ensure_user_record(db, user_id)
    servers = rec.get("servers", {})
    info = servers.get(server_id)
    if not isinstance(info, dict):
        return False
    info["auth"] = "ask"
    info.pop("secret", None)
    servers[server_id] = info
    rec["servers"] = servers
    save_server_db(db)
    return True

def get_server_keychain_password(info: Dict[str, Any]) -> Optional[str]:
    if not isinstance(info, dict):
        return None
    secret = str(info.get("secret", ""))
    if not secret:
        return None
    try:
        return decrypt_secret(secret)
    except Exception:
        logger.exception("Could not decrypt server keychain")
        return None

def server_has_keychain(info: Dict[str, Any]) -> bool:
    return bool(isinstance(info, dict) and info.get("secret"))

# ================= UI (INLINE BUTTONS) =================
def keyboard_main(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔌 اتصال", callback_data="M:CONNECT"),
             InlineKeyboardButton("🔑 اتصال Keychain", callback_data="M:QUICK_CONNECT")],
            [InlineKeyboardButton("📚 سرورها", callback_data="M:SERVERS"),
             InlineKeyboardButton("📤 آپلود فایل", callback_data="M:UPLOAD")],
            [InlineKeyboardButton("📊 وضعیت", callback_data="M:STATUS"),
             InlineKeyboardButton("🛑 قطع", callback_data="M:STOP")],
            [InlineKeyboardButton("➕ افزودن سرور", callback_data="M:ADD_SERVER"),
             InlineKeyboardButton("🔐 تنظیم Keychain", callback_data="M:KEYCHAIN_SETUP")],
            [InlineKeyboardButton("🆔 آی‌دی من", callback_data="M:MYID"),
             InlineKeyboardButton("❓ راهنما", callback_data="M:HELP")],
        ]
    )

def keyboard_servers_list(user_id: int) -> InlineKeyboardMarkup:
    servers = get_user_servers(user_id)
    default_id = get_user_default_server_id(user_id)

    rows: List[List[InlineKeyboardButton]] = []
    for sid, info in sorted(servers.items(), key=lambda kv: str(kv[1].get("name", "")).lower()):
        name = str(info.get("name", sid))
        star = "⭐ " if sid == default_id else ""
        rows.append([InlineKeyboardButton(f"{star}🖥 {name}", callback_data=f"SV:OPEN:{sid}")])

    rows.append([InlineKeyboardButton("➕ اضافه", callback_data="M:ADD_SERVER"),
                 InlineKeyboardButton("⬅️ منو", callback_data="M:MENU")])
    return InlineKeyboardMarkup(rows)

def keyboard_server_actions(server_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔌 اتصال", callback_data=f"SV:CONNECT:{server_id}"),
             InlineKeyboardButton("⭐ پیش‌فرض", callback_data=f"SV:DEFAULT:{server_id}")],
            [InlineKeyboardButton("🔑 ذخیره/تغییر Keychain", callback_data=f"SV:KEYCHAIN:{server_id}"),
             InlineKeyboardButton("🧹 حذف Keychain", callback_data=f"SV:KEYCHAIN_CLEAR:{server_id}")],
            [InlineKeyboardButton("🗑 حذف", callback_data=f"SV:DELETE:{server_id}"),
             InlineKeyboardButton("⬅️ لیست", callback_data="M:SERVERS")],
        ]
    )

def keyboard_wizard_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="W:CANCEL")]])

def keyboard_add_server_auth() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔑 ذخیره پسورد در Keychain", callback_data="W:ADD_AUTH:KEYCHAIN")],
            [InlineKeyboardButton("🚪 بدون Keychain / هر بار پسورد بپرس", callback_data="W:ADD_AUTH:ASK")],
            [InlineKeyboardButton("❌ لغو", callback_data="W:CANCEL")],
        ]
    )

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
        self.target = ""
        self.kb_page = 0
        self.sftp_lock = threading.Lock()

    def start(self, user: str, host: str, port: int, password: str) -> Tuple[bool, Optional[str]]:
        try:
            self.target = f"{user}@{host}:{port}"
            self.client = paramiko.SSHClient()

            if STRICT_HOST_KEY:
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

    def loop(self):
        last_update = 0.0
        while not self.stop.is_set():
            try:
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

        try:
            self.close()
        except Exception:
            pass

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
            logger.debug("Edit failed: %s", e)

    def send(self, text: str):
        try:
            if self.chan and not self.stop.is_set():
                self.chan.send(text)
                self.last_activity = now_ts()
        except Exception:
            logger.exception("Send failed")

    def keyboard(self) -> InlineKeyboardMarkup:
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
                    InlineKeyboardButton("📤 آپلود", callback_data="A:UPLOAD"),
                    InlineKeyboardButton("📚 سرورها", callback_data="A:SERVERS"),
                    InlineKeyboardButton("🛑 قطع", callback_data="A:STOP"),
                ],
            ]
            return InlineKeyboardMarkup(rows)

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
                InlineKeyboardButton("📤 آپلود", callback_data="A:UPLOAD"),
                InlineKeyboardButton("📚 سرورها", callback_data="A:SERVERS"),
                InlineKeyboardButton("🛑 قطع", callback_data="A:STOP"),
            ],
        ]
        return InlineKeyboardMarkup(rows)


    def _normalize_upload_dir(self, sftp, remote_dir: str) -> str:
        remote_dir = (remote_dir or ".").strip()
        if remote_dir in ("", ".", "./"):
            return sftp.normalize(".")
        if remote_dir == "~":
            return sftp.normalize(".")
        if remote_dir.startswith("~/"):
            return posixpath.join(sftp.normalize("."), remote_dir[2:])
        return remote_dir

    def _mkdir_p(self, sftp, remote_dir: str) -> None:
        remote_dir = self._normalize_upload_dir(sftp, remote_dir)
        if remote_dir in ("", "."):
            return
        if remote_dir.startswith("/"):
            cur = "/"
            parts = [p for p in remote_dir.split("/") if p]
        else:
            cur = sftp.normalize(".")
            parts = [p for p in remote_dir.split("/") if p and p != "."]
        for part in parts:
            cur = posixpath.join(cur, part)
            try:
                sftp.stat(cur)
            except IOError:
                sftp.mkdir(cur)

    def _unique_remote_path(self, sftp, remote_path: str) -> str:
        if UPLOAD_OVERWRITE:
            return remote_path
        base, ext = posixpath.splitext(remote_path)
        candidate = remote_path
        i = 1
        while True:
            try:
                sftp.stat(candidate)
                candidate = f"{base}_{i}{ext}"
                i += 1
            except IOError:
                return candidate

    def upload_file(self, local_path: str, filename: str, remote_dir: str = ".") -> str:
        if not self.client:
            raise RuntimeError("SSH client is not connected")
        filename = sanitize_filename(filename)
        with self.sftp_lock:
            sftp = self.client.open_sftp()
            try:
                target_dir = self._normalize_upload_dir(sftp, remote_dir)
                if UPLOAD_CREATE_DIR:
                    self._mkdir_p(sftp, target_dir)
                else:
                    sftp.chdir(target_dir)
                try:
                    target_dir = sftp.normalize(target_dir)
                except Exception:
                    pass
                remote_path = posixpath.join(target_dir.rstrip("/"), filename)
                remote_path = self._unique_remote_path(sftp, remote_path)
                sftp.put(local_path, remote_path)
                return remote_path
            finally:
                try:
                    sftp.close()
                except Exception:
                    pass

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


def set_upload(key: SessionKey, remote_dir: str) -> None:
    with STATE_LOCK:
        UPLOADS[key] = UploadState(remote_dir=remote_dir or ".")

def get_upload(key: SessionKey) -> Optional[UploadState]:
    with STATE_LOCK:
        return UPLOADS.get(key)

def clear_upload(key: SessionKey) -> None:
    with STATE_LOCK:
        UPLOADS.pop(key, None)

def start_ssh_session_with_password(ctx: CallbackContext, key: SessionKey, user: str, host: str, port: int, password: str) -> Tuple[bool, Optional[str], Optional[SSHSession]]:
    stop_session(key)
    sess = SSHSession(key, ctx.bot)
    with STATE_LOCK:
        SESSIONS[key] = sess
    ok, err = sess.start(user, host, port, password)
    if not ok:
        with STATE_LOCK:
            SESSIONS.pop(key, None)
        return False, err, None
    return True, None, sess

# ================= MODIFIER HELPERS =================
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

    ch0 = key_token[0]
    seq = ""
    if "ALT" in mods:
        seq += "\x1b"

    if "CTRL" in mods:
        c = ch0.lower()
        if "a" <= c <= "z":
            seq += chr(ord(c) - 96)
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

# ================= WIZARD =================
def wizard_ask_target(update: Update, ctx: CallbackContext):
    key = session_key_from_update(update)
    chat_id, user_id = key

    prompt = ctx.bot.send_message(
        chat_id,
        "🔌 مقصد SSH رو بفرست:\n"
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
    st.data.update({"user": p.user, "host": p.host, "port": p.port, "server_id": p.server_id})
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

def wizard_start_keychain_setup(update: Update, ctx: CallbackContext):
    key = session_key_from_update(update)
    chat_id, user_id = key
    if not keychain_available():
        ctx.bot.send_message(chat_id, "❌ Keychain فعال نیست یا cryptography نصب نیست.")
        return
    prompt = ctx.bot.send_message(
        chat_id,
        "🔐 یوزرنیم پیش‌فرض SSH را بفرست. بعد از آن پسورد را می‌پرسم و رمزنگاری‌شده ذخیره می‌کنم.",
        reply_markup=keyboard_wizard_cancel(),
    )
    set_wizard(key, WizardState(step="KEYCHAIN_USER", prompt_msg_id=prompt.message_id))

def wizard_ask_quick_host(update: Update, ctx: CallbackContext):
    key = session_key_from_update(update)
    chat_id, user_id = key
    if not get_user_keychain_default(user_id):
        ctx.bot.send_message(chat_id, "اول Keychain پیش‌فرض را تنظیم کن.")
        wizard_start_keychain_setup(update, ctx)
        return
    prompt = ctx.bot.send_message(
        chat_id,
        "🔑 فقط IP/Host و در صورت نیاز پورت را بفرست:\n<code>1.2.3.4</code> یا <code>1.2.3.4:22</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard_wizard_cancel(),
    )
    set_wizard(key, WizardState(step="QUICK_HOSTPORT", prompt_msg_id=prompt.message_id))

def wizard_ask_upload_dir(update: Update, ctx: CallbackContext):
    key = session_key_from_update(update)
    chat_id, user_id = key
    if not get_session(key):
        ctx.bot.send_message(chat_id, "اول به سرور وصل شو، بعد آپلود فایل را بزن.")
        return
    prompt = ctx.bot.send_message(
        chat_id,
        "📤 مسیر مقصد روی سرور را بفرست. برای home/current SFTP بفرست: <code>.</code>\nمثال: <code>/root/uploads</code>\nبعدش چند فایل را با هم از تلگرام انتخاب و ارسال کن. با /done تمام می‌شود.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard_wizard_cancel(),
    )
    set_wizard(key, WizardState(step="UPLOAD_DIR", prompt_msg_id=prompt.message_id))

def wizard_process_text(update: Update, ctx: CallbackContext) -> bool:
    key = session_key_from_update(update)
    st = get_wizard(key)
    if not st:
        return False

    chat_id, user_id = key
    msg = update.message
    if not msg:
        return False

    text = (msg.text or "").strip()

    # In groups: require reply to prompt for safety
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
            ctx.bot.send_message(chat_id, "❌ فرمت اشتباهه. مثال: <code>root@1.2.3.4:22</code>", parse_mode=ParseMode.HTML)
            return True
        user, host, port = target
        _try_delete()
        set_pending(key, PendingConn(user=user, host=host, port=port))
        wizard_ask_password(ctx, key)
        return True

    if st.step == "AWAIT_PASSWORD":
        pwd = text
        _try_delete()

        p = get_pending(key)
        if not p:
            clear_wizard(key)
            ctx.bot.send_message(chat_id, "❌ درخواست اتصال پیدا نشد. دوباره تلاش کن.", parse_mode=ParseMode.HTML)
            return True

        stop_session(key)

        sess = SSHSession(key, ctx.bot)
        with STATE_LOCK:
            SESSIONS[key] = sess

        ok, err = sess.start(p.user, p.host, p.port, pwd)
        clear_wizard(key)

        if not ok:
            with STATE_LOCK:
                SESSIONS.pop(key, None)
            ctx.bot.send_message(chat_id, f"❌ اتصال ناموفق:\n<code>{html.escape(str(err))}</code>", parse_mode=ParseMode.HTML)
        else:
            ctx.bot.send_message(
                chat_id,
                f"✅ وصل شدی به <b>{html.escape(sess.target)}</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard_main(user_id),
            )
        return True


    if st.step == "KEYCHAIN_USER":
        username = text.strip()
        if not username:
            ctx.bot.send_message(chat_id, "❌ یوزرنیم خالیه.")
            return True
        _try_delete()
        st.step = "KEYCHAIN_PASSWORD"
        st.data["username"] = username
        prompt = ctx.bot.send_message(
            chat_id,
            f"🔐 پسورد SSH برای <b>{html.escape(username)}</b> را بفرست. پیام پسورد حذف می‌شود.",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard_wizard_cancel(),
        )
        st.prompt_msg_id = prompt.message_id
        set_wizard(key, st)
        return True

    if st.step == "KEYCHAIN_PASSWORD":
        password = text
        _try_delete()
        username = str(st.data.get("username", "")).strip()
        try:
            set_user_keychain_default(user_id, username, password)
            clear_wizard(key)
            ctx.bot.send_message(chat_id, "✅ Keychain پیش‌فرض ذخیره شد. از این به بعد با «اتصال Keychain» فقط IP و پورت می‌فرستی.", reply_markup=keyboard_main(user_id))
        except Exception as e:
            logger.exception("Failed to save default keychain")
            clear_wizard(key)
            ctx.bot.send_message(chat_id, f"❌ ذخیره Keychain ناموفق بود: <code>{html.escape(str(e))}</code>", parse_mode=ParseMode.HTML)
        return True

    if st.step == "QUICK_HOSTPORT":
        hp = parse_host_port(text)
        if not hp:
            ctx.bot.send_message(chat_id, "❌ فرمت اشتباهه. مثال: <code>1.2.3.4:22</code>", parse_mode=ParseMode.HTML)
            return True
        _try_delete()
        kc = get_user_keychain_default(user_id)
        if not kc:
            clear_wizard(key)
            ctx.bot.send_message(chat_id, "❌ Keychain پیش‌فرض پیدا نشد. دوباره تنظیمش کن.")
            return True
        user, password = kc
        host, port = hp
        ok, err, sess = start_ssh_session_with_password(ctx, key, user, host, port, password)
        clear_wizard(key)
        if not ok:
            ctx.bot.send_message(chat_id, f"❌ اتصال Keychain ناموفق:\n<code>{html.escape(str(err))}</code>", parse_mode=ParseMode.HTML)
        else:
            ctx.bot.send_message(chat_id, f"✅ با Keychain وصل شدی به <b>{html.escape(sess.target)}</b>", parse_mode=ParseMode.HTML, reply_markup=keyboard_main(user_id))
        return True

    if st.step == "SET_SERVER_PASSWORD":
        password = text
        _try_delete()
        sid = str(st.data.get("server_id", ""))
        try:
            ok = set_server_keychain_password(user_id, sid, password)
            clear_wizard(key)
            ctx.bot.send_message(chat_id, "✅ پسورد این سرور در Keychain ذخیره شد." if ok else "❌ سرور پیدا نشد.", reply_markup=keyboard_servers_list(user_id))
        except Exception as e:
            logger.exception("Failed to save server keychain")
            clear_wizard(key)
            ctx.bot.send_message(chat_id, f"❌ ذخیره ناموفق بود: <code>{html.escape(str(e))}</code>", parse_mode=ParseMode.HTML)
        return True

    if st.step == "ADD_SERVER_PASSWORD":
        password = text
        _try_delete()
        name = str(st.data.get("name", "")).strip()
        user = str(st.data.get("user", "")).strip()
        host = str(st.data.get("host", "")).strip()
        port = int(st.data.get("port", 22))
        sid = str(st.data.get("server_id", ""))
        servers = get_user_servers(user_id)
        if not sid:
            sid = gen_server_id()
            while sid in servers:
                sid = gen_server_id()
        try:
            servers[sid] = {
                "name": name,
                "user": user,
                "host": host,
                "port": port,
                "auth": "keychain",
                "secret": encrypt_secret(password),
                "created_at": int(now_ts()),
                "last_used": int(now_ts()),
            }
            set_user_servers(user_id, servers)
            clear_wizard(key)
            ctx.bot.send_message(chat_id, f"✅ سرور <b>{html.escape(name)}</b> با Keychain ذخیره شد.", parse_mode=ParseMode.HTML, reply_markup=keyboard_servers_list(user_id))
        except Exception as e:
            logger.exception("Failed to add server with keychain")
            clear_wizard(key)
            ctx.bot.send_message(chat_id, f"❌ ذخیره ناموفق بود: <code>{html.escape(str(e))}</code>", parse_mode=ParseMode.HTML)
        return True

    if st.step == "UPLOAD_DIR":
        remote_dir = text.strip() or "."
        _try_delete()
        set_upload(key, remote_dir)
        clear_wizard(key)
        ctx.bot.send_message(chat_id, f"✅ حالت آپلود فعال شد. مقصد: <code>{html.escape(remote_dir)}</code>\nحالا یک یا چند فایل را بفرست. برای پایان: /done", parse_mode=ParseMode.HTML)
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
        existing = find_server_by_name(user_id, name)
        if existing:
            sid, _ = existing
        else:
            sid = gen_server_id()
            while sid in servers:
                sid = gen_server_id()

        st.step = "ADD_SERVER_AUTH"
        st.data.update({"server_id": sid, "user": user, "host": host, "port": port})
        set_wizard(key, st)
        ctx.bot.send_message(
            chat_id,
            f"روش اتصال برای <b>{html.escape(name)}</b> را انتخاب کن:",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard_add_server_auth(),
        )
        return True

    clear_wizard(key)
    return False

# ================= COMMANDS =================
def start_cmd(update: Update, ctx: CallbackContext):
    if not guard(update):
        return
    text = (
        "SSHBot آماده است ✅\n\n"
        "از دکمه‌ها استفاده کن یا دستورها رو تایپ کن.\n"

    )
    update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard_main(update.effective_user.id),
                              disable_web_page_preview=True)

def menu_cmd(update: Update, ctx: CallbackContext):
    if not guard(update):
        return
    update.message.reply_text("منو:", reply_markup=keyboard_main(update.effective_user.id))

def id_cmd(update: Update, ctx: CallbackContext):
    if not guard(update):
        return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    update.message.reply_text(f"🆔 User ID: {user_id}\n🧾 Chat ID: {chat_id}")

def help_cmd(update: Update, ctx: CallbackContext):
    if not guard(update):
        return
    text = (
        "HELP / راهنما\n\n"
        "✅ حالت دکمه‌ای: /start یا /menu\n"
        "🆔 گرفتن آی‌دی: /id\n\n"
        "Legacy commands:\n"
        " /ssh user@host[:port]\n"
        " /pass <password>  (پیام حذف میشه)\n"
        " /stop\n\n"
        "Multi-server:\n"
        " /servers\n"
        " /addserver <name> user@host[:port]\n"
        " /delserver <name>\n\n"
        "Key combos:\n"
        " /ctrl c   /alt a   /keys ctrl+alt+c\n"
    )
    update.message.reply_text(text)

def servers_cmd(update: Update, ctx: CallbackContext):
    if not guard(update):
        return
    user_id = update.effective_user.id
    update.message.reply_text("📚 سرورهای شما:", reply_markup=keyboard_servers_list(user_id))

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
        update.message.reply_text("❌ Target invalid. Example: root@1.2.3.4:22")
        return
    user, host, port = target

    servers = get_user_servers(user_id)
    existing = find_server_by_name(user_id, name)
    if existing:
        sid, _ = existing
    else:
        sid = gen_server_id()
        while sid in servers:
            sid = gen_server_id()

    servers[sid] = {"name": name, "user": user, "host": host, "port": port, "created_at": int(now_ts()), "last_used": int(now_ts())}
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
    found = find_server_by_name(user_id, name)
    if not found:
        update.message.reply_text("❌ پیدا نشد.")
        return
    sid, _ = found

    servers = get_user_servers(user_id)
    servers.pop(sid, None)

    default_id = get_user_default_server_id(user_id)
    if default_id == sid:
        set_user_servers(user_id, servers, default_id="")
    else:
        set_user_servers(user_id, servers)

    update.message.reply_text("🗑 حذف شد.", reply_markup=keyboard_servers_list(user_id))


def serverpass_cmd(update: Update, ctx: CallbackContext):
    if not guard(update):
        return
    user_id = update.effective_user.id
    key = session_key_from_update(update)
    if not keychain_available():
        update.message.reply_text("❌ Keychain فعال نیست یا cryptography نصب نیست.")
        return
    if not ctx.args:
        update.message.reply_text("Usage: /serverpass <server name>")
        return
    name = " ".join(ctx.args).strip()
    found = find_server_by_name(user_id, name)
    if not found:
        update.message.reply_text("❌ سرور پیدا نشد.")
        return
    sid, info = found
    prompt = update.message.reply_text(
        f"🔑 پسورد SSH برای <b>{html.escape(str(info.get('name', name)))}</b> را بفرست. پیام پسورد حذف می‌شود.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard_wizard_cancel(),
    )
    set_wizard(key, WizardState(step="SET_SERVER_PASSWORD", data={"server_id": sid}, prompt_msg_id=prompt.message_id))

def keychain_cmd(update: Update, ctx: CallbackContext):
    if not guard(update):
        return
    wizard_start_keychain_setup(update, ctx)

def quickssh_cmd(update: Update, ctx: CallbackContext):
    if not guard(update):
        return
    key = session_key_from_update(update)
    chat_id, user_id = key
    if not ctx.args:
        wizard_ask_quick_host(update, ctx)
        return
    hp = parse_host_port(" ".join(ctx.args))
    if not hp:
        update.message.reply_text("Usage: /quickssh host[:port]")
        return
    kc = get_user_keychain_default(user_id)
    if not kc:
        update.message.reply_text("اول /keychain را اجرا کن تا یوزرنیم و پسورد پیش‌فرض ذخیره شود.")
        return
    user, password = kc
    host, port = hp
    ok, err, sess = start_ssh_session_with_password(ctx, key, user, host, port, password)
    if not ok:
        update.message.reply_text(f"❌ اتصال Keychain ناموفق:\n<code>{html.escape(str(err))}</code>", parse_mode=ParseMode.HTML)
    else:
        update.message.reply_text(f"✅ با Keychain وصل شدی به <b>{html.escape(sess.target)}</b>", parse_mode=ParseMode.HTML, reply_markup=keyboard_main(user_id))

def upload_cmd(update: Update, ctx: CallbackContext):
    if not guard(update):
        return
    key = session_key_from_update(update)
    if not get_session(key):
        update.message.reply_text("اول به سرور وصل شو، بعد /upload را بزن.")
        return
    remote_dir = " ".join(ctx.args).strip() if ctx.args else "."
    set_upload(key, remote_dir or ".")
    update.message.reply_text(f"✅ حالت آپلود فعال شد. مقصد: <code>{html.escape(remote_dir or '.')}</code>\nحالا یک یا چند فایل را بفرست. برای پایان: /done", parse_mode=ParseMode.HTML)

def done_cmd(update: Update, ctx: CallbackContext):
    if not guard(update):
        return
    key = session_key_from_update(update)
    st = get_upload(key)
    clear_upload(key)
    update.message.reply_text(f"✅ آپلود تمام شد. تعداد فایل‌های آپلودشده: {st.count if st else 0}")

def status_cmd(update: Update, ctx: CallbackContext):
    if not guard(update):
        return
    key = session_key_from_update(update)
    s = get_session(key)
    if not s:
        update.message.reply_text("ℹ️ سشن فعالی نداری.")
        return
    uptime = int(now_ts() - s.connected_at)
    idle = int(now_ts() - s.last_activity)
    update.message.reply_text(f"📊 وضعیت:\nTarget: {s.target}\nUptime: {uptime}s\nIdle: {idle}s")

def ssh_cmd(update: Update, ctx: CallbackContext):
    if not guard(update):
        return
    key = session_key_from_update(update)

    if not ctx.args:
        stopped = stop_session(key)
        update.message.reply_text("سشن قطع شد ✅" if stopped else "سشن فعالی نیست.")
        return

    stop_session(key)

    target = parse_target(ctx.args[0])
    if not target:
        update.message.reply_text("Usage: /ssh user@host[:port]")
        return
    user, host, port = target
    set_pending(key, PendingConn(user=user, host=host, port=port))
    update.message.reply_text("پسورد رو با /pass <password> بفرست (پیام حذف میشه).")

def pass_cmd(update: Update, ctx: CallbackContext):
    if not guard(update):
        return
    key = session_key_from_update(update)
    chat_id, user_id = key

    p = get_pending(key)
    if not p:
        update.message.reply_text("اول /ssh یا دکمه اتصال رو بزن.")
        return
    if not ctx.args:
        update.message.reply_text("Usage: /pass <password>")
        return

    pwd = " ".join(ctx.args)

    try:
        update.message.delete()
    except Exception:
        pass

    ok, err, sess = start_ssh_session_with_password(ctx, key, p.user, p.host, p.port, pwd)
    with STATE_LOCK:
        PENDING.pop(key, None)

    if not ok:
        update.message.reply_text(f"❌ اتصال ناموفق: {err}")
    else:
        update.message.reply_text("✅ وصل شدی.")

def stop_cmd(update: Update, ctx: CallbackContext):
    if not guard(update):
        return
    key = session_key_from_update(update)
    stopped = stop_session(key)
    update.message.reply_text("✅ قطع شد." if stopped else "سشن فعالی نیست.")


def document_msg(update: Update, ctx: CallbackContext):
    if not guard(update):
        return
    key = session_key_from_update(update)
    chat_id, user_id = key
    s = get_session(key)
    if not s:
        update.message.reply_text("اول به سرور وصل شو، بعد /upload را بزن و فایل بفرست.")
        return

    # Allow caption: /upload /remote/path on first file
    caption = (update.message.caption or "").strip()
    if caption.startswith("/upload"):
        parts = caption.split(maxsplit=1)
        set_upload(key, parts[1].strip() if len(parts) > 1 else ".")

    upst = get_upload(key)
    if not upst:
        update.message.reply_text("برای آپلود فایل اول /upload [مسیر] را بزن، بعد یک یا چند فایل بفرست.")
        return

    msg = update.message
    attachment = None
    filename = ""
    size = 0
    if msg.document:
        attachment = msg.document
        filename = msg.document.file_name or f"file_{msg.message_id}"
        size = msg.document.file_size or 0
    elif msg.photo:
        attachment = msg.photo[-1]
        filename = f"photo_{msg.message_id}.jpg"
        size = attachment.file_size or 0
    else:
        update.message.reply_text("این نوع فایل پشتیبانی نمی‌شود. فایل را به صورت Document بفرست.")
        return

    if MAX_UPLOAD_BYTES > 0 and size and size > MAX_UPLOAD_BYTES:
        update.message.reply_text(f"❌ فایل بزرگ‌تر از حد مجاز است ({MAX_UPLOAD_BYTES} bytes).")
        return

    safe_name = sanitize_filename(filename)
    local_path = os.path.join(UPLOAD_TMP_DIR, f"{chat_id}_{user_id}_{msg.message_id}_{safe_name}")
    status = update.message.reply_text(f"📥 دریافت فایل: <code>{html.escape(safe_name)}</code>", parse_mode=ParseMode.HTML)
    try:
        tg_file = attachment.get_file()
        tg_file.download(custom_path=local_path)
        remote_path = s.upload_file(local_path, safe_name, upst.remote_dir)
        with STATE_LOCK:
            cur = UPLOADS.get(key)
            if cur:
                cur.count += 1
                UPLOADS[key] = cur
        try:
            status.edit_text(f"✅ آپلود شد:\n<code>{html.escape(remote_path)}</code>", parse_mode=ParseMode.HTML)
        except Exception:
            update.message.reply_text(f"✅ آپلود شد:\n<code>{html.escape(remote_path)}</code>", parse_mode=ParseMode.HTML)
        if DELETE_UPLOADED_TG_MESSAGE:
            try:
                msg.delete()
            except Exception:
                pass
    except Exception as e:
        logger.exception("Upload failed")
        try:
            status.edit_text(f"❌ آپلود ناموفق:\n<code>{html.escape(str(e))}</code>", parse_mode=ParseMode.HTML)
        except Exception:
            update.message.reply_text(f"❌ آپلود ناموفق: {e}")
    finally:
        try:
            if os.path.exists(local_path):
                os.remove(local_path)
        except Exception:
            pass

def text_msg(update: Update, ctx: CallbackContext):
    if not guard(update):
        return

    if wizard_process_text(update, ctx):
        return

    key = session_key_from_update(update)
    s = get_session(key)
    if not s:
        return

    try:
        ctx.bot.delete_message(update.effective_chat.id, update.message.message_id)
    except Exception:
        pass

    s.send((update.message.text or "") + "\n")

def cb(update: Update, ctx: CallbackContext):
    q = update.callback_query
    if not q:
        return

    # auth checks
    if PRIVATE_ONLY:
        try:
            if q.message.chat.type != "private":
                q.answer("⛔ فقط خصوصی", show_alert=True)
                return
        except Exception:
            pass
    if ALLOWED_USERS and q.from_user.id not in ALLOWED_USERS:
        q.answer("⛔ دسترسی نداری", show_alert=True)
        return
    if ALLOWED_CHATS and q.message.chat_id not in ALLOWED_CHATS:
        q.answer("⛔ دسترسی نداری", show_alert=True)
        return

    key = session_key_from_query(update)
    chat_id, user_id = key
    data = q.data or ""

    # wizard cancel
    if data == "W:CANCEL":
        clear_wizard(key)
        q.answer("لغو شد")
        ctx.bot.send_message(chat_id, "❌ لغو شد.", reply_markup=keyboard_main(user_id))
        return

    if data == "W:ADD_AUTH:ASK":
        st = get_wizard(key)
        if not st or st.step != "ADD_SERVER_AUTH":
            q.answer("درخواست پیدا نشد", show_alert=True)
            return
        name = str(st.data.get("name", "")).strip()
        sid = str(st.data.get("server_id", ""))
        servers = get_user_servers(user_id)
        servers[sid] = {
            "name": name,
            "user": str(st.data.get("user", "")),
            "host": str(st.data.get("host", "")),
            "port": int(st.data.get("port", 22)),
            "auth": "ask",
            "created_at": int(now_ts()),
            "last_used": int(now_ts()),
        }
        set_user_servers(user_id, servers)
        clear_wizard(key)
        q.answer("ذخیره شد")
        ctx.bot.send_message(chat_id, f"✅ سرور <b>{html.escape(name)}</b> ذخیره شد. موقع اتصال پسورد می‌پرسم.", parse_mode=ParseMode.HTML, reply_markup=keyboard_servers_list(user_id))
        return

    if data == "W:ADD_AUTH:KEYCHAIN":
        st = get_wizard(key)
        if not st or st.step != "ADD_SERVER_AUTH":
            q.answer("درخواست پیدا نشد", show_alert=True)
            return
        if not keychain_available():
            q.answer("Keychain فعال نیست", show_alert=True)
            return
        st.step = "ADD_SERVER_PASSWORD"
        set_wizard(key, st)
        q.answer()
        prompt = ctx.bot.send_message(chat_id, "🔑 پسورد SSH این سرور را بفرست. پیام حذف و رمزنگاری می‌شود.", reply_markup=keyboard_wizard_cancel())
        st.prompt_msg_id = prompt.message_id
        set_wizard(key, st)
        return

    # menu
    if data == "M:MENU":
        q.edit_message_text("منو:", reply_markup=keyboard_main(user_id))
        q.answer()
        return

    if data == "M:HELP":
        q.answer()
        ctx.bot.send_message(chat_id, "راهنما: /help")
        return

    if data == "M:MYID":
        q.answer()
        ctx.bot.send_message(chat_id, f"🆔 User ID: {user_id}\n🧾 Chat ID: {chat_id}")
        return

    if data == "M:STATUS":
        s = get_session(key)
        if not s:
            q.answer("سشن فعال نیست")
            return
        uptime = int(now_ts() - s.connected_at)
        idle = int(now_ts() - s.last_activity)
        q.answer("OK")
        ctx.bot.send_message(chat_id, f"📊 وضعیت:\nTarget: {s.target}\nUptime: {uptime}s\nIdle: {idle}s")
        return

    if data == "M:STOP":
        stopped = stop_session(key)
        clear_wizard(key)
        q.answer("قطع شد" if stopped else "سشن فعالی نیست")
        return

    if data == "M:CONNECT":
        q.answer()
        wizard_ask_target(update, ctx)
        return

    if data == "M:QUICK_CONNECT":
        q.answer()
        wizard_ask_quick_host(update, ctx)
        return

    if data == "M:KEYCHAIN_SETUP":
        q.answer()
        wizard_start_keychain_setup(update, ctx)
        return

    if data == "M:UPLOAD":
        q.answer()
        wizard_ask_upload_dir(update, ctx)
        return

    if data == "M:ADD_SERVER":
        q.answer()
        wizard_start_add_server(update, ctx)
        return

    if data == "M:SERVERS":
        try:
            q.edit_message_text("📚 سرورهای شما:", reply_markup=keyboard_servers_list(user_id))
        except Exception:
            ctx.bot.send_message(chat_id, "📚 سرورهای شما:", reply_markup=keyboard_servers_list(user_id))
        q.answer()
        return

    # server actions
    if data.startswith("SV:OPEN:"):
        sid = data.split("SV:OPEN:", 1)[1]
        servers = get_user_servers(user_id)
        info = servers.get(sid)
        if not isinstance(info, dict):
            q.answer("پیدا نشد", show_alert=True)
            return
        name = str(info.get("name", sid))
        user = str(info.get("user", ""))
        host = str(info.get("host", ""))
        port = int(info.get("port", 22))
        default_id = get_user_default_server_id(user_id)
        star = "⭐ " if sid == default_id else ""
        auth_txt = "🔑 Keychain: فعال" if server_has_keychain(info) else "🔑 Keychain: خاموش"
        text = f"{star}<b>{html.escape(name)}</b>\n<code>{html.escape(user)}@{html.escape(host)}:{port}</code>\n{auth_txt}"
        q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard_server_actions(sid))
        q.answer()
        return

    if data.startswith("SV:CONNECT:"):
        sid = data.split("SV:CONNECT:", 1)[1]
        servers = get_user_servers(user_id)
        info = servers.get(sid)
        if not isinstance(info, dict):
            q.answer("پیدا نشد", show_alert=True)
            return
        info["last_used"] = int(now_ts())
        servers[sid] = info
        set_user_servers(user_id, servers)

        user = str(info.get("user", ""))
        host = str(info.get("host", ""))
        port = int(info.get("port", 22))
        saved_pwd = get_server_keychain_password(info) if server_has_keychain(info) else None
        if saved_pwd:
            q.answer("اتصال با Keychain…")
            ok, err, sess = start_ssh_session_with_password(ctx, key, user, host, port, saved_pwd)
            if not ok:
                ctx.bot.send_message(chat_id, f"❌ اتصال Keychain ناموفق:\n<code>{html.escape(str(err))}</code>", parse_mode=ParseMode.HTML)
            else:
                ctx.bot.send_message(chat_id, f"✅ وصل شدی به <b>{html.escape(sess.target)}</b>", parse_mode=ParseMode.HTML, reply_markup=keyboard_main(user_id))
            return

        set_pending(key, PendingConn(
            user=user,
            host=host,
            port=port,
            server_id=sid
        ))
        q.answer("پسورد رو بفرست…")
        wizard_ask_password(ctx, key)
        return

    if data.startswith("SV:KEYCHAIN_CLEAR:"):
        sid = data.split("SV:KEYCHAIN_CLEAR:", 1)[1]
        ok = clear_server_keychain_password(user_id, sid)
        q.answer("Keychain حذف شد" if ok else "پیدا نشد", show_alert=not ok)
        try:
            q.edit_message_text("📚 سرورهای شما:", reply_markup=keyboard_servers_list(user_id))
        except Exception:
            pass
        return

    if data.startswith("SV:KEYCHAIN:"):
        sid = data.split("SV:KEYCHAIN:", 1)[1]
        servers = get_user_servers(user_id)
        info = servers.get(sid)
        if not isinstance(info, dict):
            q.answer("پیدا نشد", show_alert=True)
            return
        if not keychain_available():
            q.answer("Keychain فعال نیست", show_alert=True)
            return
        q.answer()
        prompt = ctx.bot.send_message(chat_id, f"🔑 پسورد SSH برای <b>{html.escape(str(info.get('name', sid)))}</b> را بفرست. پیام حذف می‌شود.", parse_mode=ParseMode.HTML, reply_markup=keyboard_wizard_cancel())
        set_wizard(key, WizardState(step="SET_SERVER_PASSWORD", data={"server_id": sid}, prompt_msg_id=prompt.message_id))
        return

    if data.startswith("SV:DELETE:"):
        sid = data.split("SV:DELETE:", 1)[1]
        servers = get_user_servers(user_id)
        if sid not in servers:
            q.answer("پیدا نشد", show_alert=True)
            return
        servers.pop(sid, None)
        default_id = get_user_default_server_id(user_id)
        if default_id == sid:
            set_user_servers(user_id, servers, default_id="")
        else:
            set_user_servers(user_id, servers)
        q.answer("حذف شد")
        try:
            q.edit_message_text("📚 سرورهای شما:", reply_markup=keyboard_servers_list(user_id))
        except Exception:
            pass
        return

    if data.startswith("SV:DEFAULT:"):
        sid = data.split("SV:DEFAULT:", 1)[1]
        servers = get_user_servers(user_id)
        if sid not in servers:
            q.answer("پیدا نشد", show_alert=True)
            return
        set_user_default_server_id(user_id, sid)
        q.answer("پیش‌فرض شد ⭐")
        try:
            q.edit_message_text("📚 سرورهای شما:", reply_markup=keyboard_servers_list(user_id))
        except Exception:
            pass
        return

    # terminal UI
    s = get_session(key)

    if data.startswith("KB:PAGE:"):
        if not s:
            q.answer("سشن فعال نیست")
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
        q.answer()
        return

    if data == "A:STOP":
        stopped = stop_session(key)
        q.answer("قطع شد" if stopped else "سشن فعال نیست")
        return

    if data == "A:SERVERS":
        q.answer()
        ctx.bot.send_message(chat_id, "📚 سرورها:", reply_markup=keyboard_servers_list(user_id))
        return

    if data == "A:UPLOAD":
        q.answer()
        wizard_ask_upload_dir(update, ctx)
        return

    if data.startswith("K:"):
        if not s:
            q.answer("سشن فعال نیست")
            return
        keyname = data[2:]
        val = KEYS.get(keyname)
        if val is not None:
            s.send(val)
        q.answer()
        return

    if data.startswith("MC:"):
        if not s:
            q.answer("سشن فعال نیست")
            return
        mname = data.split("MC:", 1)[1]
        seq = MACROS.get(mname, "")
        if seq:
            s.send(seq)
        q.answer()
        return

    if data.startswith("QC:"):
        if not s:
            q.answer("سشن فعال نیست")
            return
        cname = data.split("QC:", 1)[1]
        cmd = QUICK_CMDS.get(cname, "")
        if cmd:
            s.send(cmd)
        q.answer()
        return

    q.answer()

# modifier commands (kept)
def process_modifier_command(primary_mod: str, update: Update, ctx: CallbackContext):
    key = session_key_from_update(update)
    chat_id, user_id = key
    s = get_session(key)

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
        update.message.reply_text("Could not parse key. Usage: /ctrl c  or /keys ctrl+alt+c")
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

    try:
        update.message.delete()
    except Exception:
        pass

    if not s:
        update.message.reply_text("No active session. / سشن فعالی وجود ندارد.")
        return

    tokens = ctx.args or []
    if not tokens:
        update.message.reply_text("Usage: /keys ctrl+alt+c")
        return

    mods, keytok = parse_combo_tokens(tokens)
    seq = build_sequence_from_mods_and_key(mods, keytok)
    if not seq:
        update.message.reply_text("Could not parse key.")
        return

    s.send(seq)

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
    dp.add_handler(CommandHandler("id", id_cmd))
    dp.add_handler(CommandHandler("status", status_cmd))

    dp.add_handler(CommandHandler("servers", servers_cmd))
    dp.add_handler(CommandHandler("addserver", addserver_cmd))
    dp.add_handler(CommandHandler("delserver", delserver_cmd))
    dp.add_handler(CommandHandler("serverpass", serverpass_cmd))

    dp.add_handler(CommandHandler("keychain", keychain_cmd))
    dp.add_handler(CommandHandler("quickssh", quickssh_cmd))
    dp.add_handler(CommandHandler("upload", upload_cmd))
    dp.add_handler(CommandHandler("done", done_cmd))

    dp.add_handler(CommandHandler("ssh", ssh_cmd))
    dp.add_handler(CommandHandler("pass", pass_cmd))
    dp.add_handler(CommandHandler("stop", stop_cmd))

    dp.add_handler(CommandHandler("ctrl", ctrl_cmd))
    dp.add_handler(CommandHandler("alt", alt_cmd))
    dp.add_handler(CommandHandler("shift", shift_cmd))
    dp.add_handler(CommandHandler("keys", keys_cmd))

    dp.add_handler(MessageHandler((Filters.document | Filters.photo) & ~Filters.command, document_msg))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, text_msg))
    dp.add_handler(CallbackQueryHandler(cb))

    logger.info("SSH bot started")
    up.start_polling()
    up.idle()

if __name__ == "__main__":
    main()
