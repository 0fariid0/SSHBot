"""Browser integration checks of real xterm controls with mock SSH and clipboard."""
from playwright.sync_api import sync_playwright
from ui_smoke import handler

with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,args=['--no-sandbox'])
    page=browser.new_page(viewport={'width':1360,'height':900})
    errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    page.route('**/*',handler)
    page.add_init_script('''
      window.__clipboard='echo hello';window.__copied='';window.__denyRead=false;window.__denyWrite=false;
      Object.defineProperty(navigator,'clipboard',{configurable:true,value:{
        async readText(){if(window.__denyRead)throw Error('denied');return window.__clipboard;},
        async writeText(text){if(window.__denyWrite)throw Error('denied');window.__copied=text;}
      }});
      window.__sockets=[];window.WebSocket=class {
        static OPEN=1;
        constructor(){window.__sockets.push(this);this.messages=[];this.readyState=1;setTimeout(()=>this.onopen?.(),10)}
        send(value){const m=JSON.parse(value);this.messages.push(m);if(m.ticket)setTimeout(()=>{
          this.onmessage?.({data:JSON.stringify({ready:true,session_id:'fixture'})});
          this.onmessage?.({data:new TextEncoder().encode('root@test:~# hello\\r\\n').buffer});},10)}
        close(){this.readyState=3;this.onclose?.()}
      };
    ''')
    def inputs():return page.evaluate("window.__sockets[0].messages.filter(m=>m.type==='input').map(m=>m.data)")
    def focus():page.locator('.xterm-helper-textarea').first.focus()
    def select():page.evaluate('sessions.get(current).term.select(0,0,4)')
    page.goto('https://ssh.example.com/')
    page.locator('#workspace').wait_for(state='visible')
    page.locator('.connect').first.click()
    page.wait_for_function("document.querySelector('.tab.ready')")
    page.wait_for_function("sessions.get(current).term.buffer.active.getLine(0).translateToString().includes('root')")

    # Selection + Ctrl+C copies, without sending SIGINT; no selection sends one SIGINT.
    select();focus();before=inputs();page.keyboard.press('Control+c')
    page.wait_for_function("window.__copied==='root'");assert inputs()==before
    page.evaluate('sessions.get(current).term.clearSelection()');focus();page.keyboard.press('Control+c')
    assert inputs()==before+['\x03']
    focus();page.keyboard.press('Control+x');page.keyboard.press('Control+d')
    assert inputs()[-2:]==['\x18','\x04']
    page.locator('[data-key]').nth(1).click();assert inputs()[-1]=='\x18'

    # Right click uses captured selection; without selection it pastes exactly once.
    select();page.evaluate("window.__copied='' ");before=inputs()
    page.locator('.xterm-screen').click(button='right')
    page.wait_for_function("window.__copied==='root'");assert inputs()==before
    page.evaluate('sessions.get(current).term.clearSelection()')
    page.locator('.xterm-screen').click(button='right')
    page.wait_for_function("window.__sockets[0].messages.some(m=>m.data==='echo hello')")
    assert inputs()==before+['echo hello']
    before=inputs();focus();page.keyboard.press('Control+Shift+v')
    page.wait_for_function("window.__sockets[0].messages.filter(m=>m.type==='input').length>"+str(len(before)))
    assert inputs()==before+['echo hello']

    # Native paste follows the same path and xterm bracketed-paste mode is respected.
    page.evaluate("new Promise(resolve=>sessions.get(current).term.write('\\x1b[?2004h',resolve))")
    before=inputs()
    page.evaluate("""(()=>{const data=new DataTransfer();data.setData('text/plain','native paste');document.querySelector('.xterm-helper-textarea').dispatchEvent(new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true}));})()""")
    assert inputs()==before+['\x1b[200~native paste\x1b[201~']

    # Multiline confirmation can cancel without sending any bytes.
    page.evaluate("window.__clipboard='line1\\nline2'")
    before=inputs();page.once('dialog',lambda d:d.dismiss());page.locator('#paste').click()
    assert inputs()==before
    page.once('dialog',lambda d:d.accept());page.locator('#paste').click()
    page.wait_for_function("window.__sockets[0].messages.filter(m=>m.type==='input').length>"+str(len(before)))
    assert len(inputs())==len(before)+1
    assert inputs()[-1]=='\x1b[200~line1\rline2\x1b[201~'
    page.evaluate("window.__clipboard='x'.repeat(60001)")
    before=inputs();page.locator('#paste').click();page.get_by_text('متن برای یک بار چسباندن طولانی است؛ آن را به بخش‌های کوچک‌تر تقسیم کن.').wait_for();assert inputs()==before

    # A clipboard permission denial provides a working manual input dialog.
    page.evaluate('window.__denyRead=true');page.locator('#paste').click()
    page.locator('#clipboard-dialog').wait_for(state='visible')
    before=inputs();page.locator('#clipboard-text').fill('manual paste')
    page.locator('#clipboard-apply').click();page.locator('#clipboard-dialog').wait_for(state='hidden')
    assert inputs()==before+['\x1b[200~manual paste\x1b[201~']
    page.evaluate('window.__denyRead=false;window.__denyWrite=true');select()
    page.locator('#copy').click();page.locator('#clipboard-dialog').wait_for(state='visible')
    assert page.locator('#clipboard-text').input_value()=='root'
    assert page.locator('#clipboard-text').get_attribute('readonly') is not None
    page.locator('#clipboard-cancel').click();page.evaluate('window.__denyWrite=false')

    # Customize and validate conflicting shortcuts without sending them to SSH.
    page.locator('#terminal-settings-button').click()
    field=page.locator('[data-action=copy][data-slot="0"]')
    before=inputs();field.focus();page.keyboard.press('Control+Shift+p')
    assert field.input_value()=='Ctrl + Shift + P';assert inputs()==before
    conflict=page.locator('[data-action=paste][data-slot="0"]');conflict.focus();page.keyboard.press('Control+Shift+p')
    page.locator('#shortcut-form [type=submit]').click()
    assert page.locator('#terminal-settings').is_visible()
    assert 'تکراری' in page.locator('#shortcut-error').text_content()
    conflict.focus();page.keyboard.press('Control+v')
    page.locator('#right-click').select_option('menu')
    page.locator('#terminal-font-size').fill('18')
    page.locator('#shortcut-form [type=submit]').click()
    page.locator('#terminal-settings').wait_for(state='hidden')
    assert page.evaluate('sessions.get(current).term.options.fontSize')==18
    select();focus();page.evaluate("window.__copied='' ");page.keyboard.press('Control+Shift+p')
    page.wait_for_function("window.__copied==='root'")

    # Menu mode, Escape dismissal, and user-specific local persistence.
    page.locator('.xterm-screen').click(button='right');page.locator('#terminal-menu').wait_for(state='visible')
    assert page.locator('#context-copy').is_enabled()
    page.keyboard.press('Escape');assert page.locator('#terminal-menu').is_hidden()
    saved=page.evaluate("JSON.parse(localStorage.getItem('sshbot.terminal.v1.1234'))")
    assert saved['bindings']['copy'][0]=='Ctrl+Shift+KeyP'
    assert saved['rightClick']=='menu'
    assert 'root' not in str(saved) and 'echo hello' not in str(saved)
    page.evaluate('terminalControls.setUser(5678)');assert page.evaluate('terminalControls.fontSize')==14
    page.evaluate('terminalControls.setUser(1234)');assert page.evaluate('terminalControls.fontSize')==18

    print('Checking delayed clipboard target...',flush=True)
    # Pending clipboard access must not paste into a hidden/background terminal.
    page.evaluate("()=>{navigator.clipboard.readText=()=>new Promise(resolve=>window.__resolveClipboard=resolve);}")
    before=inputs();page.locator('#paste').click();page.locator('#workspace-home').click()
    page.evaluate("window.__resolveClipboard('must not reach hidden SSH')")
    page.get_by_text('اتصال فعال نیست.',exact=True).wait_for();assert inputs()==before
    page.evaluate("()=>{navigator.clipboard.readText=async()=>window.__clipboard;}")
    page.locator('.tab-select').first.click()

    print('Checking tab hotkeys...',flush=True)
    # Tab hotkeys preserve independent connections; a focused Ctrl+X still reaches SSH.
    page.locator('#workspace-home').click();page.locator('.connect').nth(1).click()
    page.wait_for_function("document.querySelectorAll('.tab.ready').length===2")
    focus();page.keyboard.press('Alt+ArrowRight')
    page.wait_for_function("document.querySelector('#terminal-name').textContent==='Germany · Production'")
    focus();page.keyboard.press('Control+Shift+f');assert 'full' in page.locator('#terminal-area').get_attribute('class')
    focus();page.keyboard.press('Control+Shift+f');assert 'full' not in (page.locator('#terminal-area').get_attribute('class') or '')
    page.once('dialog',lambda d:d.accept());focus();page.keyboard.press('Alt+Shift+w')
    page.wait_for_function("document.querySelectorAll('.tab').length===1")

    print('Checking reload...',flush=True)
    # Reload keeps bindings and font size. No terminal input leaks into settings forms.
    page.goto('https://ssh.example.com/');page.locator('#workspace').wait_for(state='visible')
    page.locator('.connect').first.click();page.wait_for_function("document.querySelector('.tab.ready')")
    assert page.evaluate('sessions.get(current).term.options.fontSize')==18
    page.locator('#terminal-settings-button').click()
    assert page.locator('[data-action=copy][data-slot="0"]').input_value()=='Ctrl + Shift + P'
    page.screenshot(path='/tmp/sshbot-shortcuts-desktop.png')
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.querySelector("#terminal-settings").scrollWidth<=document.querySelector("#terminal-settings").clientWidth')
    page.screenshot(path='/tmp/sshbot-shortcuts-mobile.png')
    assert not errors,errors
    browser.close()
print('PASS: selection copy/SIGINT, raw controls, smart right-click, single/native/bracketed/multiline paste, paste limits, clipboard-denied fallbacks, custom bindings/conflicts, user-specific persistence/reload, tab/focus/close keys, settings desktop/mobile.')
