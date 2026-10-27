/* Local keyboard/clipboard preferences. No clipboard text or SSH secrets are persisted. */
'use strict';
window.TerminalControls=(()=>{
    const isMac=/Mac|iPhone|iPad/.test(navigator.platform||'');
    const actions=[
        ['copy','کپی متن انتخاب‌شده',[isMac?'Meta+KeyC':'Ctrl+Shift+KeyC','']],
        ['paste','چسباندن',[isMac?'Meta+KeyV':'Ctrl+KeyV','Ctrl+Shift+KeyV']],
        ['interrupt','قطع دستور (Ctrl+C)',['Ctrl+KeyC','']],
        ['cancelEditor','ارسال Ctrl+X',['Ctrl+KeyX','']],
        ['eof','ارسال Ctrl+D',['Ctrl+KeyD','']],
        ['clearScreen','ارسال Ctrl+L',['Ctrl+KeyL','']],
        ['selectAll','انتخاب کل متن ترمینال',['Ctrl+Shift+KeyA','']],
        ['nextTab','تب بعدی',['Alt+ArrowRight','']],
        ['previousTab','تب قبلی',['Alt+ArrowLeft','']],
        ['closeTab','بستن اتصال فعلی',['Alt+Shift+KeyW','']],
        ['focus','حالت تمرکز ترمینال',['Ctrl+Shift+KeyF','']],
        ['fontUp','بزرگ‌کردن نوشته',['Ctrl+Equal','']],
        ['fontDown','کوچک‌کردن نوشته',['Ctrl+Minus','']],
        ['fontReset','اندازه پیش‌فرض نوشته',['Ctrl+Digit0','']]
    ];
    const defaults=()=>({rightClick:'smart',copyOnSelect:false,smartCtrlC:true,confirmPaste:true,fontSize:14,bindings:Object.fromEntries(actions.map(([id,,keys])=>[id,[...keys]]))});
    const validCode=/^(Key[A-Z]|Digit[0-9]|F([1-9]|1[0-2])|Arrow(Left|Right|Up|Down)|Insert|Delete|Home|End|PageUp|PageDown|Equal|Minus|BracketLeft|BracketRight|Backslash|Semicolon|Quote|Comma|Period|Slash|Backquote|Space|Enter|Tab|Escape|Backspace)$/;
    function combo(e){if(!validCode.test(e.code))return '';return [e.ctrlKey?'Ctrl':'',e.altKey?'Alt':'',e.shiftKey?'Shift':'',e.metaKey?'Meta':'',e.code].filter(Boolean).join('+');}
    function validBinding(value){if(value==='')return true;if(typeof value!=='string')return false;const bits=value.split('+'),code=bits.pop();if(!validCode.test(code)||new Set(bits).size!==bits.length||bits.some(x=>!['Ctrl','Alt','Shift','Meta'].includes(x)))return false;return bits.includes('Ctrl')||bits.includes('Alt')||bits.includes('Meta')||/^F\d+$/.test(code)||(bits.length===1&&bits[0]==='Shift'&&code==='Insert');}
    function label(key){return key?key.replace(/Key([A-Z])/,'$1').replace(/Digit([0-9])/,'$1').replace('Equal','=').replace('Minus','−').replace('ArrowRight','→').replace('ArrowLeft','←').replace(/\+/g,' + '):'بدون میانبر';}
    function create(ctx){
        const $=s=>document.querySelector(s);
        let prefs=defaults(),storageKey='',draft=null,menuSession=null,pasteSession=null;
        const menu=$('#terminal-menu'),settings=$('#terminal-settings'),clipDialog=$('#clipboard-dialog');
        function persist(next){prefs=next;try{localStorage.setItem(storageKey,JSON.stringify(prefs));}catch{ctx.toast('تنظیمات برای همین اجرا اعمال شد؛ ذخیره در این مرورگر ممکن نیست.');}apply();}
        function setUser(uid){
            storageKey='sshbot.terminal.v1.'+uid;prefs=defaults();
            try{
                const data=JSON.parse(localStorage.getItem(storageKey)||'null');
                if(data&&typeof data==='object'){
                    if(['smart','menu','paste'].includes(data.rightClick))prefs.rightClick=data.rightClick;
                    for(const key of ['copyOnSelect','smartCtrlC','confirmPaste'])if(typeof data[key]==='boolean')prefs[key]=data[key];
                    if(Number.isInteger(data.fontSize)&&data.fontSize>=10&&data.fontSize<=28)prefs.fontSize=data.fontSize;
                    const used=new Set();
                    for(const [id] of actions){const keys=data.bindings?.[id];if(Array.isArray(keys)&&keys.length===2&&keys.every(validBinding))prefs.bindings[id]=[...keys];}
                    // Corrupt/conflicting storage never silently captures unrelated keys.
                    for(const [id] of actions)for(const key of prefs.bindings[id]){if(key&&used.has(key))throw Error('duplicate');if(key)used.add(key);}
                }
            }catch{prefs=defaults();}
            apply();
        }
        function apply(){
            for(const s of ctx.getSessions().values()){s.term.options.fontSize=prefs.fontSize;ctx.resize(s);}
            for(const [id] of actions){const keys=prefs.bindings[id].filter(Boolean).map(label).join(' / ');const el=$('#'+id);if(el)el.title=keys;}
        }
        function active(s){if(!s||ctx.getSessions().get(s.id)!==s||!s.ready||!ctx.isVisible()||ctx.getSession()!==s){ctx.toast('اتصال فعال نیست.');return false;}return true;}
        function fallbackClipboard(mode,text,s){
            pasteSession=s;$('#clipboard-title').textContent=mode==='copy'?'کپی متن':'چسباندن دستی';
            $('#clipboard-hint').textContent=mode==='copy'?'دسترسی مستقیم کلیپ‌بورد ممکن نیست. متن را با Ctrl+C یا منوی دستگاه کپی کن.':'دسترسی مستقیم کلیپ‌بورد ممکن نیست. متن را اینجا Paste کن و سپس «ارسال به ترمینال» را بزن.';
            const area=$('#clipboard-text');area.readOnly=mode==='copy';area.value=mode==='copy'?text:'';
            $('#clipboard-apply').hidden=mode==='copy';clipDialog.showModal();area.focus();if(mode==='copy')area.select();
        }
        async function copy(s=ctx.getSession()){
            const text=s?.term.getSelection();if(!text){ctx.toast('اول متن ترمینال را انتخاب کن.');return;}
            try{if(!navigator.clipboard?.writeText)throw Error('unsupported');await navigator.clipboard.writeText(text);ctx.toast('متن انتخاب‌شده کپی شد.');}
            catch{fallbackClipboard('copy',text,s);}
        }
        function pasteText(text,s){
            if(!active(s)||!text)return false;
            if(text.length>60000){ctx.toast('متن برای یک بار چسباندن طولانی است؛ آن را به بخش‌های کوچک‌تر تقسیم کن.');return false;}
            if(prefs.confirmPaste&&/[\r\n\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(text)&&!confirm('این متن چندخطی یا دارای کلید کنترلی است و ممکن است دستور اجرا کند. در ترمینال چسبانده شود؟'))return false;
            // xterm handles CR/LF and bracketed paste according to the remote mode.
            s.term.paste(text);s.term.focus();return true;
        }
        async function paste(s=ctx.getSession()){
            if(!active(s))return;
            try{if(!navigator.clipboard?.readText)throw Error('unsupported');const text=await navigator.clipboard.readText();if(text)pasteText(text,s);else ctx.toast('کلیپ‌بورد خالی است.');}
            catch{fallbackClipboard('paste','',s);}
        }
        function closeMenu(){menu.hidden=true;menuSession=null;}
        function openMenu(e,s){
            menuSession=s;menu.hidden=false;
            $('#context-copy').disabled=!s.term.hasSelection();$('#context-paste').disabled=!s.ready;
            const r=menu.getBoundingClientRect();menu.style.left=Math.max(8,Math.min(e.clientX,innerWidth-r.width-8))+'px';menu.style.top=Math.max(8,Math.min(e.clientY,innerHeight-r.height-8))+'px';$('#context-copy').focus();
        }
        function attach(s){
            s.term.options.fontSize=prefs.fontSize;
            // Capture before xterm/browser context handlers so a right click preserves selection.
            s.el.addEventListener('mousedown',e=>{if(e.button===2&&!e.shiftKey)e.stopImmediatePropagation();},true);
            s.el.addEventListener('contextmenu',e=>{
                if(e.shiftKey)return; // Shift+right-click retains the device menu.
                e.preventDefault();e.stopImmediatePropagation();closeMenu();
                if(prefs.rightClick==='menu')openMenu(e,s);
                else if(prefs.rightClick==='smart'&&s.term.hasSelection())copy(s);
                else paste(s);
            },true);
            s.el.addEventListener('paste',e=>{
                if(!e.clipboardData)return;e.preventDefault();e.stopImmediatePropagation();pasteText(e.clipboardData.getData('text/plain'),s);
            },true);
            let selectionTimer;
            s.term.onSelectionChange(()=>{clearTimeout(selectionTimer);if(prefs.copyOnSelect&&s.term.hasSelection())selectionTimer=setTimeout(()=>{if(prefs.copyOnSelect&&ctx.getSessions().has(s.id)&&s.term.hasSelection()&&!$('dialog[open]'))copy(s);},200);});
        }
        function perform(action,s){
            if(!s)return;
            if(action==='copy')return copy(s);
            if(action==='paste')return paste(s);
            if(action==='interrupt'){
                if(prefs.smartCtrlC&&s.term.hasSelection())return copy(s);
                if(active(s))ctx.send(s,{type:'input',data:'\x03'});
            }else if(['cancelEditor','eof','clearScreen'].includes(action)){
                if(active(s))ctx.send(s,{type:'input',data:{cancelEditor:'\x18',eof:'\x04',clearScreen:'\x0c'}[action]});
            }else if(action==='selectAll')s.term.selectAll();
            else if(action==='nextTab'||action==='previousTab'){
                const ids=[...ctx.getSessions().keys()],i=ids.indexOf(s.id),step=action==='nextTab'?1:-1;if(ids.length)ctx.activate(ids[(i+step+ids.length)%ids.length]);
            }else if(action==='closeTab')ctx.close(s.id);
            else if(action==='focus')ctx.focus();
            else if(action.startsWith('font')){const size=action==='fontReset'?14:Math.min(28,Math.max(10,prefs.fontSize+(action==='fontUp'?1:-1)));persist({...prefs,fontSize:size});}
        }
        function openSettings(){closeMenu();draft=structuredClone(prefs);renderSettings();settings.showModal();}
        function renderSettings(){
            $('#shortcut-error').textContent='';$('#right-click').value=draft.rightClick;$('#copy-on-select').checked=draft.copyOnSelect;$('#smart-ctrl-c').checked=draft.smartCtrlC;$('#confirm-paste').checked=draft.confirmPaste;$('#terminal-font-size').value=draft.fontSize;
            const list=$('#shortcut-list');list.replaceChildren();
            for(const [id,title] of actions){
                const row=document.createElement('div');row.className='shortcut-row';const text=document.createElement('span');text.textContent=title;row.append(text);
                draft.bindings[id].forEach((key,slot)=>{
                    const input=document.createElement('input');input.readOnly=true;input.value=label(key);input.className='shortcut-capture';input.dataset.action=id;input.dataset.slot=slot;input.setAttribute('aria-label',title+(slot?'، میانبر دوم':'، میانبر اول'));input.title='کلیک کن و ترکیب کلید را بزن؛ Backspace برای حذف';
                    input.onkeydown=e=>{
                        if(e.key==='Tab'||e.key==='Escape')return;
                        e.preventDefault();e.stopPropagation();if(e.repeat)return;
                        if((e.code==='Backspace'||e.code==='Delete')&&!e.ctrlKey&&!e.altKey&&!e.metaKey&&!e.shiftKey){draft.bindings[id][slot]='';input.value=label('');return;}
                        const next=combo(e);if(!validBinding(next)||!next)return;
                        if(next==='Meta+KeyQ'||next==='Alt+F4'||next==='Ctrl+Alt+Delete'){$('#shortcut-error').textContent='این ترکیب برای سیستم‌عامل رزرو شده است.';return;}
                        draft.bindings[id][slot]=next;input.value=label(next);$('#shortcut-error').textContent='';
                    };row.append(input);
                });list.append(row);
            }
        }
        document.addEventListener('keydown',e=>{
            if(e.key==='Escape'&&!menu.hidden){e.preventDefault();e.stopImmediatePropagation();closeMenu();ctx.getSession()?.term.focus();return;}
            if($('dialog[open]')||e.isComposing||e.getModifierState?.('AltGraph')||!ctx.isVisible()||!e.target.closest('#terminal-area'))return;
            const key=combo(e);if(!key)return;
            const entry=actions.find(([id])=>prefs.bindings[id].includes(key));if(!entry)return;
            e.preventDefault();e.stopImmediatePropagation();if(!e.repeat)Promise.resolve(perform(entry[0],ctx.getSession())).catch(err=>ctx.toast(err.message));
        },true);
        document.addEventListener('pointerdown',e=>{if(!menu.hidden&&!menu.contains(e.target))closeMenu();});
        window.addEventListener('resize',closeMenu);
        $('#context-copy').onclick=()=>{const s=menuSession;closeMenu();copy(s);};
        $('#context-paste').onclick=()=>{const s=menuSession;closeMenu();paste(s);};
        $('#context-select').onclick=()=>{const s=menuSession;closeMenu();s?.term.selectAll();};
        $('#context-settings').onclick=openSettings;
        $('#terminal-settings-button').onclick=openSettings;
        $('#terminal-settings-close').onclick=()=>settings.close();
        $('#terminal-settings-cancel').onclick=()=>settings.close();
        $('#shortcut-reset').onclick=()=>{draft=defaults();renderSettings();};
        $('#shortcut-form').onsubmit=e=>{
            e.preventDefault();const used=new Map();
            for(const [id,title] of actions)for(const key of draft.bindings[id]){
                if(!key)continue;if(used.has(key)){$('#shortcut-error').textContent='میانبر '+label(key)+' تکراری است: '+used.get(key)+' و '+title;return;}used.set(key,title);
            }
            draft.rightClick=$('#right-click').value;draft.copyOnSelect=$('#copy-on-select').checked;draft.smartCtrlC=$('#smart-ctrl-c').checked;draft.confirmPaste=$('#confirm-paste').checked;draft.fontSize=Number($('#terminal-font-size').value);
            if(!Number.isInteger(draft.fontSize)||draft.fontSize<10||draft.fontSize>28)return;
            persist(draft);settings.close();ctx.getSession()?.term.focus();ctx.toast('تنظیمات ترمینال روی این دستگاه ذخیره شد.');
        };
        $('#clipboard-cancel').onclick=()=>clipDialog.close();
        $('#clipboard-form').onsubmit=e=>{e.preventDefault();if(pasteText($('#clipboard-text').value,pasteSession)){clipDialog.close();pasteSession?.term.focus();}};
        clipDialog.addEventListener('close',()=>{$('#clipboard-text').value='';pasteSession=null;});
        return {setUser,attach,copy,paste,closeMenu,get fontSize(){return prefs.fontSize;}};
    }
    return {create};
})();
