import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

spec=importlib.util.spec_from_file_location('manager', Path(__file__).resolve().parents[1]/'deploy/manager.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


def config():
    return dict(m.DEFAULTS, owner_id=1234, bot_token='123:'+'a'*30, public_url='https://ssh.example.com',email='owner@example.com',web_server='apache2',data_dir=str(m.DATA))


def test_templates_loopback_separate_vhost_and_limits():
    c=config();c['listen_port']=8810
    a=m.render_site(c)
    assert 'ServerName ssh.example.com' in a
    assert 'ws://127.0.0.1:8810/ws' in a
    assert a.index('ProxyPass /ws')<a.index('ProxyPass / http')
    assert 'DocumentRoot /var/www/sshbot-acme' in a
    assert 'ProxyRequests Off' in a
    assert 'php' not in a.lower() and 'Listen ' not in a
    c['web_server']='nginx'
    n=m.render_site(c)
    assert 'proxy_pass http://127.0.0.1:8810' in n
    assert c['public_url'][8:] in n
    unit=m.render_unit(c,Path('/opt/sshbot-web/releases/test'))
    assert '--host 127.0.0.1 --port 8810' in unit
    assert 'CPUQuota=50%' in unit and 'MemoryMax=512M' in unit
    assert 'apache' not in unit and 'mysql' not in unit


@pytest.mark.parametrize('field,value',[('public_url','https://bad.com;rm'),('public_url','https://ok.test/path'),('owner_id',0),('listen_port',443),('cpu_percent',401),('max_sessions',9),('email','x\nInclude /tmp/config')])
def test_config_input_validation(field,value):
    c=config();c[field]=value
    with pytest.raises(m.ManagerError):m.validate(c)


def test_file_transaction_preserves_files_symlinks_modes(tmp_path):
    source=tmp_path/'site';source.write_text('old');source.chmod(0o640)
    link=tmp_path/'enabled';link.symlink_to(source)
    new=tmp_path/'new'
    tx=m.FileTransaction();tx.write(source,'new');tx.remove(link);tx.write(new,'temporary')
    tx.rollback()
    assert source.read_text()=='old' and source.stat().st_mode & 0o777==0o640
    assert link.is_symlink() and link.resolve()==source
    assert not new.exists()


def test_apache_detection_does_not_stop_other_services(monkeypatch):
    calls=[]
    def run(*args,**kw):
        calls.append(args)
        return SimpleNamespace(stdout='LISTEN 0 511 *:80 *:* users:(("apache2",pid=22,fd=4))\n',returncode=0)
    monkeypatch.setattr(m,'run',run);monkeypatch.setattr(m,'active',lambda service:True)
    assert m.detect_web()=='apache2'
    assert all(c[0]=='ss' for c in calls)


def test_unknown_listener_rejected(monkeypatch):
    monkeypatch.setattr(m,'run',lambda *a,**k:SimpleNamespace(stdout='LISTEN 0 511 *:80 *:* users:(("caddy",pid=2,fd=4))\n'))
    with pytest.raises(m.ManagerError):m.detect_web()


def test_webhook_guard_never_deletes_webhook(monkeypatch):
    calls=[]
    def telegram(token,method,payload=None):
        calls.append(method)
        return {'username':'sales'} if method=='getMe' else {'url':'https://sales.example.com/hook'}
    monkeypatch.setattr(m,'telegram',telegram)
    with pytest.raises(m.ManagerError):m.bot_preflight(config())
    assert calls==['getMe','getWebhookInfo']


def test_domain_collisions_and_wildcards(tmp_path,monkeypatch):
    monkeypatch.setattr(m,'ETC',tmp_path)
    monkeypatch.setattr(m,'old_v6_paths',lambda:[])
    p=tmp_path/'apache2/sites-available/sales.conf';p.parent.mkdir(parents=True)
    p.write_text('ServerName sales.example.com\nServerAlias *.private.example.com\n')
    m.domain_conflict('ssh.example.com','apache2')
    for domain in ['sales.example.com','ssh.private.example.com']:
        with pytest.raises(m.ManagerError):m.domain_conflict(domain,'apache2')


@pytest.fixture
def deployment(tmp_path,monkeypatch):
    for name,path in [('APP','opt/sshbot-web'),('DATA','data'),('CONFIG','etc/sshbot-web/config.json'),('ETC','etc'),('BACKUPS','backups'),('UNIT','etc/systemd/sshbot-web.service'),('COMMAND','bin/sshbot-manager'),('ACME','www/acme'),('CERT_ROOT','certs'),('HOOK','hooks/deploy.sh'),('RENEW_UNIT','etc/systemd/sshbot-cert.service'),('RENEW_TIMER','etc/systemd/sshbot-cert.timer')]:
        monkeypatch.setattr(m,name,tmp_path/path)
    m.CONFIG.parent.mkdir(parents=True);m.UNIT.parent.mkdir(parents=True)
    release=m.APP/'releases/old';(release/'venv/bin').mkdir(parents=True);(release/'venv/bin/python').touch()
    (m.APP/'current').symlink_to(release)
    c=config();m.CONFIG.write_text(json.dumps(c));m.UNIT.write_text('ExecStart=/opt/sshbot-web/old\n')
    available,enabled=m.site_paths('apache2');available.parent.mkdir(parents=True);enabled.parent.mkdir(parents=True)
    available.write_text(m.MARKER+'\nold site');enabled.symlink_to(available)
    sales=available.parent/'sales.conf';sales.write_text('sales untouched')
    cert=m.CERT_ROOT/m.cert_name('ssh.example.com');cert.mkdir(parents=True);(cert/'fullchain.pem').touch()
    calls=[]
    def run(*args,**kwargs):
        calls.append(tuple(map(str,args)))
        return SimpleNamespace(returncode=0,stdout='*:80 namevhost sales.example.com\n',stderr='')
    monkeypatch.setattr(m,'run',run)
    monkeypatch.setattr(m,'detect_web',lambda:'apache2')
    monkeypatch.setattr(m,'domain_conflict',lambda *a:None)
    monkeypatch.setattr(m,'old_v6_paths',lambda:[])
    monkeypatch.setattr(m,'bot_preflight',lambda c:{'username':'sshbot'})
    monkeypatch.setattr(m,'telegram',lambda *a,**k:{'type':'default'})
    monkeypatch.setattr(m,'web_dependencies',lambda s:None)
    monkeypatch.setattr(m,'test_web',lambda s:None)
    monkeypatch.setattr(m,'active',lambda s:True)
    monkeypatch.setattr(m,'free_port',lambda p:True)
    monkeypatch.setattr(m.socket,'getaddrinfo',lambda *a:[])
    monkeypatch.setattr(m.pwd,'getpwnam',lambda n:SimpleNamespace(pw_uid=os.getuid(),pw_gid=os.getgid()))
    monkeypatch.setattr('builtins.input',lambda prompt:'yes')
    return c,calls,sales,available,enabled


def test_failed_change_rolls_back_only_own_files(deployment,monkeypatch):
    c,calls,sales,available,enabled=deployment
    original=m.CONFIG.read_bytes();c['listen_port']=8800
    monkeypatch.setattr(m,'health',lambda c:(_ for _ in ()).throw(m.ManagerError('simulated TLS failure')))
    with pytest.raises(m.ManagerError):m.apply_config(c)
    assert m.CONFIG.read_bytes()==original
    assert available.read_text()==m.MARKER+'\nold site'
    assert enabled.is_symlink()
    assert sales.read_text()=='sales untouched'
    assert not m.HOOK.exists()
    assert not any(a[0]=='systemctl' and a[1] in ('stop','restart','disable') and a[2] in ('apache2','nginx','sshbot','mariadb') for a in calls)


def test_successful_change_reloads_apache_and_updates_only_sshbot(deployment,monkeypatch):
    c,calls,sales,available,enabled=deployment
    c['listen_port']=8800;menus=[]
    monkeypatch.setattr(m,'health',lambda c:None)
    monkeypatch.setattr(m,'telegram',lambda token,method,payload=None:menus.append((method,payload)) or {'type':'default'})
    m.apply_config(c)
    assert json.loads(m.CONFIG.read_text())['listen_port']==8800
    assert '127.0.0.1:8800' in available.read_text()
    assert sales.read_text()=='sales untouched'
    assert ('systemctl','restart','sshbot-web') in calls
    assert ('systemctl','reload','apache2') in calls
    assert any(a[0]=='setChatMenuButton' for a in menus)
    assert not any(a[0]=='systemctl' and a[1] in ('stop','restart','disable') and a[2]=='apache2' for a in calls)


def test_port_collision_prevents_mutation(deployment,monkeypatch):
    c,calls,sales,available,enabled=deployment
    before=available.read_text();c['listen_port']=8800
    monkeypatch.setattr(m,'free_port',lambda p:False)
    with pytest.raises(m.ManagerError):m.apply_config(c)
    assert available.read_text()==before and not calls


def test_domain_change_keeps_old_route_until_cert_and_updates_menu(deployment,monkeypatch):
    c,calls,sales,available,enabled=deployment
    c['public_url']='https://new.example.com'
    phases=[]
    def cert(c):
        content=available.read_text()
        assert 'old site' in content and 'ServerName new.example.com' in content
        phases.append('issued')
    old_run=m.run
    def run(*args,**kwargs):
        if args[0]=='curl' and '/.well-known/' in args[-1]:
            return SimpleNamespace(returncode=0,stdout=args[-1].rsplit('/',1)[-1],stderr='')
        return old_run(*args,**kwargs)
    monkeypatch.setattr(m,'run',run)
    monkeypatch.setattr(m,'issue_cert',cert)
    monkeypatch.setattr(m,'health',lambda c:phases.append('healthy'))
    menus=[]
    monkeypatch.setattr(m,'telegram',lambda token,method,payload=None:menus.append((method,payload)) or {'type':'default'})
    m.apply_config(c)
    assert phases==['issued','healthy']
    assert 'old site' not in available.read_text()
    assert 'ServerName new.example.com' in available.read_text()
    assert menus[-1][1]['menu_button']['web_app']['url']=='https://new.example.com'
    assert sales.read_text()=='sales untouched'


def test_cert_failure_restores_old_domain(deployment,monkeypatch):
    c,calls,sales,available,enabled=deployment
    before=m.CONFIG.read_bytes();c['public_url']='https://new.example.com'
    old_run=m.run
    def run(*args,**kwargs):
        if args[0]=='curl':return SimpleNamespace(returncode=0,stdout=args[-1].rsplit('/',1)[-1],stderr='')
        return old_run(*args,**kwargs)
    monkeypatch.setattr(m,'run',run)
    monkeypatch.setattr(m,'issue_cert',lambda c:(_ for _ in ()).throw(m.ManagerError('ACME unavailable')))
    with pytest.raises(m.ManagerError):m.apply_config(c)
    assert m.CONFIG.read_bytes()==before
    assert available.read_text()==m.MARKER+'\nold site'
    assert sales.read_text()=='sales untouched'


def test_release_preserves_service_executable_permissions(tmp_path,monkeypatch):
    monkeypatch.setattr(m,'APP',tmp_path/'app')
    source=tmp_path/'source';(source/'web').mkdir(parents=True);(source/'deploy').mkdir()
    (source/'web/app.py').write_text('# app')
    (source/'deploy/manager.py').write_text('# manager')
    (source/'requirements.txt').write_text('')
    def run(*args,**kw):
        if args[:3]==('python3','-m','venv'):
            venv=Path(args[3]);(venv/'bin').mkdir(parents=True)
            for name in ('python','uvicorn','pip'):
                p=venv/'bin'/name;p.write_text('#!/bin/sh\n');p.chmod(0o700)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(m,'run',run)
    release=m.prepare_release(source)
    assert (release/'venv/bin/uvicorn').stat().st_mode & 0o777 == 0o755
    assert m.APP.stat().st_mode & 0o005==0o005
