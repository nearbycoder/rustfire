document.addEventListener('trix-file-accept',event=>event.preventDefault());
const csrfToken=document.querySelector('meta[name="csrf-token"]')?.content||'';
const decodeAutocompleteName=value=>{const textarea=document.createElement('textarea');textarea.innerHTML=value;return textarea.value;};
const formatLocalTimes=(root=document)=>root.querySelectorAll('[data-local-datetime]').forEach(node=>{const date=new Date(node.dateTime);if(Number.isNaN(date.getTime()))return;node.textContent=new Intl.DateTimeFormat(undefined,{dateStyle:'short',timeStyle:'short'}).format(date);node.title=new Intl.DateTimeFormat(undefined,{dateStyle:'short',timeStyle:'short'}).format(date)});
formatLocalTimes();
const pingForm=document.getElementById('ping-form');
if(pingForm){
  const input=document.getElementById('ping-search');
  const selectedBox=document.getElementById('ping-selected');
  const suggestionBox=document.getElementById('ping-suggestions');
  const selected=new Map();
  let options=[],active=0,generation=0,timer;
  const hide=()=>{options=[];suggestionBox.replaceChildren();suggestionBox.hidden=true;input.setAttribute('aria-expanded','false');input.removeAttribute('aria-activedescendant');};
  const markActive=()=>{[...suggestionBox.children].forEach((button,index)=>button.setAttribute('aria-selected',String(index===active)));input.setAttribute('aria-activedescendant',`ping-option-${active}`);};
  const renderSelected=()=>{
    selectedBox.replaceChildren();
    for(const [id,name] of selected){
      const pill=document.createElement('span');pill.className='ping-pill';
      const hidden=document.createElement('input');hidden.type='hidden';hidden.name='user_ids[]';hidden.value=String(id);
      const label=document.createElement('span');label.textContent=name;
      const remove=document.createElement('button');remove.type='button';remove.textContent='×';remove.setAttribute('aria-label',`Remove ${name}`);remove.addEventListener('click',()=>{selected.delete(id);renderSelected();input.focus();});
      pill.append(hidden,label,remove);selectedBox.append(pill);
    }
    input.required=!selected.size;
  };
  const choose=person=>{selected.set(person.value,person.name);renderSelected();input.value='';input.setCustomValidity('');generation++;clearTimeout(timer);hide();input.focus();};
  const search=()=>{
    const current=++generation;clearTimeout(timer);input.setCustomValidity('');
    timer=setTimeout(async()=>{
      try{
        const response=await fetch(`/autocompletable/users?query=${encodeURIComponent(input.value.trim())}`);
        if(!response.ok||current!==generation)return;
        const people=(await response.json()).map(person=>({...person,name:decodeAutocompleteName(person.name)}));if(current!==generation)return;
        options=people.filter(person=>!selected.has(person.value));active=0;suggestionBox.replaceChildren();
        for(const [index,person] of options.entries()){
          const button=document.createElement('button');button.type='button';button.id=`ping-option-${index}`;button.setAttribute('role','option');button.className='ping-suggestion';
          const avatar=document.createElement('img');avatar.src=person.avatar_url;avatar.alt='';
          const label=document.createElement('span');label.textContent=person.name;button.append(avatar,label);
          button.addEventListener('mousedown',event=>event.preventDefault());button.addEventListener('click',()=>choose(person));suggestionBox.append(button);
        }
        suggestionBox.hidden=!options.length;input.setAttribute('aria-expanded',String(!!options.length));if(options.length)markActive();
      }catch{hide()}
    },120);
  };
  input.addEventListener('focus',search);
  input.addEventListener('input',search);
  input.addEventListener('keydown',event=>{
    if(event.key==='Escape'){generation++;clearTimeout(timer);hide();return;}
    if(event.key==='Backspace'&&!input.value&&selected.size){selected.delete([...selected.keys()].at(-1));renderSelected();return;}
    if(suggestionBox.hidden||!options.length)return;
    if(event.key==='ArrowDown'||event.key==='ArrowUp'){event.preventDefault();active=(active+(event.key==='ArrowDown'?1:-1)+options.length)%options.length;markActive();}
    if(event.key==='Enter'||event.key==='Tab'){event.preventDefault();choose(options[active]);}
  });
  input.addEventListener('blur',()=>setTimeout(()=>{if(!suggestionBox.contains(document.activeElement))hide();},150));
  pingForm.addEventListener('submit',event=>{if(!selected.size){event.preventDefault();input.setCustomValidity('Choose a person from the list');input.reportValidity();input.focus();}});
}
const chat = document.querySelector('.chat');
if (chat) {
  const roomId = Number(chat.dataset.roomId);
  const messages = chat.querySelector('.messages');
  const decorateOwn=()=>{
    messages.querySelectorAll('.message').forEach(node=>node.classList.toggle('own',node.dataset.creatorId===document.body.dataset.userId));
    messages.querySelectorAll('.boost-item').forEach(node=>node.classList.toggle('mine',node.dataset.boosterId===document.body.dataset.userId));
  };
  const formatMessageGroups=()=>{
    messages.querySelectorAll('.day-separator').forEach(node=>node.remove());
    let previous=null,previousDay=null;
    for(const message of messages.querySelectorAll('.message')){
      const time=new Date(message.querySelector('time')?.dateTime||'').getTime();
      const priorTime=previous?new Date(previous.querySelector('time')?.dateTime||'').getTime():NaN;
      message.classList.toggle('threaded',!!previous&&message.dataset.creatorId===previous.dataset.creatorId&&Number.isFinite(time)&&Number.isFinite(priorTime)&&Math.abs(time-priorTime)<=300000);
      if(Number.isFinite(time)){
        const date=new Date(time);
        const day=`${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}`;
        if(day!==previousDay){
          const separator=document.createElement('div');separator.className='day-separator';
          const label=document.createElement('time');label.dateTime=day;label.textContent=new Intl.DateTimeFormat(undefined,{dateStyle:'long'}).format(date);
          separator.append(label);message.before(separator);
        }
        previousDay=day;
      }
      previous=message;
    }
  };
  decorateOwn();
  formatMessageGroups();
  let historyMode=chat.dataset.historyMode==='true';
  const atMessage=chat.dataset.atMessage;
  if(atMessage)document.getElementById(`message-${atMessage}`)?.scrollIntoView({block:'center'});
  else messages.scrollTop=messages.scrollHeight;
  const returnButton=document.createElement('button');
  returnButton.type='button';returnButton.className='return-to-latest';returnButton.textContent='↓ Latest messages';returnButton.hidden=true;
  chat.append(returnButton);
  const nearBottom=()=>messages.scrollHeight-messages.scrollTop-messages.clientHeight<150;
  const updateReturnButton=()=>{returnButton.hidden=!historyMode&&nearBottom()};
  returnButton.addEventListener('click',()=>{if(historyMode)location.href=`/rooms/${roomId}`;else{messages.scrollTop=messages.scrollHeight;returnButton.hidden=true}});
  updateReturnButton();
  let historyLoading=false,historyDone=false;
  async function loadOlder(){
    if(historyLoading||historyDone)return;
    const first=messages.querySelector('.message[data-message-id]');
    if(!first){historyDone=true;return;}
    historyLoading=true;
    try{
      const response=await fetch(`/rooms/${roomId}/messages?before=${first.dataset.messageId}`);
      if(response.status===204){historyDone=true;return;}
      if(!response.ok)throw Error(`History returned ${response.status}`);
      const html=await response.text();
      if(!html.trim()){historyDone=true;return;}
      const previousHeight=messages.scrollHeight,previousTop=messages.scrollTop;
      messages.insertAdjacentHTML('afterbegin',html);
      formatLocalTimes(messages);decorateOwn();formatMessageGroups();
      messages.scrollTop=previousTop+messages.scrollHeight-previousHeight;
    }catch(error){console.error('Could not load older messages',error)}
    finally{historyLoading=false}
  }
  let cursor = Math.max(0, ...Array.from(messages.querySelectorAll('[data-message-id]'), node => Number(node.dataset.messageId)));
  let lastRefreshAt=Number(chat.dataset.refreshSince)||0;
  let newerLoading=false;
  async function loadNewer(){
    if(newerLoading||!historyMode)return;
    const last=Array.from(messages.querySelectorAll('.message[data-message-id]')).at(-1);
    if(!last)return;
    newerLoading=true;
    try{
      const response=await fetch(`/rooms/${roomId}/messages?after=${last.dataset.messageId}`);
      if(response.status===204){historyMode=false;updateReturnButton();catchUp();return;}
      if(!response.ok)throw Error(`Newer history returned ${response.status}`);
      const html=await response.text();
      if(!html.trim()){historyMode=false;updateReturnButton();catchUp();return;}
      messages.insertAdjacentHTML('beforeend',html);
      formatLocalTimes(messages);decorateOwn();formatMessageGroups();
      const newest=Array.from(messages.querySelectorAll('.message[data-message-id]')).at(-1);
      if(newest)cursor=Math.max(cursor,Number(newest.dataset.messageId));
      updateReturnButton();
    }catch(error){console.error('Could not load newer messages',error)}
    finally{newerLoading=false}
  }
  messages.addEventListener('scroll',()=>{updateReturnButton();if(messages.scrollTop<240)loadOlder();else if(historyMode&&nearBottom())loadNewer()},{passive:true});
  let catchingUp = false;
  async function catchUp() {
    if (catchingUp||historyMode) return;
    catchingUp = true;
    const wasNearBottom=nearBottom();
    try {
      for (;;) {
        const response = await fetch(`/rooms/${roomId}/refresh?after=${cursor}`, {headers:{Accept:'application/json'}});
        if (!response.ok) break;
        const page = await response.json();
        for (const entry of page.messages) {
          if (!document.getElementById(`message-${entry.id}`)) messages.insertAdjacentHTML('beforeend', entry.html);
        }
        formatLocalTimes(messages);
        cursor = page.next_after;
        decorateOwn();
        formatMessageGroups();
        if (!page.has_more || !page.messages.length) break;
      }
      const refreshed=await fetch(`/rooms/${roomId}/refresh?since=${lastRefreshAt}`,{headers:{Accept:'application/json'}});
      if(refreshed.ok){
        const page=await refreshed.json();
        for(const entry of page.updated||[]){
          const existing=document.getElementById(`message-${entry.id}`);
          if(existing&&!existing.querySelector('.inline-edit'))(existing.closest('[data-stream-message]')||existing).outerHTML=entry.html;
        }
        lastRefreshAt=page.checked_at;
        formatLocalTimes(messages);decorateOwn();formatMessageGroups();
      }
      if(wasNearBottom)messages.scrollTop=messages.scrollHeight;
      updateReturnButton();
    } catch (error) { console.error('Could not refresh room', error); }
    finally { catchingUp = false; }
  }
  window.addEventListener('online',()=>catchUp());
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)catchUp()});
  let socket;
  const signedStreamName=chat.querySelector('turbo-cable-stream-source[channel="RoomMessagesChannel"]')?.getAttribute('signed-stream-name');
  const messageIdent=JSON.stringify({channel:'RoomMessagesChannel',signed_stream_name:signedStreamName});
  const typingIdent=JSON.stringify({channel:'TypingNotificationsChannel',room_id:roomId});
  const presenceIdent=JSON.stringify({channel:'PresenceChannel',room_id:roomId});
  const unreadIdent=JSON.stringify({channel:'UnreadRoomsChannel'});
  const readIdent=JSON.stringify({channel:'ReadRoomsChannel'});
  const roomListIdent=JSON.stringify({channel:'RoomListChannel'});
  const markRoom=(rid,unread)=>document.querySelectorAll(`.sidebar .room-link[href='/rooms/${rid}']`).forEach(link=>{link.classList.toggle('unread',unread);if(unread&&link.parentElement?.id==='direct-rooms')link.parentElement.prepend(link)});
  let sidebarRefreshPending=false,sidebarRefreshAgain=false;
  async function refreshSidebar(){
    if(sidebarRefreshPending){sidebarRefreshAgain=true;return;}
    sidebarRefreshPending=true;
    try{
      const response=await fetch(`/users/me/sidebar?active=${roomId}`);
      if(!response.ok)return;
      const html=await response.text();
      const replacement=new DOMParser().parseFromString(html,'text/html').querySelector('.sidebar');
      const current=document.querySelector('.sidebar');
      if(!replacement||!current)return;
      replacement.classList.toggle('open',current.classList.contains('open'));
      current.replaceWith(replacement);
      if(response.headers.get('x-rustfire-active-room-accessible')==='0')location.href='/';
    }catch(error){console.error('Could not refresh room list',error)}
    finally{sidebarRefreshPending=false;if(sidebarRefreshAgain){sidebarRefreshAgain=false;refreshSidebar()}}
  }
  function updateDirectRoom(data){
    const nav=document.getElementById('direct-rooms');
    if(!nav||!Number.isInteger(data?.room_id)||typeof data.html!=='string')return false;
    const link=new DOMParser().parseFromString(data.html,'text/html').querySelector('.room-link');
    if(!link||link.getAttribute('href')!==`/rooms/${data.room_id}`)return false;
    link.classList.toggle('active',data.room_id===roomId);
    nav.querySelector(`.room-link[href='/rooms/${data.room_id}']`)?.remove();
    nav.prepend(link);
    if(sidebarRefreshPending)sidebarRefreshAgain=true;
    return true;
  }
  const typingIndicator=document.getElementById('typing-indicator');
  const typingPeople=new Map();
  const renderTyping=()=>{const names=[...typingPeople.values()].map(person=>person.name);typingIndicator.hidden=!names.length;typingIndicator.textContent=names.length===1?`${names[0]} is typing…`:names.length?`${names.join(', ')} are typing…`:'';};
  function typingFrame(data) {
    if(!data?.user || data.user.id===Number(document.body.dataset.userId))return;
    const prior=typingPeople.get(data.user.id);
    if(prior)clearTimeout(prior.timer);
    if(data.action==='start')typingPeople.set(data.user.id,{name:data.user.name,timer:setTimeout(()=>{typingPeople.delete(data.user.id);renderTyping();},6000)});
    else typingPeople.delete(data.user.id);
    renderTyping();
  }
  const sendTyping=action=>{if(socket?.readyState===WebSocket.OPEN)socket.send(JSON.stringify({command:'message',identifier:typingIdent,data:JSON.stringify({action})}));};
  const sendPresence=action=>{if(socket?.readyState===WebSocket.OPEN)socket.send(JSON.stringify({command:'message',identifier:presenceIdent,data:JSON.stringify({action})}));};
  setInterval(()=>{if(!document.hidden)sendPresence('refresh');},50000);
  let visibilityTimer;
  document.addEventListener('visibilitychange',()=>{clearTimeout(visibilityTimer);visibilityTimer=setTimeout(()=>sendPresence(document.hidden?'absent':'present'),5000);});
  function applyRoomStream(html){
    const streamDocument=new DOMParser().parseFromString(html,'text/html');
    for(const stream of streamDocument.querySelectorAll('turbo-stream')){
      const targetId=stream.getAttribute('target');
      const target=targetId&&document.getElementById(targetId);
      if(!target||(target!==messages&&!messages.contains(target)))continue;
      const action=stream.getAttribute('action');
      if(action==='remove'){
        if(target!==messages){target.remove();formatMessageGroups();}
        continue;
      }
      const fragment=stream.querySelector('template')?.content.cloneNode(true);
      if(!fragment)continue;
      if(action==='append'){
        if(target===messages&&historyMode){updateReturnButton();continue;}
        for(const node of [...fragment.children])if(node.id&&document.getElementById(node.id))node.remove();
        if(!fragment.childNodes.length)continue;
        const scrollToLatest=target===messages&&(nearBottom()||[...fragment.querySelectorAll('.message')].some(node=>node.dataset.creatorId===document.body.dataset.userId));
        const addedMessages=[...fragment.querySelectorAll('.message[data-message-id]')];
        target.append(fragment);
        if(target===messages){
          formatLocalTimes(messages);decorateOwn();formatMessageGroups();
          if(!catchingUp)for(const node of addedMessages)cursor=Math.max(cursor,Number(node.dataset.messageId));
          if(scrollToLatest)messages.scrollTop=messages.scrollHeight;
          updateReturnButton();
          for(const node of addedMessages){const sound=node.querySelector('[data-sound]');if(sound)new Audio(sound.dataset.sound).play().catch(()=>{});}
        }else decorateOwn();
      }else if(action==='replace'&&target!==messages){
        if(target.closest('.inline-edit'))continue;
        target.replaceWith(fragment);
        formatLocalTimes(messages);decorateOwn();formatMessageGroups();
      }
    }
  }
  function connect() {
    socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/cable`, 'actioncable-v1-json');
    socket.addEventListener('open', () => { catchingUp = false; for(const identifier of [messageIdent,typingIdent,presenceIdent,unreadIdent,readIdent,roomListIdent])socket.send(JSON.stringify({command:'subscribe',identifier})); });
    socket.addEventListener('message', e => {
      try { const frame = JSON.parse(e.data); if (frame.type === 'confirm_subscription') { if(frame.identifier===messageIdent)catchUp(); if(frame.identifier===presenceIdent&&document.hidden)sendPresence('absent'); if(frame.identifier===roomListIdent)refreshSidebar(); return; } const data = frame.message; if(frame.identifier===messageIdent){if(typeof data==='string')applyRoomStream(data);return;} if(frame.identifier===typingIdent){typingFrame(data);return;} if(frame.identifier===unreadIdent){markRoom(data?.roomId,true);return;} if(frame.identifier===readIdent){markRoom(data?.room_id,false);return;} if(frame.identifier===roomListIdent){if(data?.type!=='direct_room_added'||!updateDirectRoom(data))refreshSidebar();return;}
      } catch {}
    });
    socket.addEventListener('close', () => {for(const person of typingPeople.values())clearTimeout(person.timer);typingPeople.clear();renderTyping();setTimeout(connect, 1500);});
  }
  connect();
  document.addEventListener('click',event=>{if(event.target.closest('[data-toggle-sidebar]'))document.querySelector('.sidebar')?.classList.toggle('open')});
  const composer=document.getElementById('composer');
  async function restoreMessage(article){
    const response=await fetch(`/rooms/${roomId}/messages/${article.dataset.messageId}`,{headers:{'X-Rustfire-Fragment':'1'}});
    if(!response.ok)throw Error('Could not load message');
    (article.closest('[data-stream-message]')||article).outerHTML=await response.text();
    formatLocalTimes(messages);decorateOwn();formatMessageGroups();
  }
  const typingInput=composer.querySelector('trix-editor');
  const bodyInput=composer.querySelector('[name="message[body]"]');
  const mentionBox=document.getElementById('mention-suggestions');
  let mentionOptions=[],mentionRange=null,mentionSelected=0,mentionGeneration=0,mentionTimer,ignoreNextMentionChange=false;
  const hideMentions=()=>{mentionGeneration++;clearTimeout(mentionTimer);mentionOptions=[];mentionRange=null;mentionBox.hidden=true;mentionBox.replaceChildren();typingInput.removeAttribute('aria-activedescendant');};
  const markMention=()=>{[...mentionBox.children].forEach((button,index)=>{button.classList.toggle('selected',index===mentionSelected);button.setAttribute('aria-selected',String(index===mentionSelected));});typingInput.setAttribute('aria-activedescendant',`mention-option-${mentionSelected}`);};
  const chooseMention=(person)=>{if(!mentionRange||!typingInput.editor||typeof person.sgid!=='string')return;const span=document.createElement('span');span.className='mention';span.setAttribute('sgid',person.sgid);const avatar=document.createElement('img');avatar.src=person.avatar_url;avatar.className='avatar';avatar.alt=person.name;span.append(avatar,document.createTextNode(person.name));typingInput.editor.setSelectedRange(mentionRange);typingInput.editor.insertAttachment(new Trix.Attachment({content:span.outerHTML,contentType:'application/vnd.campfire.mention',sgid:person.sgid}));typingInput.editor.insertString(' ');ignoreNextMentionChange=true;hideMentions();typingInput.focus();};
  const refreshMentions=()=>{if(ignoreNextMentionChange){ignoreNextMentionChange=false;return;}const editor=typingInput.editor;if(!editor)return;const position=editor.getPosition();const before=editor.getDocument().toString().slice(0,position);const match=before.match(/(?:^|\s)@([^@\n]{0,32})$/);if(!match){hideMentions();return;}const query=match[1].trim();mentionRange=[position-match[1].length-1,position];const generation=++mentionGeneration;clearTimeout(mentionTimer);mentionTimer=setTimeout(async()=>{try{const response=await fetch(`/autocompletable/users?room_id=${roomId}&query=${encodeURIComponent(query)}`);if(!response.ok||generation!==mentionGeneration)return;const people=(await response.json()).map(person=>({...person,name:decodeAutocompleteName(person.name)}));if(generation!==mentionGeneration)return;mentionOptions=people.filter(person=>person.value!==Number(document.body.dataset.userId));mentionSelected=0;mentionBox.replaceChildren();for(const [index,person] of mentionOptions.entries()){const button=document.createElement('button');button.type='button';button.id=`mention-option-${index}`;button.setAttribute('role','option');button.textContent=person.name;button.addEventListener('mousedown',event=>event.preventDefault());button.addEventListener('click',()=>chooseMention(person));mentionBox.append(button);}mentionBox.hidden=!mentionOptions.length;if(mentionOptions.length)markMention();}catch{hideMentions();}},120);};
  typingInput.addEventListener('trix-change',refreshMentions);
  typingInput.addEventListener('trix-paste',async(event)=>{
    const range=event.paste?.range;
    const editor=typingInput.editor;
    if(!range||!editor)return;
    const url=editor.getDocument().getStringAtRange(range).trim();
    if(!/^(?:https?:\/\/|www\.)\S+$/i.test(url))return;
    try {
      const response=await fetch('/unfurl_link',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify({url})});
      if(!response.ok||response.status===204)return;
      const {title,description,image,url:canonical}=await response.json();
      if(!editor.getDocument().toString().includes(url))return;
      const wrapper=document.createElement('div');wrapper.className='og-embed';
      const link=document.createElement('a');link.href=canonical;link.rel='noreferrer';link.target='_blank';link.textContent=title;wrapper.append(link);
      const text=document.createElement('p');text.textContent=description;wrapper.append(text);
      if(image){const img=document.createElement('img');img.src=image;img.alt='';wrapper.append(img)}
      const prior=editor.getSelectedRange();
      editor.recordUndoEntry('Insert link preview');
      editor.insertAttachment(new Trix.Attachment({contentType:'application/vnd.actiontext.opengraph-embed',content:wrapper.outerHTML,filename:title,href:canonical,url:image,caption:description}));
      editor.setSelectedRange(prior);
    } catch(error) { console.debug('Link preview unavailable',error); }
  });
  typingInput.addEventListener('keydown',event=>{if(mentionBox.hidden)return;if(event.key==='Escape'){event.preventDefault();hideMentions();return;}if(event.key==='ArrowDown'||event.key==='ArrowUp'){event.preventDefault();mentionSelected=(mentionSelected+(event.key==='ArrowDown'?1:-1)+mentionOptions.length)%mentionOptions.length;markMention();return;}if(event.key==='Enter'||event.key==='Tab'){event.preventDefault();chooseMention(mentionOptions[mentionSelected]);}},true);
  typingInput.addEventListener('blur',()=>setTimeout(()=>{if(!mentionBox.contains(document.activeElement))hideMentions();},150));
  const richToggle=document.getElementById('rich-toggle');
  richToggle.addEventListener('click',()=>{const opened=composer.classList.toggle('rich-open');richToggle.setAttribute('aria-expanded',String(opened));typingInput.focus();});
  let typingTimer;
  let lastTypingSent=0;
  typingInput.addEventListener('trix-change',()=>{clearTimeout(typingTimer);if(typingInput.editor?.getDocument().toString().trim()){if(Date.now()-lastTypingSent>750){sendTyping('start');lastTypingSent=Date.now();}typingTimer=setTimeout(()=>sendTyping('stop'),2500);}else sendTyping('stop');});
  typingInput.addEventListener('blur',()=>{clearTimeout(typingTimer);sendTyping('stop');});
  composer.addEventListener('submit', async e => {
    e.preventDefault(); const form=e.currentTarget; const file=form.querySelector('input[type=file]'); if (!typingInput.editor?.getDocument().toString().trim() && !file.files.length) return;
    clearTimeout(typingTimer);sendTyping('stop');
    const res=await fetch(form.action,{method:'POST',body:new FormData(form),headers:{Accept:'application/json','X-CSRF-Token':csrfToken}});
    if (res.ok) { bodyInput.value=''; typingInput.editor.loadHTML(''); file.value=''; form.querySelector('[name="message[client_message_id]"]').value=crypto.randomUUID(); if(historyMode)location.href=`/rooms/${roomId}`; } else alert('Could not send message');
  });
  function revealBoost(node){
    const boost=node.closest('.boost-item');
    if(!boost?.classList.contains('mine'))return;
    const shown=boost.classList.toggle('revealed');
    boost.querySelector('.boost-delete').hidden=!shown;
    node.setAttribute('aria-expanded',String(shown));
  }
  messages.addEventListener('keydown',event=>{
    const content=event.target.closest('[data-boost-reveal]');
    if(content&&(event.key==='Enter'||event.key===' ')){event.preventDefault();revealBoost(content);}
  });
  messages.addEventListener('click', async e => {
    const boostDelete=e.target.closest('[data-delete-boost]');
    if(boostDelete){
      const response=await fetch(boostDelete.dataset.deleteBoost,{method:'DELETE',headers:{'X-CSRF-Token':csrfToken,Accept:'text/vnd.turbo-stream.html'}});
      if(response.ok)boostDelete.closest('.boost-item')?.remove();
      else alert('Could not delete boost');
      return;
    }
    const boostReveal=e.target.closest('[data-boost-reveal]');
    if(boostReveal){revealBoost(boostReveal);return;}
    const edit=e.target.closest('[data-edit-message]');
    if(edit){
      e.preventDefault();const article=edit.closest('.message');
      const response=await fetch(edit.href,{headers:{'X-Rustfire-Inline':'1'}});
      if(!response.ok){alert('Could not edit message');return;}
      article.querySelector('.message-body').innerHTML=await response.text();
      article.classList.add('editing');edit.closest('details').open=false;
      article.querySelector('.inline-edit trix-editor')?.focus();return;
    }
    const cancel=e.target.closest('[data-cancel-edit]');
    if(cancel){try{await restoreMessage(cancel.closest('.message'))}catch{alert('Could not restore message')}return;}
    const remove=e.target.closest('[data-delete-message]');
    if(remove){
      if(!confirm('Delete this message?'))return;
      const response=await fetch(remove.dataset.deleteMessage,{method:'POST',headers:{'X-CSRF-Token':csrfToken}});
      if(response.ok){const article=remove.closest('.message');(article.closest('[data-stream-message]')||article).remove();formatMessageGroups()}else alert('Could not delete message');return;
    }
    const customBoost=e.target.closest('[data-custom-boost]');
    if(customBoost){const form=customBoost.nextElementSibling;form.hidden=false;form.querySelector('input').focus();return;}
    const lightbox=e.target.closest('[data-lightbox]');
    if(lightbox){e.preventDefault();let dialog=document.querySelector('.image-lightbox');if(!dialog){dialog=document.createElement('dialog');dialog.className='image-lightbox';dialog.innerHTML='<button type="button" aria-label="Close image">×</button><img alt="">';dialog.querySelector('button').addEventListener('click',()=>dialog.close());dialog.addEventListener('click',event=>{if(event.target===dialog)dialog.close()});document.body.append(dialog)}dialog.querySelector('img').src=lightbox.href;dialog.showModal();return;}
    const sound=e.target.closest('[data-sound]'); if(sound) { new Audio(sound.dataset.sound).play().catch(()=>{}); return; }
    const reply=e.target.closest('[data-reply]');
    if(reply){
      const article=reply.closest('.message');const body=article.querySelector('.message-body').cloneNode(true);
      const preview=body.querySelector('.og-embed a')?.href;
      body.querySelectorAll('.og-embed').forEach(node=>node.remove());
      body.querySelectorAll('.mention').forEach(node=>node.replaceWith(document.createTextNode(node.textContent.trim())));
      const quoted=body.querySelector('.trix-content')?.innerHTML||body.querySelector('[data-message-presentation]')?.innerHTML||body.innerHTML||preview||'';
      const block=document.createElement('blockquote');block.innerHTML=quoted;
      const cite=document.createElement('cite');cite.textContent=article.querySelector('.message-meta strong')?.textContent+' ';
      const link=document.createElement('a');link.href=article.querySelector('.message-meta a')?.href||'#';link.textContent='#';cite.append(link);
      typingInput.editor.loadHTML(block.outerHTML+cite.outerHTML+'<br>');typingInput.focus();reply.closest('details').open=false;return;
    }
    const copy=e.target.closest('[data-copy-link]');
    if(copy){try{await navigator.clipboard.writeText(new URL(copy.dataset.copyLink,location.origin).href);copy.textContent='Copied';setTimeout(()=>copy.textContent='Copy link',1500);}catch{}return;}
    const button=e.target.closest('[data-boost]'); if(!button) return;
    const res=await fetch(`/messages/${button.dataset.boost}/boosts`,{method:'POST',body:new URLSearchParams({content:button.dataset.emoji||'👍'}),headers:{'Content-Type':'application/x-www-form-urlencoded','X-CSRF-Token':csrfToken}});
    if(!res.ok) alert('Could not boost message');
    else button.closest('details').open=false;
  });
  messages.addEventListener('submit', async e => {
    const editForm=e.target.closest('.inline-edit form');
    if(editForm){
      e.preventDefault();const article=editForm.closest('.message');
      const response=await fetch(editForm.action,{method:'POST',body:new URLSearchParams(new FormData(editForm)),headers:{Accept:'application/json','X-CSRF-Token':csrfToken}});
      if(response.ok){try{await restoreMessage(article)}catch{alert('Message saved, but could not reload it')}}
      else alert('Could not save message');
      return;
    }
    const form=e.target.closest('.custom-boost-form');if(!form)return;
    e.preventDefault();
    const response=await fetch(form.action,{method:'POST',body:new URLSearchParams(new FormData(form)),headers:{'X-CSRF-Token':csrfToken}});
    if(response.ok){form.reset();form.hidden=true;form.closest('details').open=false;}
    else alert('Could not boost message');
  });
}
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/service-worker').catch(() => {});
document.addEventListener('submit',async event=>{
  const form=event.target;
  if(!(form instanceof HTMLFormElement)||new URL(form.action).pathname!=='/session/logout'||!('serviceWorker' in navigator))return;
  event.preventDefault();
  try{
    const registration=await navigator.serviceWorker.getRegistration();
    const subscription=await registration?.pushManager?.getSubscription();
    if(subscription){const field=document.createElement('input');field.type='hidden';field.name='push_subscription_endpoint';field.value=subscription.endpoint;form.append(field)}
  }catch{}
  HTMLFormElement.prototype.submit.call(form);
},true);
document.addEventListener('click',async(event)=>{
  const button=event.target.closest('[data-enable-push]'); if(!button) return;
  const status=button.parentElement.querySelector('[data-push-status]')||document.querySelector('[data-push-status]');
  const report=(message)=>{if(status) status.textContent=message};
  if(!('serviceWorker' in navigator)||!('PushManager' in window)||!('Notification' in window)){report('Browser notifications are unavailable here.');return}
  try {
    const permission=await Notification.requestPermission();
    if(permission!=='granted'){report('Notification permission was not granted.');return}
    const registration=await navigator.serviceWorker.ready;
    let subscription=await registration.pushManager.getSubscription();
    if(!subscription){
      const key=document.querySelector('meta[name="vapid-public-key"]')?.content||'';
      const padded=(key+'='.repeat((4-key.length%4)%4)).replace(/-/g,'+').replace(/_/g,'/');
      subscription=await registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:Uint8Array.from(atob(padded),char=>char.charCodeAt(0))});
    }
    const {endpoint,keys:{p256dh,auth}}=subscription.toJSON();
    const response=await fetch('/users/me/push_subscriptions',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify({push_subscription:{endpoint,p256dh_key:p256dh,auth_key:auth}})});
    if(!response.ok) throw Error(`Subscription failed (${response.status})`);
    report('Browser notifications enabled.');
  } catch(error){report(error.message||'Could not enable notifications.')}
});
