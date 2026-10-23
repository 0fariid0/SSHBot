#!/usr/bin/env python3
"""SSHBot 6.1 host manager. Standard library only; no shell-evaluated input."""
import argparse
import contextlib
import fcntl
import errno
import fnmatch
import getpass
import hashlib
import json
import os
import pwd
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP = Path('/opt/sshbot-web')
DATA = Path('/var/lib/sshbot-web')
CONFIG = Path('/etc/sshbot-web/config.json')
BACKUPS = Path('/var/backups/sshbot-web')
UNIT = Path('/etc/systemd/system/sshbot-web.service')
COMMAND = Path('/usr/local/sbin/sshbot-manager')
ACME = Path('/var/www/sshbot-acme')
ETC = Path('/etc')
ACME_CONFIG = Path('/etc/sshbot-web/letsencrypt')
ACME_WORK = Path('/var/lib/sshbot-web-acme')
ACME_LOGS = Path('/var/log/sshbot-web-acme')
CERT_ROOT = ACME_CONFIG/'live'
HOOK = ACME_CONFIG/'renewal-hooks/deploy/reload-web.sh'
RENEW_UNIT = Path('/etc/systemd/system/sshbot-web-certbot.service')
RENEW_TIMER = Path('/etc/systemd/system/sshbot-web-certbot.timer')
MARKER = '# Managed by SSHBot Web 6.1'
SERVICE = 'sshbot-web'
DEFAULTS = dict(listen_port=8765, session_hours=8, max_sessions=8, upload_mb=50, memory_mb=512, cpu_percent=50)


class ManagerError(Exception):
    pass


def run(*args, check=True, capture=True, timeout=120, input=None, env=None):
    result = subprocess.run([str(a) for a in args], input=input, text=True,
                            capture_output=capture, timeout=timeout, env=env)
    if check and result.returncode:
        # Arguments never contain a bot token; HTTP operations use in-memory payloads.
        raise ManagerError(f"فرمان ناموفق: {args[0]}\n" + (result.stderr or result.stdout or '')[-3000:])
    return result


def active(service):
    return run('systemctl', 'is-active', '--quiet', service, check=False).returncode == 0


def read_config():
    if not CONFIG.exists():
        return None
    data = json.loads(CONFIG.read_text())
    return dict(DEFAULTS, **data)


def validate(c):
    domain = c['public_url'].removeprefix('https://')
    if c['public_url'] != 'https://' + domain or len(domain) > 253 or not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', domain):
        raise ManagerError('دامنه معتبر نیست؛ فقط نام دامنه بدون مسیر وارد کنید.')
    if not re.fullmatch(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', c.get('email', '')):
        raise ManagerError('ایمیل معتبر لازم است.')
    if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]{15,}', c.get('bot_token', '')):
        raise ManagerError('توکن ربات معتبر نیست.')
    for field, low, high in [('owner_id',1,2**53-1),('listen_port',1024,65535),('session_hours',1,24),('max_sessions',1,8),('upload_mb',1,50),('memory_mb',256,4096),('cpu_percent',10,400)]:
        if type(c.get(field)) is not int or not low <= c[field] <= high:
            raise ManagerError(f'مقدار {field} باید بین {low} و {high} باشد.')
    if c.get('web_server') not in ('apache2','nginx'):
        raise ManagerError('وب‌سرور ناشناخته است.')
    if c.get('data_dir') != str(DATA):
        raise ManagerError('مسیر داده سفارشی نیاز به انتقال دستی دارد؛ تغییری اعمال نشد.')
    return domain


def free_port(port):
    sockets = []
    try:
        for family, address in [(socket.AF_INET,'0.0.0.0'), (socket.AF_INET6,'::')]:
            try:
                sock = socket.socket(family)
            except OSError:
                continue
            sockets.append(sock)
            if family == socket.AF_INET6:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            try:
                sock.bind((address, port))
            except OSError as exc:
                if family==socket.AF_INET6 and exc.errno in (errno.EAFNOSUPPORT,errno.EADDRNOTAVAIL):continue
                raise
        return True
    except OSError:
        return False
    finally:
        for sock in sockets:
            sock.close()


def choose_port():
    for port in range(8765, 8865):
        if free_port(port):
            return port
    raise ManagerError('پورت آزاد در بازه پیش‌فرض پیدا نشد.')


def detect_web():
    owners = set()
    for port in (80,443):
        rows = run('ss','-H','-ltnp',f'sport = :{port}').stdout
        for line in rows.splitlines():
            names = re.findall(r'\("([^"]+)"', line)
            if not names or any(n not in ('apache2','nginx') for n in names):
                raise ManagerError(f'پورت {port} در اختیار سرویس دیگری است؛ معماری دستی لازم است. هیچ سرویسی متوقف نشد.')
            owners.update(names)
    if len(owners) > 1:
        raise ManagerError('Apache و Nginx هم‌زمان روی پورت‌های عمومی هستند؛ تنظیم ترکیبی نیاز به بررسی دستی دارد.')
    if owners:
        server = owners.pop()
        if not active(server):
            raise ManagerError('مالک پورت با سرویس استاندارد سیستم تطبیق ندارد.')
        return server
    for service in ('apache2','nginx'):
        if active(service):
            return service
    if Path('/etc/apache2/apache2.conf').exists() or Path('/etc/nginx/nginx.conf').exists():
        raise ManagerError('وب‌سرور نصب است ولی فعال نیست؛ ابتدا وضعیت آن را مشخص کنید.')
    return 'apache2'


def site_paths(server):
    base = ETC / ('apache2' if server == 'apache2' else 'nginx')
    return base/'sites-available/zz-sshbot-web.conf', base/'sites-enabled/zz-sshbot-web.conf'


def old_v6_paths():
    source=Path('/etc/nginx/sites-available/sshbot-web')
    if CONFIG.exists() and UNIT.exists() and source.exists():
        text=source.read_text()
        if '/opt/sshbot-web/' in UNIT.read_text() and 'proxy_pass http://127.0.0.1:8765;' in text and 'sshbot-acme' in text:
            return [source, Path('/etc/nginx/sites-enabled/sshbot-web')]
    return []


def domain_conflict(domain, server):
    own = {p.resolve() for p in site_paths(server) + tuple(old_v6_paths())}
    # Conservative scan: enabled and disabled sites, plus additional conf fragments.
    base=ETC/server
    for p in base.rglob('*'):
        if not p.is_file() or p.resolve() in own or p.suffix not in ('.conf',''):
            continue
        try:
            text=p.read_text()
        except (UnicodeError, OSError):
            continue
        text=re.sub(r'#.*','',text)
        names=[]
        if server=='apache2':
            for line in text.splitlines():
                m=re.match(r'\s*Server(?:Name|Alias)\s+(.+)',line,re.I)
                if m:names.extend(m.group(1).split())
        else:
            for m in re.finditer(r'\bserver_name\s+([^;]+);',text):names.extend(m.group(1).split())
        for name in names:
            name=name.strip('"\'').lower().split(':')[0]
            if name.startswith('~'):
                raise ManagerError(f'دامنه regex در {p} نیاز به بررسی دستی دارد.')
            if name in ('_',''):
                continue
            if domain==name or fnmatch.fnmatchcase(domain,name) or (name.startswith('.') and (domain==name[1:] or domain.endswith(name))):
                raise ManagerError(f'دامنه با سایت موجود در {p} تداخل دارد؛ زیردامنه دیگری انتخاب کنید.')


def telegram(token, method, payload=None):
    data=json.dumps(payload or {}).encode()
    req=urllib.request.Request(f'https://api.telegram.org/bot{token}/{method}', data=data, headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            obj=json.load(r)
        if not obj.get('ok'):
            raise ValueError()
        return obj['result']
    except Exception:
        # Never include URLs, which contain the bot token.
        raise ManagerError(f'ارتباط Telegram برای {method} ناموفق بود؛ توکن و شبکه را بررسی کنید.') from None


def bot_preflight(c):
    bot=telegram(c['bot_token'],'getMe')
    hook=telegram(c['bot_token'],'getWebhookInfo')
    if hook.get('url'):
        raise ManagerError('این توکن Webhook فعال دارد؛ احتمالاً متعلق به ربات فروش است. توکن جدا وارد کنید. Webhook حذف نشد.')
    legacy=Path('/etc/sshbot.env')
    if legacy.exists() and c['bot_token'] in legacy.read_text() and active('sshbot'):
        raise ManagerError('همین توکن در سرویس قدیمی sshbot فعال است. آن سرویس خودکار متوقف نمی‌شود؛ از توکن جدا استفاده کنید یا انتقال را دستی انجام دهید.')
    return bot


def cert_name(domain):
    return 'sshbot-web-' + hashlib.sha256(domain.encode()).hexdigest()[:16]


def render_site(c, tls=True):
    domain=c['public_url'][8:]
    port=c['listen_port']
    cert=c.get('certificate',cert_name(domain))
    root=str(ACME)
    if c['web_server']=='apache2':
        http=f'''{MARKER}
<VirtualHost *:80>
    ServerName {domain}
    DocumentRoot {root}
    <Directory {root}>
        Options None
        AllowOverride None
        Require all granted
    </Directory>
    RewriteEngine On
    RewriteCond %{{REQUEST_URI}} !^/\\.well-known/acme-challenge/
    RewriteRule ^ https://{domain}%{{REQUEST_URI}} [R=302,L,NE]
</VirtualHost>
'''
        if not tls:
            return http
        return http+f'''<VirtualHost *:443>
    ServerName {domain}
    SSLEngine on
    SSLCertificateFile {CERT_ROOT}/{cert}/fullchain.pem
    SSLCertificateKeyFile {CERT_ROOT}/{cert}/privkey.pem
    SSLProtocol -all +TLSv1.2 +TLSv1.3
    ProxyRequests Off
    ProxyPreserveHost On
    ProxyTimeout 3600
    RequestHeader set X-Forwarded-Proto "https"
    LimitRequestBody {(c['upload_mb']+1)*1024*1024}
    ProxyPass /ws ws://127.0.0.1:{port}/ws timeout=3600
    ProxyPassReverse /ws ws://127.0.0.1:{port}/ws
    ProxyPass / http://127.0.0.1:{port}/ timeout=3600
    ProxyPassReverse / http://127.0.0.1:{port}/
</VirtualHost>
'''
    http=f'''{MARKER}
server {{
    listen 80;
    listen [::]:80;
    server_name {domain};
    location /.well-known/acme-challenge/ {{ root {root}; }}
    location / {{ return 302 https://{domain}$request_uri; }}
}}
'''
    if not tls:return http
    return http+f'''server {{
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name {domain};
    ssl_certificate {CERT_ROOT}/{cert}/fullchain.pem;
    ssl_certificate_key {CERT_ROOT}/{cert}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    client_max_body_size {c['upload_mb']+1}m;
    location / {{
        proxy_pass http://127.0.0.1:{port};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
        proxy_buffering off;
    }}
}}
'''


def render_unit(c, release):
    return f'''{MARKER}
[Unit]
Description=SSHBot Web isolated Mini App
After=network-online.target
Wants=network-online.target
[Service]
User=sshbot-web
Group=sshbot-web
WorkingDirectory={release}
Environment=SSHBOT_CONFIG={CONFIG}
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart={release}/venv/bin/uvicorn web.app:app --host 127.0.0.1 --port {c['listen_port']} --workers 1 --ws-max-size 131072 --no-access-log
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths={DATA}
CPUQuota={c['cpu_percent']}%
MemoryMax={c['memory_mb']}M
TasksMax=128
Nice=10
[Install]
WantedBy=multi-user.target
'''


class FileTransaction:
    """Rollback only explicitly registered files/symlinks, never whole /etc trees."""
    def __init__(self):
        self.before={}

    def capture(self,p):
        p=Path(p)
        if p in self.before:return
        if p.is_symlink():self.before[p]=('link',os.readlink(p))
        elif p.exists():
            st=p.stat();self.before[p]=('file',p.read_bytes(),st.st_mode & 0o777,st.st_uid,st.st_gid)
        else:self.before[p]=None

    def write(self,p,body,mode=0o644,uid=0,gid=0):
        p=Path(p);self.capture(p);p.parent.mkdir(parents=True,exist_ok=True)
        tmp=p.with_name(p.name+'.sshbot-'+secrets.token_hex(4))
        try:
            tmp.write_text(body);os.chmod(tmp,mode);os.chown(tmp,uid,gid);os.replace(tmp,p)
        finally:
            tmp.unlink(missing_ok=True)

    def link(self,p,target):
        p=Path(p);self.capture(p);p.parent.mkdir(parents=True,exist_ok=True)
        p.unlink(missing_ok=True);p.symlink_to(target)

    def remove(self,p):
        p=Path(p);self.capture(p);p.unlink(missing_ok=True)

    def rollback(self):
        for p,value in reversed(list(self.before.items())):
            p.unlink(missing_ok=True)
            if value is None:continue
            if value[0]=='link':p.symlink_to(value[1])
            else:
                p.write_bytes(value[1]);os.chmod(p,value[2]);os.chown(p,value[3],value[4])


def test_web(server):
    run(*(['apache2ctl','configtest'] if server=='apache2' else ['nginx','-t']))


def reload_web(server):
    test_web(server)
    if active(server):run('systemctl','reload',server)
    else:run('systemctl','start',server)


def app_sql(sql, params=(), fetch=False):
    code="""import json,sqlite3,sys
q=json.load(sys.stdin)
db=sqlite3.connect(q['path'],timeout=10)
try:
    with db:
        cur=db.execute(q['sql'],q['params'])
        rows=cur.fetchall() if q['fetch'] else []
    print(json.dumps(rows))
finally:db.close()
"""
    result=run('runuser','-u','sshbot-web','--','python3','-c',code,input=json.dumps({'path':str(DATA/'vault.sqlite3'),'sql':sql,'params':params,'fetch':fetch}))
    return json.loads(result.stdout)


def snapshot(label='manual'):
    BACKUPS.mkdir(parents=True,exist_ok=True,mode=0o700)
    dest=BACKUPS/(time.strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(3)+'-'+label)
    dest.mkdir(mode=0o700)
    if CONFIG.exists():shutil.copy2(CONFIG,dest/'config.json')
    if (DATA/'vault.sqlite3').exists():
        temporary=DATA/('.backup-'+secrets.token_hex(12)+'.sqlite3')
        code="import sqlite3,sys; a=sqlite3.connect(sys.argv[1]); b=sqlite3.connect(sys.argv[2]); a.backup(b); b.close(); a.close()"
        try:
            run('runuser','-u','sshbot-web','--','python3','-c',code,DATA/'vault.sqlite3',temporary)
            shutil.copy2(temporary,dest/'vault.sqlite3')
        finally:temporary.unlink(missing_ok=True)
        shutil.copy2(DATA/'vault.key',dest/'vault.key')
    c=read_config()
    if c:
        for p in [UNIT,RENEW_UNIT,RENEW_TIMER,*site_paths(c.get('web_server','nginx')),*old_v6_paths()]:
            if p.exists() and not p.is_symlink():
                target=dest/'files'/str(p).lstrip('/');target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target)
    print('پشتیبان خصوصی:',dest)
    return dest


def web_dependencies(server):
    # Install only missing packages. Never upgrade an existing web server/PHP/MariaDB.
    packages=['python3-venv','ca-certificates','certbot','curl',server]
    missing=[p for p in packages if run('dpkg-query','-W','-f=${Status}',p,check=False).stdout.strip()!='install ok installed']
    if missing:
        env=dict(os.environ,DEBIAN_FRONTEND='noninteractive',NEEDRESTART_MODE='l')
        run('apt-get','update',capture=False,timeout=600,env=env)
        policy=FileTransaction()
        try:
            policy.write(Path('/usr/sbin/policy-rc.d'), '#!/bin/sh\n# Temporary SSHBot package-install guard\nexit 101\n', 0o755)
            run('apt-get','install','-y','--no-upgrade',*missing,capture=False,timeout=900,env=env)
        finally:
            policy.rollback()
    if server=='apache2':
        # Do not alter MPM, PHP handlers, existing sites or ports.conf.
        run('a2enmod','proxy','proxy_http','proxy_wstunnel','ssl','headers','rewrite')


def prepare_release(source):
    if not (source/'web/app.py').exists() or not (source/'requirements.txt').exists():
        raise ManagerError('بسته کامل سورس را استخراج کنید.')
    release=APP/'releases'/(time.strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(3))
    release.mkdir(parents=True,mode=0o755)
    shutil.copytree(source/'web',release/'web',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    shutil.copytree(source/'deploy',release/'deploy',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    shutil.copy2(source/'requirements.txt',release/'requirements.txt')
    run('python3','-m','venv',release/'venv',timeout=120)
    run(release/'venv/bin/pip','install','--disable-pip-version-check','-r',release/'requirements.txt',capture=False,timeout=900)
    # No credentials are stored in program directories.
    for p in [APP,APP/'releases',release,*release.rglob('*')]:
        if p.is_symlink():continue
        os.chmod(p,(p.stat().st_mode & 0o777)|0o444|(0o111 if p.is_dir() or p.stat().st_mode & 0o111 else 0))
    return release


def owned_preflight(c):
    for p in site_paths(c['web_server']):
        if p.exists() and MARKER not in p.read_text():
            raise ManagerError(f'فایل {p} متعلق به این مدیر نیست؛ بازنویسی نشد.')
    if UNIT.exists() and '/opt/sshbot-web/' not in UNIT.read_text():
        raise ManagerError('نام سرویس sshbot-web قبلاً برای برنامه دیگری استفاده شده است.')
    for p in (RENEW_UNIT,RENEW_TIMER):
        if p.exists() and MARKER not in p.read_text():
            raise ManagerError('نام سرویس تمدید SSL با سرویس موجود تداخل دارد.')
    if HOOK.exists() and MARKER not in HOOK.read_text():
        raise ManagerError('فایل hook تمدید SSL با نام یکسان متعلق به این مدیر نیست.')
    if COMMAND.exists() and 'SSHBot' not in COMMAND.read_text():
        raise ManagerError('فرمان sshbot-manager متعلق به برنامه دیگری است.')
    if (APP/'current').exists() and not (APP/'current').is_symlink():
        raise ManagerError('مسیر current ناشناخته است.')
    if not CONFIG.exists() and DATA.exists() and any(DATA.iterdir()):
        raise ManagerError('مسیر داده موجود است ولی تنظیمات مالکیت SSHBot پیدا نشد؛ بررسی دستی لازم است.')
    if c.get('web_server')=='nginx' and not Path('/etc/nginx/sites-enabled').exists() and Path('/etc/nginx/nginx.conf').exists():
        raise ManagerError('Nginx شما ساختار sites-enabled استاندارد ندارد؛ بررسی دستی لازم است.')


def certbot_args():
    return ['--config-dir',str(ACME_CONFIG),'--work-dir',str(ACME_WORK),'--logs-dir',str(ACME_LOGS)]


def issue_cert(c):
    domain=c['public_url'][8:]
    run('certbot','certonly','--webroot','-w',ACME,'--non-interactive','--agree-tos','--email',c['email'],
        '--cert-name',c['certificate'],'-d',domain,'--keep-until-expiring',*certbot_args(),capture=False,timeout=240)


def health(c):
    for attempt in range(20):
        try:
            req=urllib.request.Request(f"http://127.0.0.1:{c['listen_port']}/health")
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=2) as r:
                if json.load(r).get('version')=='6.1.0':break
        except Exception:pass
        time.sleep(.5)
    else:raise ManagerError('سرویس جدید سالم بالا نیامد؛ تنظیمات قبلی بازگردانده می‌شود.')
    # Explicit domain routes check locally and publicly, with TLS certificate validation.
    for extra in [[],['--resolve',f"{c['public_url'][8:]}:443:127.0.0.1"]]:
        response=run('curl','--noproxy','*','-fsS','--max-time','15',*extra,c['public_url']+'/health')
        if json.loads(response.stdout).get('version')!='6.1.0':raise ManagerError('دامنه به برنامه جدید نمی‌رسد.')


def apply_config(c,source=None):
    old=read_config()
    if not source and not (APP/'current/venv/bin/python').exists():
        raise ManagerError('برای ارتقای نسخه قبلی ابتدا گزینه ۱ را از بسته جدید اجرا کنید.')
    domain=validate(c)
    detected=detect_web()
    if detected!=c['web_server']:
        raise ManagerError('وب‌سرور تشخیص‌داده‌شده با تنظیمات یکسان نیست؛ تغییر معماری خودکار انجام نمی‌شود.')
    owned_preflight(c);domain_conflict(domain,c['web_server'])
    if not old or old['listen_port']!=c['listen_port'] or not active(SERVICE):
        if not free_port(c['listen_port']):raise ManagerError('پورت داخلی انتخاب‌شده اشغال است.')
    try:socket.getaddrinfo(domain,80)
    except socket.gaierror:raise ManagerError('دامنه resolve نمی‌شود؛ ابتدا DNS را تنظیم کنید.')
    if shutil.disk_usage('/opt').free < 512*1024*1024:
        raise ManagerError('کمتر از ۵۱۲ مگابایت فضای آزاد برای نصب و پشتیبان وجود دارد.')
    mem=re.search(r'^MemAvailable:\s+(\d+)',Path('/proc/meminfo').read_text(),re.M)
    if mem and int(mem.group(1))<128*1024:
        raise ManagerError('حافظه آزاد برای نصب کم است؛ ابتدا منابع سرور را بررسی کنید.')
    bot=bot_preflight(c)
    previous_menu=telegram(c['bot_token'],'getChatMenuButton')
    print(f"وب‌سرور: {detected} | دامنه: {domain} | پورت داخلی: {c['listen_port']} | ربات: @{bot['username']}")
    print('فقط فایل‌های SSHBot تغییر می‌کنند. اتصال‌های SSHBot هنگام اعمال تنظیمات بسته خواهند شد.')
    if input('برای اعمال بنویسید yes: ').strip()!='yes':return
    snapshot('before-change')
    if active(detected):
        test_web(detected)
        if detected=='apache2':
            vhosts=run('apache2ctl','-S')
            for filename in re.findall(r'\((/[^():]+):\d+\)',vhosts.stdout+vhosts.stderr):
                if not filename.startswith('/etc/apache2/'):
                    raise ManagerError('VirtualHost خارج از مسیر استاندارد Apache دیده شد؛ بررسی دستی لازم است.')
            if not re.search(r'(?:\*:80|port 80|:80\s)', vhosts.stdout+vhosts.stderr):
                raise ManagerError('Apache سایت اصلی بدون VirtualHost دارد؛ برای حفظ مسیرهای موجود، تنظیم دستی لازم است.')
    web_dependencies(detected)
    test_web(detected)
    release=prepare_release(source) if source else APP/'current'
    if not (release/'venv/bin/python').exists():
        # Upgrade v6.0 must be executed from the new extracted package.
        raise ManagerError('نسخه ۶.۱ نصب نیست؛ گزینه نصب/ارتقا را از بسته جدید اجرا کنید.')
    try:account=pwd.getpwnam('sshbot-web')
    except KeyError:
        run('useradd','--system','--home',DATA,'--shell','/usr/sbin/nologin','sshbot-web');account=pwd.getpwnam('sshbot-web')
    DATA.mkdir(parents=True,exist_ok=True,mode=0o700);os.chmod(DATA,0o700);os.chown(DATA,account.pw_uid,account.pw_gid)
    CONFIG.parent.mkdir(parents=True,exist_ok=True,mode=0o750);os.chown(CONFIG.parent,0,account.pw_gid);os.chmod(CONFIG.parent,0o750)
    ACME.mkdir(parents=True,exist_ok=True,mode=0o755);os.chmod(ACME,0o755)
    tx=FileTransaction();was_active=active(SERVICE);menu_changed=False
    timer_was_active=active('sshbot-web-certbot.timer')
    timer_was_enabled=run('systemctl','is-enabled','--quiet','sshbot-web-certbot.timer',check=False).returncode==0
    service_was_enabled=run('systemctl','is-enabled','--quiet',SERVICE,check=False).returncode==0
    available,enabled=site_paths(detected)
    c['certificate']=cert_name(domain)
    try:
        needs_cert=not (CERT_ROOT/c['certificate']/'fullchain.pem').exists()
        if needs_cert:
            existing=available.read_text() if available.exists() else ''
            # Keep the old domain's HTTPS routes until the new certificate is ready.
            if old and old['public_url']==c['public_url']:existing=''
            tx.write(available,existing+render_site(c,tls=False))
            tx.link(enabled,available);reload_web(detected)
            probe=secrets.token_hex(16);challenge=ACME/'.well-known/acme-challenge'/probe
            challenge.parent.mkdir(parents=True,exist_ok=True,mode=0o755)
            os.chmod(challenge.parent.parent,0o755);os.chmod(challenge.parent,0o755)
            challenge.write_text(probe);os.chmod(challenge,0o644)
            try:
                response=run('curl','--noproxy','*','-fsS','--max-time','15',f'http://{domain}/.well-known/acme-challenge/{probe}')
                if response.stdout!=probe:raise ManagerError('دامنه/پروکسی به این VPS نمی‌رسد؛ SSL درخواست نشد.')
            finally:challenge.unlink(missing_ok=True)
            issue_cert(c)
        for p in old_v6_paths():tx.remove(p)
        tx.write(available,render_site(c));tx.link(enabled,available)
        # No global renewal hooks: the hook reloads only the selected existing web server.
        hook=HOOK
        cmd='apache2ctl configtest' if detected=='apache2' else 'nginx -t'
        tx.write(hook,f'#!/bin/sh\n{MARKER}\ncase "$RENEWED_LINEAGE" in\n  */sshbot-web-*) {cmd} && systemctl reload {detected} ;;\nesac\n',0o755)
        test_web(detected)
        tx.write(RENEW_UNIT,f'{MARKER}\n[Unit]\nDescription=SSHBot private certificate renewal\n[Service]\nType=oneshot\nExecStart=/usr/bin/certbot renew --quiet '+ ' '.join(certbot_args())+'\n')
        tx.write(RENEW_TIMER,f'{MARKER}\n[Unit]\nDescription=SSHBot private certificate renewal timer\n[Timer]\nOnCalendar=*-*-* 03,15:00:00\nRandomizedDelaySec=3600\nPersistent=true\n[Install]\nWantedBy=timers.target\n')
        tx.write(CONFIG,json.dumps(c,ensure_ascii=False,indent=2),0o640,gid=account.pw_gid)
        if source:tx.link(APP/'current',release)
        tx.write(UNIT,render_unit(c,release.resolve()))
        tx.write(COMMAND,f'#!/bin/sh\n{MARKER}\nexec python3 {APP}/current/deploy/manager.py "$@"\n',0o755)
        if (DATA/'vault.sqlite3').exists():
            app_sql('DELETE FROM sessions')
        run('systemctl','daemon-reload');run('systemctl','restart',SERVICE)
        reload_web(detected);health(c)
        if old and old.get('email') != c['email']:
            run('certbot','update_account','--non-interactive','--email',c['email'],*certbot_args(),capture=False)
        menu_changed=True
        telegram(c['bot_token'],'setChatMenuButton',{'menu_button':{'type':'web_app','text':'SSHBot','web_app':{'url':c['public_url']}}})
        run('systemctl','enable',SERVICE)
        run('systemctl','enable','--now','sshbot-web-certbot.timer')
        print('✓ اعمال شد. منوی بعدی: sudo sshbot-manager')
    except BaseException:
        print('خطا: بازگردانی فایل‌ها و سرویس SSHBot به حالت قبلی…')
        run('systemctl','stop',SERVICE,check=False)
        if not timer_was_active:run('systemctl','stop','sshbot-web-certbot.timer',check=False)
        if not timer_was_enabled:run('systemctl','disable','sshbot-web-certbot.timer',check=False)
        if not service_was_enabled:run('systemctl','disable',SERVICE,check=False)
        tx.rollback();run('systemctl','daemon-reload',check=False)
        try:reload_web(detected)
        except Exception:print('بازخوانی وب‌سرور ناموفق بود؛ configtest و لاگ را بررسی کنید.')
        if was_active:run('systemctl','start',SERVICE,check=False)
        if menu_changed:
            try:telegram(c['bot_token'],'setChatMenuButton',{'menu_button':previous_menu})
            except Exception:print('بازگردانی دکمه Telegram ناموفق بود؛ از منوی تعمیر دکمه استفاده کنید.')
        raise


def ask(label,default=None,secret=False):
    prompt=label+(f' [{default}]' if default is not None and not secret else '')+': '
    value=(getpass.getpass(prompt) if secret else input(prompt)).strip()
    return value if value else (default if default is not None else '')


def installed():
    c=read_config()
    if not c:raise ManagerError('ابتدا نصب کنید.')
    return c


def new_config():
    old=read_config() or {}
    c=dict(DEFAULTS,**old)
    c['public_url']='https://'+ask('دامنه اختصاصی، بدون https',old.get('public_url','')[8:] or None).lower()
    c['email']=ask('ایمیل SSL',old.get('email'))
    c['owner_id']=int(ask('آیدی عددی مالک',old.get('owner_id')))
    c['bot_token']=ask('توکن جداگانه SSHBot؛ خالی = حفظ قبلی',old.get('bot_token'),secret=True)
    c['web_server']=detect_web();c['data_dir']=str(DATA)
    c['listen_port']=int(ask('پورت داخلی',old['listen_port'] if 'listen_port' in old else choose_port()))
    return c


def settings(choice):
    c=installed()
    if choice=='2':c['public_url']='https://'+ask('دامنه جدید، بدون https').lower()
    elif choice=='3':c['bot_token']=ask('توکن جدید، مستقل از فروش',secret=True)
    elif choice=='4':c['owner_id']=int(ask('آیدی عددی مالک جدید'))
    elif choice=='5':c['listen_port']=int(ask('پورت داخلی جدید',choose_port()))
    elif choice=='6':
        c['email']=ask('ایمیل SSL',c.get('email'))
        for key,label in [('session_hours','اعتبار ورود به ساعت (۱ تا ۲۴)'),('max_sessions','تعداد اتصال هم‌زمان هر کاربر (۱ تا ۸)'),('upload_mb','حداکثر آپلود MB (۱ تا ۵۰)'),('memory_mb','حد حافظه MB (۲۵۶ تا ۴۰۹۶)'),('cpu_percent','سقف CPU درصد (۱۰ تا ۴۰۰؛ ۱۰۰ = یک هسته)')]:
            c[key]=int(ask(label,c[key]))
    apply_config(c)


def users_menu():
    c=installed();db_path=DATA/'vault.sqlite3'
    if not db_path.exists():raise ManagerError('دیتابیس هنوز ساخته نشده است.')
    print('مالک:',c['owner_id'],' | کاربران:',[r[0] for r in app_sql('SELECT uid FROM users',fetch=True)])
    action=ask('1 افزودن | 2 لغو دسترسی | Enter برگشت')
    if action not in ('1','2'):return
    uid=int(ask('آیدی عددی'))
    if not 0<uid<2**53 or uid==c['owner_id']:raise ManagerError('آیدی نامعتبر یا متعلق به مالک است.')
    if action=='1':app_sql('INSERT OR IGNORE INTO users VALUES(?)',(uid,))
    else:
        app_sql('DELETE FROM users WHERE uid=?',(uid,));app_sql('DELETE FROM sessions WHERE uid=?',(uid,))
    print('انجام شد؛ لغو دسترسی حداکثر طی یک ثانیه اتصال‌های همان کاربر را می‌بندد.')


def restore_data():
    installed()
    candidates=sorted(p for p in BACKUPS.glob('*') if p.is_dir() and (p/'vault.sqlite3').exists() and (p/'vault.key').exists())
    for i,p in enumerate(candidates,1):print(i,p.name)
    if not candidates:raise ManagerError('پشتیبان داده پیدا نشد.')
    idx=int(ask('شماره پشتیبان'))-1
    if not 0<=idx<len(candidates):raise ManagerError('شماره نامعتبر است.')
    target=candidates[idx]
    with contextlib.closing(sqlite3.connect(f'file:{target}/vault.sqlite3?mode=ro',uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ManagerError('پشتیبان خراب است.')
    verify="import json,sqlite3,sys; from pathlib import Path; from cryptography.fernet import Fernet; p=Path(sys.argv[1]); f=Fernet((p/'vault.key').read_bytes()); db=sqlite3.connect('file:'+str(p/'vault.sqlite3')+'?mode=ro',uri=True); [f.decrypt(json.loads(row[0])['encrypted'].encode()) for row in db.execute(\"SELECT body FROM items WHERE kind='credential'\")]; db.close()"
    run(APP/'current/venv/bin/python','-c',verify,target)
    print('فقط سرورها، Keychain و کاربران بازیابی می‌شوند. دامنه، توکن و مالک فعلی حفظ می‌شوند. نشست‌ها باطل می‌شوند.')
    if ask('برای جایگزینی داده فعلی بنویسید RESTORE')!='RESTORE':return
    before=snapshot('before-restore');was_active=active(SERVICE)
    run('systemctl','stop',SERVICE)
    account=pwd.getpwnam('sshbot-web')
    def copy_pair(source):
        for name in ('vault.sqlite3-wal','vault.sqlite3-shm'):(DATA/name).unlink(missing_ok=True)
        for name in ('vault.sqlite3','vault.key'):
            shutil.copyfile(source/name,DATA/name);os.chmod(DATA/name,0o600);os.chown(DATA/name,account.pw_uid,account.pw_gid)
        with contextlib.closing(sqlite3.connect(DATA/'vault.sqlite3')) as db:
            with db:db.execute('DELETE FROM sessions')
    try:copy_pair(target)
    except BaseException:copy_pair(before);raise
    finally:
        if was_active:run('systemctl','start',SERVICE)
    print('بازیابی انجام شد.')


def import_legacy():
    installed()
    src=Path(ask('مسیر کامل servers.json قدیمی','/opt/sshbot/data/servers.json'))
    key=Path(ask('مسیر کامل keychain.key قدیمی','/opt/sshbot/data/keychain.key'))
    if not src.is_absolute() or not src.is_file():raise ManagerError('فایل JSON معتبر پیدا نشد.')
    print('سرویس قدیمی متوقف نمی‌شود؛ داده قدیمی فقط خوانده می‌شود. کاربران منتقل‌شده مجوز ورود خودکار نمی‌گیرند.')
    if ask('برای انتقال بنویسید yes')!='yes':return
    snapshot('before-import');was_active=active(SERVICE)
    run('systemctl','stop',SERVICE)
    try:
        env=dict(os.environ,PYTHONPATH=str(APP/'current'))
        run(APP/'current/venv/bin/python','-m','web.migrate','--source',src,'--key',key,'--target',DATA,capture=False,env=env)
    finally:
        account=pwd.getpwnam('sshbot-web')
        for p in [DATA,*DATA.iterdir()]:os.chown(p,account.pw_uid,account.pw_gid)
        if was_active:run('systemctl','start',SERVICE)


def status():
    c=read_config()
    print('سرویس:', 'فعال' if active(SERVICE) else 'متوقف/نصب‌نشده')
    if c:
        print('دامنه:',c['public_url'],'| مالک:',c['owner_id'],'| پورت:',c['listen_port'])
        print('توکن: مخفی | وب‌سرور:',c.get('web_server','نسخه قدیمی'))
    run('systemctl','status',SERVICE,'--no-pager','-l',check=False,capture=False)


def diagnostics():
    print('مالک پورت‌های عمومی:')
    for port in (80,443):print(run('ss','-H','-ltnp',f'sport = :{port}').stdout)
    c=read_config()
    if c:
        print('تشخیص وب‌سرور:',detect_web());test_web(c.get('web_server','nginx'))
        bot=bot_preflight(c);print('ربات:',bot['username'])
        health(c);print('✓ HTTP داخلی و HTTPS سالم‌اند.')


def ssl_menu():
    c=installed()
    print('1 وضعیت SSL | 2 تست تمدید (dry-run) | 3 تمدید در صورت نیاز')
    action=ask('انتخاب')
    if action=='1':run('certbot','certificates','--cert-name',c['certificate'],*certbot_args(),capture=False)
    elif action in ('2','3'):
        args=['certbot','renew','--cert-name',c['certificate'],*certbot_args()]
        if action=='2':args.append('--dry-run')
        run(*args,capture=False,timeout=300)
        if action=='3':reload_web(c['web_server'])


def main():
    parser=argparse.ArgumentParser(description='SSHBot management menu')
    parser.add_argument('--source',type=Path)
    parser.add_argument('--status',action='store_true')
    args=parser.parse_args()
    if os.geteuid()!=0:raise ManagerError('با sudo اجرا کنید.')
    os.umask(0o077)
    if not shutil.which('systemctl') or not shutil.which('ss'):raise ManagerError('Ubuntu با systemd و iproute2 لازم است.')
    osrelease=Path('/etc/os-release').read_text()
    if not re.search(r'^ID=ubuntu$',osrelease,re.M) or not re.search(r'^VERSION_ID="(?:22\.04|24\.04)"$',osrelease,re.M):raise ManagerError('نسخه‌های پشتیبانی‌شده: Ubuntu 22.04 و 24.04')
    with open('/run/lock/sshbot-manager.lock','w') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ManagerError('یک مدیر نصب دیگر در حال اجراست.')
        if args.status:status();return
        while True:
            print('\n╭──────── SSHBot 6.1 · مدیریت ────────╮')
            current=read_config()
            if current:print(current['public_url'], '| مالک:',current['owner_id'], '| پورت:',current['listen_port'])
            print(' 1 نصب / ارتقا از همین بسته\n 2 تغییر دامنه + SSL + دکمه ربات\n 3 تغییر توکن ربات\n 4 تغییر مالک\n 5 تغییر پورت داخلی\n 6 تنظیمات ورود، آپلود و منابع\n 7 کاربران مجاز\n 8 وضعیت سرویس\n 9 شروع / توقف / راه‌اندازی مجدد\n10 نمایش لاگ\n11 بررسی تداخل و سلامت\n12 مدیریت SSL\n13 ساخت پشتیبان\n14 بازیابی داده\n15 تعمیر دکمه Mini App\n16 انتقال داده ربات قدیمی\n 0 خروج')
            try:
                choice=ask('انتخاب')
                if choice=='0':break
                if choice=='1':
                    if not args.source:raise ManagerError('برای ارتقا، بسته جدید را استخراج و sudo bash install.sh را از آن اجرا کنید.')
                    apply_config(new_config(),args.source.resolve())
                elif choice in ('2','3','4','5','6'):settings(choice)
                elif choice=='7':users_menu()
                elif choice=='8':status()
                elif choice=='9':
                    action=ask('1 شروع | 2 توقف | 3 راه‌اندازی مجدد')
                    if action in ('1','2','3'):run('systemctl',{'1':'start','2':'stop','3':'restart'}[action],SERVICE)
                elif choice=='10':run('journalctl','-u',SERVICE,'-n','80','--no-pager',capture=False)
                elif choice=='11':diagnostics()
                elif choice=='12':ssl_menu()
                elif choice=='13':installed();snapshot()
                elif choice=='14':restore_data()
                elif choice=='16':import_legacy()
                elif choice=='15':
                    c=installed();bot_preflight(c)
                    telegram(c['bot_token'],'setChatMenuButton',{'menu_button':{'type':'web_app','text':'SSHBot','web_app':{'url':c['public_url']}}});print('✓ دکمه تنظیم شد.')
            except (ManagerError, ValueError, KeyError, OSError, subprocess.TimeoutExpired) as exc:
                print('خطا:',str(exc))
            except KeyboardInterrupt:
                print('\nعملیات لغو شد.')
            input('\nEnter برای برگشت به منو…')


if __name__=='__main__':
    try:main()
    except (ManagerError,EOFError,KeyboardInterrupt) as e:
        print(str(e));sys.exit(1)
