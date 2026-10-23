import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

import asyncssh
import httpx
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, UploadFile, File, Form
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from .security import telegram_user, digest
from .store import Store

ROOT = Path(__file__).parent
CONFIG = json.loads(Path(os.environ.get('SSHBOT_CONFIG', '/etc/sshbot-web/config.json')).read_text())
OWNER = int(CONFIG['owner_id'])
TOKEN = CONFIG['bot_token']
ORIGIN = CONFIG['public_url'].rstrip('/')
if OWNER <= 0 or not ORIGIN.startswith('https://') or not TOKEN:
    raise RuntimeError('Owner, bot token and HTTPS URL are required')
store = Store(CONFIG.get('data_dir', '/var/lib/sshbot-web'))
tickets = {}
active = {}
log = logging.getLogger('sshbot')


def authorized(uid):
    return store.allowed(uid, OWNER)


async def telegram(method, payload):
    async with httpx.AsyncClient(timeout=40) as client:
        r = await client.post(f'https://api.telegram.org/bot{TOKEN}/{method}', json=payload)
        data = r.json()
        if not data.get('ok'):
            raise RuntimeError('Telegram API rejected request: ' + method)
        return data['result']


async def bot_loop():
    offset = 0
    while True:
        try:
            updates = await telegram('getUpdates', {'offset': offset, 'timeout': 25, 'allowed_updates': ['message']})
            for update in updates:
                offset = update['update_id'] + 1
                m = update.get('message', {})
                uid = m.get('from', {}).get('id', 0)
                if m.get('chat', {}).get('type') != 'private':
                    continue
                text = m.get('text', '')
                if text.startswith('/id'):
                    await telegram('sendMessage', {'chat_id': uid, 'text': f'Telegram ID: {uid}'})
                elif text.startswith(('/start', '/menu')):
                    payload = {'chat_id': uid, 'text': '⛔ دسترسی شما فعال نیست.\nآیدی شما: ' + str(uid)}
                    if authorized(uid):
                        payload.update(text='SSHBot • فضای امن سرورهای شما\nبرای مدیریت سرورها و باز کردن ترمینال وارد شوید.', reply_markup={'inline_keyboard': [[{'text': '🖥 باز کردن SSHBot', 'web_app': {'url': ORIGIN}}]]})
                    await telegram('sendMessage', payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning('Bot polling failed; retrying (check token/network or another poller).')
            await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(bot_loop()) if CONFIG.get('polling', True) else None
    yield
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    for entry in list(active.values()):
        entry['conn'].close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware('http')
async def security_headers(request, call_next):
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and request.headers.get('origin') != ORIGIN:
        return Response('Origin rejected', status_code=403)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self' 'unsafe-inline'; connect-src 'self' wss:; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'self' https://web.telegram.org https://*.telegram.org"
    return response


def identity(request):
    token = request.cookies.get('sshbot_session', '')
    with store.db() as db:
        row = db.execute('SELECT * FROM sessions WHERE token=? AND expires>?', (digest(token), time.time())).fetchone()
    if not row or not authorized(row['uid']):
        raise HTTPException(401, 'ورود منقضی شده یا دسترسی ندارید؛ برنامه را از ربات دوباره باز کنید.')
    return row['uid']


def admin(request):
    uid = identity(request)
    if uid != OWNER:
        raise HTTPException(403, 'فقط مالک اجازه دارد.')
    return uid


def item(uid, kind, item_id):
    result = store.get(uid, kind, item_id)
    if not result:
        raise HTTPException(404, 'مورد پیدا نشد.')
    return result


class Login(BaseModel):
    init_data: str = Field(max_length=16000)


@app.post('/api/login')
async def login(data: Login, response: Response):
    try:
        uid = telegram_user(data.init_data, TOKEN)
    except (ValueError, KeyError, TypeError):
        raise HTTPException(401, 'برنامه را از دکمه داخل ربات تلگرام باز کنید.')
    if not authorized(uid):
        raise HTTPException(403, 'آیدی شما اجازه ورود ندارد: ' + str(uid))
    token = secrets.token_urlsafe(32)
    with store.db() as db:
        db.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))
        db.execute('INSERT INTO sessions VALUES(?,?,?)', (digest(token), uid, int(time.time()) + 28800))
    response.set_cookie('sshbot_session', token, max_age=28800, secure=True, httponly=True, samesite='none')
    store.log(uid, 'login')
    return {'ok': True}


@app.post('/api/logout')
def logout(request: Request, response: Response):
    with store.db() as db:
        db.execute('DELETE FROM sessions WHERE token=?', (digest(request.cookies.get('sshbot_session', '')),))
    response.delete_cookie('sshbot_session', secure=True, httponly=True, samesite='none')
    return {'ok': True}


@app.get('/api/me')
def me(request: Request):
    uid = identity(request)
    return {'id': uid, 'owner': uid == OWNER}


class Host(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    host: str = Field(min_length=1, max_length=253, pattern=r'^[a-zA-Z0-9.:%_-]+$')
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(default='root', min_length=1, max_length=64)
    group: str = Field(default='', max_length=80)
    notes: str = Field(default='', max_length=4000)
    favorite: bool = False
    credential_id: str = ''


class Credential(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: str = Field(pattern='^(password|key)$')
    secret: str = Field(default='', max_length=32000)
    passphrase: str = Field(default='', max_length=1024)


def safe_credential(c):
    return {k: v for k, v in c.items() if k != 'encrypted'}


@app.get('/api/hosts')
def hosts(request: Request):
    return store.items(identity(request), 'host')


@app.get('/api/credentials')
def credentials(request: Request):
    return [safe_credential(c) for c in store.items(identity(request), 'credential')]


def save_host(uid, data, host_id=None):
    old = item(uid, 'host', host_id) if host_id else {}
    if data.credential_id:
        item(uid, 'credential', data.credential_id)
    body = data.model_dump()
    if old.get('host') == data.host and old.get('port') == data.port:
        body['fingerprint'] = old.get('fingerprint', '')
    body['last_used'] = old.get('last_used', 0)
    host_id = host_id or secrets.token_hex(16)
    store.put(uid, 'host', host_id, body)
    store.log(uid, 'save_host', host_id)
    return dict(body, id=host_id)


@app.post('/api/hosts')
def new_host(data: Host, request: Request):
    return save_host(identity(request), data)


@app.put('/api/hosts/{host_id}')
def update_host(host_id: str, data: Host, request: Request):
    return save_host(identity(request), data, host_id)


def save_credential(uid, data, cid=None):
    old = item(uid, 'credential', cid) if cid else {}
    if not data.secret:
        if not old or old['kind'] != data.kind:
            raise HTTPException(400, 'رمز یا کلید خصوصی را وارد کنید.')
        encrypted = old['encrypted']
    else:
        if data.kind == 'key':
            try:
                asyncssh.import_private_key(data.secret, data.passphrase or None)
            except (ValueError, asyncssh.KeyImportError):
                raise HTTPException(400, 'کلید خصوصی یا Passphrase معتبر نیست.')
        encrypted = store.encrypt({'secret': data.secret, 'passphrase': data.passphrase})
    cid = cid or secrets.token_hex(16)
    body = {'name': data.name, 'kind': data.kind, 'encrypted': encrypted}
    store.put(uid, 'credential', cid, body)
    store.log(uid, 'save_credential', cid)
    return safe_credential(dict(body, id=cid))


@app.post('/api/credentials')
def new_credential(data: Credential, request: Request):
    return save_credential(identity(request), data)


@app.put('/api/credentials/{cid}')
def update_credential(cid: str, data: Credential, request: Request):
    return save_credential(identity(request), data, cid)


@app.delete('/api/hosts/{item_id}')
@app.delete('/api/credentials/{item_id}')
def delete_item(item_id: str, request: Request):
    kind = request.url.path.split('/')[2]
    uid = identity(request)
    kind = {'hosts': 'host', 'credentials': 'credential'}.get(kind)
    if not kind:
        raise HTTPException(404)
    item(uid, kind, item_id)
    if kind == 'credential' and any(h.get('credential_id') == item_id for h in store.items(uid, 'host')):
        raise HTTPException(409, 'ابتدا این Keychain را از سرورهای مرتبط جدا کنید.')
    store.remove(uid, kind, item_id)
    store.log(uid, 'delete_' + kind, item_id)
    return {'ok': True}


@app.get('/api/users')
def users(request: Request):
    admin(request)
    with store.db() as db:
        return {'owner': OWNER, 'users': [r[0] for r in db.execute('SELECT uid FROM users ORDER BY uid')]}


@app.post('/api/users/{uid}')
def allow_user(uid: int, request: Request):
    admin(request)
    if not 0 < uid < 2**53:
        raise HTTPException(400, 'آیدی عددی معتبر نیست.')
    with store.db() as db:
        db.execute('INSERT OR IGNORE INTO users VALUES(?)', (uid,))
    store.log(OWNER, 'allow_user', str(uid))
    return {'ok': True}


@app.delete('/api/users/{uid}')
async def revoke_user(uid: int, request: Request):
    admin(request)
    if uid == OWNER:
        raise HTTPException(400, 'مالک قابل حذف نیست.')
    with store.db() as db:
        db.execute('DELETE FROM users WHERE uid=?', (uid,))
        db.execute('DELETE FROM sessions WHERE uid=?', (uid,))
    for entry in list(active.values()):
        if entry['uid'] == uid:
            entry['conn'].close()
    store.log(OWNER, 'revoke_user', str(uid))
    return {'ok': True}


@app.get('/api/audit')
def audit(request: Request):
    uid = identity(request)
    with store.db() as db:
        return [dict(r) for r in db.execute('SELECT ts, action, target FROM audit WHERE uid=? ORDER BY id DESC LIMIT 100', (uid,))]


@app.post('/api/hosts/{host_id}/fingerprint')
async def fingerprint(host_id: str, request: Request):
    uid = identity(request)
    h = item(uid, 'host', host_id)
    try:
        key = await asyncio.wait_for(asyncssh.get_server_host_key(h['host'], h['port']), 12)
        if not key:
            raise ValueError()
        return {'fingerprint': key.get_fingerprint(), 'saved': h.get('fingerprint', '')}
    except (OSError, ValueError, asyncssh.Error, asyncio.TimeoutError):
        raise HTTPException(502, 'خواندن هویت سرور ناموفق بود؛ IP، پورت و فایروال را بررسی کنید.')


class Trust(BaseModel):
    fingerprint: str = Field(pattern=r'^SHA256:[A-Za-z0-9+/]{43}$')


@app.post('/api/hosts/{host_id}/trust')
def trust(host_id: str, data: Trust, request: Request):
    uid = identity(request)
    h = item(uid, 'host', host_id)
    h['fingerprint'] = data.fingerprint
    store.put(uid, 'host', host_id, h)
    store.log(uid, 'trust_host_key', host_id)
    return {'ok': True}


@app.post('/api/hosts/{host_id}/ticket')
def ticket(host_id: str, request: Request):
    uid = identity(request)
    h = item(uid, 'host', host_id)
    if not h.get('fingerprint') or not h.get('credential_id'):
        raise HTTPException(400, 'Keychain و تأیید هویت سرور لازم است.')
    now = time.time()
    for key in list(tickets):
        if tickets[key]['expires'] < now:
            tickets.pop(key, None)
    if sum(t['uid'] == uid for t in tickets.values()) >= 10:
        raise HTTPException(429, 'کمی صبر کنید.')
    value = secrets.token_urlsafe(32)
    tickets[value] = {'uid': uid, 'host': host_id, 'expires': now + 30, 'session': digest(request.cookies.get('sshbot_session', ''))}
    return {'ticket': value}


class PinnedClient(asyncssh.SSHClient):
    def __init__(self, fingerprint):
        self.fingerprint = fingerprint

    def validate_host_public_key(self, host, addr, port, key):
        return secrets.compare_digest(key.get_fingerprint(), self.fingerprint)


def session_valid(t):
    if not authorized(t['uid']):
        return False
    with store.db() as db:
        return db.execute('SELECT 1 FROM sessions WHERE token=? AND uid=? AND expires>?', (t['session'], t['uid'], time.time())).fetchone() is not None


@app.websocket('/ws')
async def terminal(ws: WebSocket):
    if ws.headers.get('origin') != ORIGIN:
        await ws.close(code=4403)
        return
    await ws.accept()
    conn = None
    tasks = []
    sid = secrets.token_hex(16)
    uid = None
    try:
        opening = await asyncio.wait_for(ws.receive_json(), 10)
        t = tickets.pop(opening.get('ticket', ''), None)
        if not t or t['expires'] < time.time() or not session_valid(t):
            await ws.close(code=4401)
            return
        uid = t['uid']
        if sum(a['uid'] == uid for a in active.values()) >= 8:
            await ws.send_json({'error': 'حداکثر ۸ ترمینال هم‌زمان.'})
            return
        h = item(uid, 'host', t['host'])
        c = item(uid, 'credential', h['credential_id'])
        secret = store.decrypt(c['encrypted'])
        keys = [asyncssh.import_private_key(secret['secret'], secret['passphrase'] or None)] if c['kind'] == 'key' else []
        conn = await asyncio.wait_for(asyncssh.connect(h['host'], port=h['port'], username=h['username'], password=secret['secret'] if c['kind'] == 'password' else None, client_keys=keys, agent_path=None, config=None, known_hosts=b'', client_factory=lambda: PinnedClient(h['fingerprint']), login_timeout=15, keepalive_interval=30, keepalive_count_max=3), 20)
        proc = await conn.create_process(term_type='xterm-256color', term_size=(100, 30), encoding=None)
        active[sid] = {'uid': uid, 'conn': conn}
        h['last_used'] = int(time.time())
        store.put(uid, 'host', t['host'], h)
        store.log(uid, 'connect', t['host'])
        await ws.send_json({'ready': True, 'session_id': sid})

        async def output(reader):
            while True:
                chunk = await reader.read(32768)
                if not chunk:
                    break
                await ws.send_bytes(chunk)

        async def input_stream():
            while True:
                msg = await ws.receive_json()
                if msg.get('type') == 'input':
                    value = msg.get('data', '')
                    if not isinstance(value, str) or len(value) > 65536:
                        raise ValueError('input too large')
                    proc.stdin.write(value.encode())
                    await proc.stdin.drain()
                elif msg.get('type') == 'resize':
                    proc.change_terminal_size(max(20, min(400, int(msg['cols']))), max(5, min(160, int(msg['rows']))))

        async def monitor():
            while session_valid(t):
                await asyncio.sleep(1)
            await ws.close(code=4401)

        # A PTY combines stdout/stderr. A separate empty stderr reader must not end the session.
        tasks = [asyncio.create_task(output(proc.stdout)), asyncio.create_task(input_stream()), asyncio.create_task(monitor())]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except Exception as exc:
        if isinstance(exc, asyncssh.HostKeyNotVerifiable):
            message = 'هویت سرور تغییر کرده؛ اتصال متوقف شد. اثر انگشت را دوباره بررسی کنید.'
        elif isinstance(exc, asyncssh.PermissionDenied):
            message = 'ورود SSH رد شد؛ نام کاربری و Keychain را بررسی کنید.'
        else:
            message = 'اتصال بسته شد یا برقرار نشد؛ شبکه، IP، پورت و اطلاعات ورود را بررسی کنید.'
        with contextlib.suppress(Exception):
            await ws.send_json({'error': message})
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        active.pop(sid, None)
        if conn:
            conn.close()
            await conn.wait_closed()
        if uid:
            store.log(uid, 'disconnect', sid)
        with contextlib.suppress(Exception):
            await ws.close()


@app.post('/api/upload/{sid}')
async def upload(sid: str, request: Request, file: UploadFile = File(...), directory: str = Form('.')):
    uid = identity(request)
    entry = active.get(sid)
    if not entry or entry['uid'] != uid:
        raise HTTPException(404, 'ترمینال فعال نیست.')
    name = (file.filename or '').replace('\\', '/').split('/')[-1]
    if not name or name in ('.', '..') or '\x00' in name or '\x00' in directory:
        raise HTTPException(400, 'نام فایل معتبر نیست.')
    path = directory.rstrip('/') + '/' + name
    size = 0
    try:
        async with entry['conn'].start_sftp_client() as sftp:
            # Exclusive creation prevents accidental overwrite of existing remote files.
            async with sftp.open(path, 'xb') as remote:
                try:
                    while chunk := await file.read(65536):
                        if not authorized(uid):
                            raise HTTPException(403, 'دسترسی لغو شد.')
                        size += len(chunk)
                        if size > 50 * 1024 * 1024:
                            raise HTTPException(413, 'حداکثر حجم هر فایل ۵۰ مگابایت است.')
                        await remote.write(chunk)
                except BaseException:
                    await remote.close()
                    await sftp.remove(path)
                    raise
    except asyncssh.SFTPError:
        raise HTTPException(400, 'آپلود انجام نشد؛ مسیر و مجوز را بررسی کنید. فایل هم‌نام بازنویسی نمی‌شود.')
    finally:
        await file.close()
    store.log(uid, 'upload', sid)
    return {'ok': True, 'bytes': size}


@app.get('/health')
def health():
    return {'ok': True, 'version': '6.0.0'}


@app.get('/')
def index():
    return FileResponse(ROOT / 'static/index.html')


app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')
