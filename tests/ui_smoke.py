"""UI-only smoke test with explicit fixture API data (no production bypass)."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

root=Path(__file__).resolve().parents[1]/'web/static'
hosts=[{'id':'h1','name':'Germany · Production','host':'192.0.2.10','port':22,'username':'root','group':'اروپا','notes':'سرور اصلی • بررسی روزانه','credential_id':'c1','favorite':True}, {'id':'h2','name':'سرور ایران','host':'198.51.100.20','port':2222,'username':'ubuntu','group':'ایران','notes':'','credential_id':'c1','favorite':False}]
creds=[{'id':'c1','name':'Production identity','kind':'key'}]
requests=[]
def handler(route):
    url=route.request.url
    if 'telegram.org' in url:
        return route.fulfill(body='window.Telegram={WebApp:{ready(){},expand(){}}};',content_type='application/javascript')
    path=url.split('ssh.example.com',1)[1].split('?')[0]
    if path.startswith('/api'):
        requests.append((route.request.method,path))
        if path.endswith('/fingerprint'):
            return route.fulfill(json={'fingerprint':'SHA256:'+'A'*43,'saved':'SHA256:'+'A'*43})
        if path.endswith('/ticket'):
            return route.fulfill(json={'ticket':'ui-fixture-only'})
        data={'/api/me':{'id':1234,'owner':True},'/api/hosts':hosts,'/api/credentials':creds,'/api/users':{'owner':1234,'users':[98765]},'/api/audit':[]}.get(path,{})
        if route.request.method=='POST' and path=='/api/hosts':
            h=route.request.post_data_json;h['id']='h3';hosts.append(h);data=h
        return route.fulfill(json=data)
    file=root/('index.html' if path=='/' else path.removeprefix('/static/'))
    route.fulfill(path=str(file))

with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,args=['--no-sandbox'])
    for width,height in [(1440,1000),(390,844)]:
        page=browser.new_page(viewport={'width':width,'height':height})
        errors=[];page.on('pageerror',lambda e: errors.append(str(e)))
        page.route('**/*',handler)
        page.add_init_script('''window.WebSocket=class {
          static OPEN=1;
          constructor(){this.readyState=1;setTimeout(()=>this.onopen?.(),10);}
          send(value){const m=JSON.parse(value);if(m.ticket){setTimeout(()=>{this.onmessage?.({data:JSON.stringify({ready:true,session_id:'fixture'})});this.onmessage?.({data:new TextEncoder().encode('root@test:~# hello\\r\\n').buffer});},10);}}
          close(){this.readyState=3;this.onclose?.();}
        };''')
        page.goto('https://ssh.example.com/')
        page.locator('#workspace').wait_for(state='visible')
        page.get_by_text('Germany · Production',exact=True).wait_for()
        page.locator('#search').fill('198.51.100')
        assert page.locator('.card').count()==1
        page.locator('#search').fill('')
        page.locator('#add').click()
        page.locator('input[name=name]').fill('New test host')
        page.locator('input[name=host]').fill('203.0.113.42')
        page.locator('select[name=credential_id]').select_option('c1')
        page.locator('#editor button[type=submit]').click()
        page.locator('#dialog').wait_for(state='hidden')
        page.locator('[data-page=credentials]').click()
        page.get_by_role('heading',name='Production identity',exact=True).wait_for()
        page.locator('#add').click()
        assert page.locator('input[name=secret]').get_attribute('type')=='password'
        page.locator('select[name=kind]').select_option('key')
        assert page.locator('textarea[name=secret]').count()==1
        page.locator('#cancel-dialog').click()
        page.locator('[data-page=users]').click()
        page.get_by_text('98765',exact=True).wait_for()
        page.locator('[data-page=hosts]').click()
        page.locator('.connect').first.click()
        page.locator('#terminal-area').wait_for(state='visible')
        page.wait_for_function("document.querySelector('#terminal-status').textContent.startsWith('Connected')")
        page.locator('[data-key]').first.click()
        page.locator('#fullscreen').click()
        assert 'full' in page.locator('#terminal-area').get_attribute('class')
        page.locator('#fullscreen').click()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.screenshot(path=str(Path('/tmp') / f'sshbot-ui-{width}.png'),full_page=True)
        assert not errors,errors
        page.close()
    browser.close()
print('Desktop/mobile: search, host creation, keychain forms, users, overflow: PASS')
