import asyncio
import hashlib
import hmac
import importlib
import json
import os
import threading
import time
from urllib.parse import urlencode

import asyncssh
import pytest
from fastapi.testclient import TestClient
from web.security import telegram_user
from web.migrate import migrate

TOKEN = '123:testtoken'
ORIGIN = 'https://ssh.example.com'


def init_data(uid=1234, age=0, extra=None):
    fields = {'auth_date': str(int(time.time()) - age), 'user': json.dumps({'id': uid, 'first_name': 'Test'})}
    fields.update(extra or {})
    check = '\n'.join(f'{k}={v}' for k, v in sorted(fields.items()))
    key = hmac.new(b'WebAppData', TOKEN.encode(), hashlib.sha256).digest()
    fields['hash'] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = tmp_path / 'config.json'
    cfg.write_text(json.dumps({'owner_id': 1234, 'bot_token': TOKEN, 'public_url': ORIGIN, 'data_dir': str(tmp_path/'data'), 'polling': False}))
    monkeypatch.setenv('SSHBOT_CONFIG', str(cfg))
    import web.app as mod
    mod = importlib.reload(mod)
    with TestClient(mod.app, base_url=ORIGIN, headers={'origin': ORIGIN}) as client:
        assert client.post('/api/login', json={'init_data': init_data()}).status_code == 200
        yield mod, client


def test_signature():
    assert telegram_user(init_data(), TOKEN) == 1234
    for data in [init_data(age=400), init_data(age=-100), init_data()+'&auth_date=1', init_data().replace('Test','Changed')]:
        with pytest.raises(ValueError): telegram_user(data, TOKEN)


def test_access_isolation_and_revocation(env):
    mod, owner = env
    with TestClient(mod.app, base_url=ORIGIN, headers={'origin': ORIGIN}) as guest:
        assert guest.get('/api/hosts').status_code == 401
        assert guest.post('/api/login', json={'init_data': init_data(987)}).status_code == 403
        assert owner.post('/api/users/987').status_code == 200
        assert guest.post('/api/login', json={'init_data': init_data(987)}).status_code == 200
        assert guest.get('/api/users').status_code == 403
        h = owner.post('/api/hosts', json={'name':'owner only','host':'127.0.0.1'}).json()
        assert guest.get('/api/hosts').json() == []
        assert guest.put('/api/hosts/'+h['id'], json={'name':'hijack','host':'127.0.0.1'}).status_code == 404
        assert guest.post('/api/hosts/'+h['id']+'/ticket').status_code == 404
        assert owner.delete('/api/users/987').status_code == 200
        assert guest.get('/api/me').status_code == 401
        assert owner.delete('/api/users/1234').status_code == 400


def test_csrf_and_vault(env):
    mod, c = env
    assert c.post('/api/hosts', headers={'origin':'https://evil.test'}, json={'name':'x','host':'x'}).status_code == 403
    cred = c.post('/api/credentials', json={'name':'password','kind':'password','secret':'superSecret123'}).json()
    assert 'encrypted' not in cred and 'secret' not in cred
    assert 'superSecret123' not in json.dumps(c.get('/api/credentials').json())
    assert b'superSecret123' not in mod.store.path.read_bytes()
    h=c.post('/api/hosts', json={'name':'test','host':'127.0.0.1','credential_id':cred['id']}).json()
    assert c.delete('/api/credentials/'+cred['id']).status_code == 409
    assert c.post('/api/hosts/'+h['id']+'/ticket').status_code == 400
    assert c.post('/api/credentials', json={'name':'invalid','kind':'key','secret':'not a key'}).status_code == 400
    key=asyncssh.generate_private_key('ssh-ed25519').export_private_key().decode()
    assert c.post('/api/credentials', json={'name':'key','kind':'key','secret':key}).status_code==200
    c.post('/api/logout')
    assert c.get('/api/me').status_code==401


@pytest.fixture
def ssh_server(tmp_path):
    loop=asyncio.new_event_loop()
    ready=threading.Event()
    state={}
    connections=set()
    key=asyncssh.generate_private_key('ssh-ed25519')
    class Server(asyncssh.SSHServer):
        def connection_made(self, conn):
            self.conn=conn;connections.add(conn)
        def connection_lost(self, exc):
            connections.discard(self.conn)
        def begin_auth(self, username): return True
        def password_auth_supported(self): return True
        def validate_password(self, username, password): return username=='root' and password=='testpass'
        def public_key_auth_supported(self): return True
        def validate_public_key(self, username, public_key): return username=='root' and public_key==key.convert_to_public()
    async def process(proc):
        proc.stdout.write(b'READY\r\n')
        while data:=await proc.stdin.read(4096):
            proc.stdout.write(b'ECHO:'+data)
    async def start():
        state['server']=await asyncssh.create_server(Server,'127.0.0.1',0,server_host_keys=[key],process_factory=process,encoding=None,sftp_factory=lambda chan: asyncssh.SFTPServer(chan,chroot=str(tmp_path)))
        state['port']=state['server'].get_port()
        ready.set()
    def worker():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(start())
        loop.run_forever()
    thread=threading.Thread(target=worker,daemon=True);thread.start();ready.wait(10)
    state.update(key=key, directory=tmp_path)
    yield state
    async def stop():
        for conn in list(connections):conn.abort()
        state['server'].close();await state['server'].wait_closed()
        pending=[task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        for task in pending:task.cancel()
        if pending:await asyncio.gather(*pending,return_exceptions=True)
    asyncio.run_coroutine_threadsafe(stop(),loop).result(5)
    loop.call_soon_threadsafe(loop.stop);thread.join(5);loop.close()


def make_host(c, server, kind='password'):
    secret='testpass' if kind=='password' else server['key'].export_private_key().decode()
    cred=c.post('/api/credentials',json={'name':'credential','kind':kind,'secret':secret}).json()
    h=c.post('/api/hosts',json={'name':'local','host':'127.0.0.1','port':server['port'],'credential_id':cred['id']}).json()
    r=c.post('/api/hosts/'+h['id']+'/fingerprint')
    assert r.status_code==200,r.text
    fp=r.json()['fingerprint']
    assert fp==server['key'].get_fingerprint()
    assert c.post('/api/hosts/'+h['id']+'/trust',json={'fingerprint':fp}).status_code==200
    return h


@pytest.mark.parametrize('kind',['password','key'])
def test_real_ssh_websocket_and_sftp(env,ssh_server,kind):
    mod,c=env
    h=make_host(c,ssh_server,kind)
    ticket=c.post('/api/hosts/'+h['id']+'/ticket').json()['ticket']
    with c.websocket_connect('/ws',headers={'origin':ORIGIN}) as ws:
        ws.send_json({'ticket':ticket})
        ready=ws.receive_json()
        assert ready.get('ready'),ready
        assert b'READY' in ws.receive_bytes()
        ws.send_json({'type':'input','data':'hello\n'})
        assert b'hello' in ws.receive_bytes()
        # Upload using the active connection, and reject overwrite.
        sid=ready['session_id']
        r=c.post('/api/upload/'+sid,files={'file':('hello.txt',b'contents')},data={'directory':'.'})
        assert r.status_code==200,r.text
        assert (ssh_server['directory']/'hello.txt').read_bytes()==b'contents'
        assert c.post('/api/upload/'+sid,files={'file':('hello.txt',b'overwrite')},data={'directory':'.'}).status_code==400
    with c.websocket_connect('/ws',headers={'origin':ORIGIN}) as ws:
        ws.send_json({'ticket':ticket})
        assert ws.receive()['type']=='websocket.close'


def test_changed_fingerprint_rejected(env,ssh_server):
    mod,c=env
    h=make_host(c,ssh_server)
    wrong=asyncssh.generate_private_key('ssh-ed25519').get_fingerprint()
    c.post('/api/hosts/'+h['id']+'/trust',json={'fingerprint':wrong})
    ticket=c.post('/api/hosts/'+h['id']+'/ticket').json()['ticket']
    with c.websocket_connect('/ws',headers={'origin':ORIGIN}) as ws:
        ws.send_json({'ticket':ticket})
        result=ws.receive_json()
        assert 'هویت سرور' in result['error']


def test_migration(tmp_path):
    from cryptography.fernet import Fernet
    from web.store import Store
    key=Fernet.generate_key();f=Fernet(key)
    (tmp_path/'key').write_bytes(key)
    secret='fernet:'+f.encrypt(b'legacy-password').decode()
    source=tmp_path/'servers.json'
    source.write_text(json.dumps({'users':{'1234':{'servers':{'old':{'host':'10.0.0.1','user':'root','secret':secret}},'default':'old'}}}))
    before=source.read_bytes()
    migrate(source,tmp_path/'key',tmp_path/'new')
    store=Store(tmp_path/'new')
    assert len(store.items(1234,'host'))==1
    assert store.decrypt(store.items(1234,'credential')[0]['encrypted'])['secret']=='legacy-password'
    migrate(source,tmp_path/'key',tmp_path/'new')
    assert len(store.items(1234,'host'))==1
    assert source.read_bytes()==before
    assert not store.allowed(1234,999)


def test_ticket_logout_and_websocket_origin(env,ssh_server):
    mod,c=env
    h=make_host(c,ssh_server)
    ticket=c.post('/api/hosts/'+h['id']+'/ticket').json()['ticket']
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect('/ws',headers={'origin':'https://evil.test'}):
            pass
    c.post('/api/logout')
    with c.websocket_connect('/ws',headers={'origin':ORIGIN}) as ws:
        ws.send_json({'ticket':ticket})
        assert ws.receive()['type']=='websocket.close'


def test_live_session_expires_on_logout(env,ssh_server):
    mod,c=env
    h=make_host(c,ssh_server)
    ticket=c.post('/api/hosts/'+h['id']+'/ticket').json()['ticket']
    with c.websocket_connect('/ws',headers={'origin':ORIGIN}) as ws:
        ws.send_json({'ticket':ticket})
        assert ws.receive_json()['ready']
        ws.receive_bytes()
        c.post('/api/logout')
        assert ws.receive()['type']=='websocket.close'


def test_configurable_limits(env):
    mod,c=env
    path=os.environ['SSHBOT_CONFIG']
    cfg=json.loads(open(path).read());cfg.update(session_hours=2,max_sessions=2,upload_mb=5)
    with open(path,'w') as f:json.dump(cfg,f)
    mod=importlib.reload(mod)
    with TestClient(mod.app,base_url=ORIGIN,headers={'origin':ORIGIN}) as client:
        r=client.post('/api/login',json={'init_data':init_data()})
        assert 'Max-Age=7200' in r.headers['set-cookie']
        assert client.get('/api/me').json()['limits']=={'session_hours':2,'max_sessions':2,'upload_mb':5}
