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
        return route.fulfill(body='''window.__tgCalls={expand:0,fullscreen:0};window.__tgEvents={};window.Telegram={WebApp:{platform:'tdesktop',isFullscreen:false,safeAreaInset:{top:window.__safeTop||0,bottom:0,left:0,right:0},contentSafeAreaInset:{top:window.__contentTop||0,bottom:0,left:0,right:0},ready(){},expand(){window.__tgCalls.expand++},isVersionAtLeast(){return window.__tgMode!=='legacy'},requestFullscreen(){window.__tgCalls.fullscreen++;this.isFullscreen=true;window.__tgEvents.fullscreenChanged?.()},onEvent(name,fn){window.__tgEvents[name]=fn}}};''',content_type='application/javascript')
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
    for width,height in [(1440,1000),(500,760),(390,844),(844,390)]:
        page=browser.new_page(viewport={'width':width,'height':height})
        errors=[];page.on('pageerror',lambda e: errors.append(str(e)))
        page.route('**/*',handler)
        page.add_init_script('''window.__sockets=[];window.WebSocket=class {
          static OPEN=1;
          constructor(){window.__sockets.push(this);this.messages=[];this.readyState=1;setTimeout(()=>this.onopen?.(),10);}
          send(value){const m=JSON.parse(value);this.messages.push(m);if(m.ticket){setTimeout(()=>{this.onmessage?.({data:JSON.stringify({ready:true,session_id:'fixture'})});this.onmessage?.({data:new TextEncoder().encode('root@test:~# hello\\r\\n').buffer});},10);}}
          close(){this.readyState=3;this.onclose?.();}
        };''')
        page.add_init_script('window.__safeTop=20;window.__contentTop=28;' if width==390 else 'window.__safeTop=0;')
        page.goto('https://ssh.example.com/')
        page.locator('#workspace').wait_for(state='visible')
        page.get_by_role('heading',name='Germany · Production',exact=True).wait_for()
        assert page.evaluate('window.__tgCalls.fullscreen')==1
        assert page.locator('#terminal-area').is_hidden()
        if width==390:
            assert page.locator('#workspace').evaluate('(el)=>getComputedStyle(el).paddingTop')=='48px'
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
        if width==390:
            page.evaluate("window.Telegram.WebApp.viewportHeight=500;window.__tgEvents.viewportChanged({isStateStable:true})")
            page.wait_for_function("document.querySelector('#workspace').getBoundingClientRect().height===500")
            assert page.locator('#terminals').bounding_box()['height']>120
            assert page.evaluate('window.__sockets[0].readyState')==1
            page.evaluate("window.Telegram.WebApp.viewportHeight=0;window.__tgEvents.viewportChanged({isStateStable:true})")
            page.wait_for_function('document.querySelector("#workspace").getBoundingClientRect().height===innerHeight')
        page.locator('[data-key]').first.click()
        assert page.evaluate("window.__sockets[0].messages.some(m=>m.type==='input')")
        # Terminal uses the available height, instead of a short embedded pane.
        term_height=page.locator('#terminals').bounding_box()['height']
        assert term_height>height*.38,(width,height,term_height)
        page.screenshot(path=str(Path('/tmp') / f'sshbot-terminal-{width}.png'))
        page.locator('#fullscreen').click()
        assert 'full' in page.locator('#terminal-area').get_attribute('class')
        page.locator('#hide-terminal').click()
        assert page.locator('#terminal-area').is_hidden()
        assert page.locator('#page-body').is_visible()
        assert page.evaluate('window.__sockets[0].readyState')==1
        page.screenshot(path=str(Path('/tmp') / f'sshbot-ui-{width}.png'))
        # Each other section must hide the terminal without closing its socket.
        for section in ['credentials','users','audit','hosts']:
            page.locator('[data-page='+section+']').click()
            assert page.locator('#terminal-area').is_hidden()
            assert page.evaluate('window.__sockets[0].readyState')==1
        page.locator('[data-page=terminals]').click()
        assert page.locator('#terminal-area').is_visible()
        page.locator('#workspace-home').click()
        page.locator('.connect').nth(1).click()
        page.wait_for_function("window.__sockets.length===2 && document.querySelectorAll('.tab.ready').length===2")
        page.locator('#workspace-home').click()
        assert page.locator('#terminal-area').is_hidden()
        page.locator('.tab-select').first.click()
        assert page.locator('#terminal-area').is_visible()
        assert page.locator('#terminal-name').text_content()=='Germany · Production'
        # Close a background tab from Hosts; this must not switch pages.
        page.locator('#workspace-home').click()
        page.on('dialog',lambda dialog:dialog.accept())
        page.locator('.tab-close').last.click()
        assert page.locator('#terminal-area').is_hidden()
        assert page.locator('[data-page=hosts]').get_attribute('class')=='selected'
        assert page.locator('.tab').count()==1
        assert page.evaluate('window.__sockets[0].readyState')==1
        page.locator('.tab-select').click()
        page.locator('.tab-close').click()
        assert page.locator('#terminal-area').is_hidden()
        page.get_by_role('heading',name='هنوز ترمینالی باز نیست').wait_for()
        assert page.locator('#session-count').text_content()=='0'
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert page.evaluate('document.documentElement.scrollHeight<=innerHeight')
        assert not errors,errors
        page.close()
    # Legacy and unsupported clients remain usable; no login failure.
    for mode in ['legacy','unsupported']:
        page=browser.new_page(viewport={'width':500,'height':760})
        page.route('**/*',handler)
        page.add_init_script('window.__tgMode='+json.dumps(mode))
        page.goto('https://ssh.example.com/')
        page.locator('#workspace').wait_for(state='visible')
        assert page.evaluate('window.__tgCalls.expand')==1
        if mode=='legacy':
            assert page.evaluate('window.__tgCalls.fullscreen')==0
            page.locator('#app-fullscreen').click()
        else:
            page.evaluate("window.__tgEvents.fullscreenFailed({error:'UNSUPPORTED'})")
        page.locator('#toast').wait_for(state='visible')
        assert page.locator('#workspace').is_visible()
        page.close()
    browser.close()
print('PASS: Desktop, compact Telegram window, mobile safe areas, landscape; isolated terminals and retained sockets; tab switch/close; fullscreen request and unsupported/legacy fallback; forms and search.')
