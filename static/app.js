document.addEventListener('trix-file-accept',event=>event.preventDefault());
document.addEventListener('toggle',event=>{
  const popup=event.target.closest?.('details[data-controller~="popup"]');
  if(!popup?.open)return;
  const menu=popup.querySelector('[data-popup-target="menu"]');
  if(!menu)return;
  const bounds=menu.getBoundingClientRect();
  popup.classList.toggle(popup.dataset.popupOrientationTopClass||'popup-orientation-top',window.innerHeight-bounds.bottom<90);
  menu.style.setProperty('--max-width',`${window.innerWidth-bounds.left}px`);
},true);
document.addEventListener('click',event=>{
  for(const popup of document.querySelectorAll('details[data-controller~="popup"][open]'))if(!popup.contains(event.target))popup.open=false;
});
document.addEventListener('keydown',event=>{
  if(event.key==='Escape')for(const popup of document.querySelectorAll('details[data-controller~="popup"][open]'))popup.open=false;
});
const csrfToken=document.querySelector('meta[name="csrf-token"]')?.content||'';
document.querySelectorAll('form[data-controller~="auto-submit"]').forEach(form=>form.requestSubmit());
document.addEventListener('keydown',event=>{
  if(event.key!=='Enter'||(!event.ctrlKey&&!event.metaKey)||event.shiftKey||event.altKey)return;
  const form=event.target instanceof Element?event.target.closest('.custom-styles-panel form'):null;
  if(form){event.preventDefault();form.requestSubmit();}
});
const decodeAutocompleteName=value=>{const textarea=document.createElement('textarea');textarea.innerHTML=value;return textarea.value;};
const formatLocalTimes=(root=document)=>root.querySelectorAll('[data-local-time-target]').forEach(node=>{const date=new Date(node.dateTime);if(Number.isNaN(date.getTime()))return;const style=node.dataset.localTimeTarget==='date'?{dateStyle:'long'}:{dateStyle:'short',timeStyle:'short'};node.textContent=new Intl.DateTimeFormat(undefined,style).format(date);node.title=node.textContent});
formatLocalTimes();
const searchShell=document.querySelector('.search-shell');
if(searchShell){
  const results=searchShell.querySelector('#search-results');
  let previous=null,previousDay=null;
  for(const message of results.querySelectorAll('.message')){
    const timestamp=Number(message.dataset.messageTimestamp);
    const previousTime=previous?Number(previous.dataset.messageTimestamp):NaN;
    message.classList.toggle('own',message.dataset.userId===document.body.dataset.userId);
    message.classList.toggle('threaded',!!previous&&message.dataset.userId===previous.dataset.userId&&Number.isFinite(timestamp)&&Number.isFinite(previousTime)&&Math.abs(timestamp-previousTime)<=300000);
    const day=Number.isFinite(timestamp)?new Date(timestamp).toDateString():null;
    message.classList.toggle('message--first-of-day',day!==previousDay);
    previousDay=day;previous=message;
  }
  results.querySelectorAll('[data-controller~="web-share"]').forEach(node=>{node.hidden=typeof navigator.canShare!=='function';});
  searchShell.addEventListener('click',event=>{if(event.target.closest('[data-toggle-sidebar]'))searchShell.querySelector('.sidebar')?.classList.toggle('open');});
}
document.addEventListener('click',async event=>{
  const copy=event.target.closest('[data-action~="copy-to-clipboard#copy"]');
  if(!copy)return;
  event.preventDefault();
  const successClass=copy.dataset.copyToClipboardSuccessClass||'btn--success';
  copy.classList.remove(successClass);
  void copy.offsetWidth;
  try{
    await navigator.clipboard.writeText(copy.dataset.copyToClipboardContentValue||'');
    copy.classList.add(successClass);
  }catch{}
});
document.querySelectorAll('.account-settings [data-controller~="web-share"]').forEach(button=>{button.hidden=typeof navigator.canShare!=='function';});
let profileInstallPrompt;
window.addEventListener('beforeinstallprompt',event=>{event.preventDefault();profileInstallPrompt=event;document.querySelectorAll('.profile-settings [data-controller~="pwa-install"]').forEach(node=>node.classList.add('pwa--can-install'));});
document.addEventListener('click',event=>{const button=event.target.closest('[data-action~="pwa-install#promptInstall"]');if(button&&profileInstallPrompt){event.preventDefault();profileInstallPrompt.prompt();profileInstallPrompt=undefined;}});
document.addEventListener('click',async event=>{
  const settings=event.target.closest('.account-settings, #system_welcome');
  if(!settings)return;
  const qr=event.target.closest('[data-action~="lightbox#open"]');
  if(qr){
    event.preventDefault();
    let dialog=document.querySelector('.image-lightbox');
    if(!dialog){dialog=document.createElement('dialog');dialog.className='image-lightbox';dialog.innerHTML='<button type="button" aria-label="Close image">×</button><img alt="Join link QR code">';dialog.querySelector('button').addEventListener('click',()=>dialog.close());dialog.addEventListener('click',e=>{if(e.target===dialog)dialog.close()});document.body.append(dialog)}
    dialog.querySelector('img').src=qr.href;dialog.showModal();return;
  }
  const share=event.target.closest('[data-action~="web-share#share"]');
  if(share){event.preventDefault();try{await navigator.share({url:share.dataset.webShareUrlValue,title:share.dataset.webShareTitleValue,text:share.dataset.webShareTextValue})}catch(error){if(error.name!=='AbortError')alert('Could not share join link')};}
});
document.querySelectorAll('[data-controller~="upload-preview"]').forEach(control=>{
  const input=control.querySelector('[data-upload-preview-target="input"]');
  const image=control.querySelector('[data-upload-preview-target="image"]');
  if(!input||!image)return;
  let objectUrl;
  input.addEventListener('change',()=>{
    if(objectUrl)URL.revokeObjectURL(objectUrl);
    objectUrl=input.files?.[0]&&input.files[0].type.startsWith('image/')?URL.createObjectURL(input.files[0]):undefined;
    if(objectUrl)image.src=objectUrl;
  });
  window.addEventListener('pagehide',()=>{if(objectUrl)URL.revokeObjectURL(objectUrl)},{once:true});
});
function initPingForm(){
  const pingForm=document.querySelector('#direct_rooms_control form[action="/rooms/directs"]');
  if(!pingForm||pingForm.dataset.initialized)return;
  pingForm.dataset.initialized='true';
  const input=pingForm.querySelector('[data-autocomplete-target="input"]');
  const select=pingForm.querySelector('[data-autocomplete-target="select"]');
  const template=pingForm.querySelector('#autocompletable-user');
  const pillContainer=input?.parentElement;
  if(!input||!select||!template||!pillContainer)return;
  const suggestionBox=document.createElement('div');
  suggestionBox.className='ping-suggestions';suggestionBox.id='ping-suggestions';suggestionBox.setAttribute('role','listbox');suggestionBox.hidden=true;
  pingForm.querySelector('.autocomplete__container')?.append(suggestionBox);
  input.setAttribute('role','combobox');input.setAttribute('aria-autocomplete','list');input.setAttribute('aria-controls','ping-suggestions');input.setAttribute('aria-expanded','false');
  const selected=new Map();
  let options=[],active=0,generation=0,timer;
  const hide=()=>{options=[];suggestionBox.replaceChildren();suggestionBox.hidden=true;input.setAttribute('aria-expanded','false');input.removeAttribute('aria-activedescendant');};
  const markActive=()=>{[...suggestionBox.children].forEach((button,index)=>button.setAttribute('aria-selected',String(index===active)));input.setAttribute('aria-activedescendant',`ping-option-${active}`);};
  const renderSelected=()=>{
    select.replaceChildren();pillContainer.querySelectorAll(':scope > .autocomplete__pill').forEach(pill=>pill.remove());
    for(const [id,person] of selected){
      const option=document.createElement('option');option.value=String(id);option.selected=true;option.textContent=person.name;select.append(option);
      const pill=template.content.firstElementChild.cloneNode(true);pill.dataset.value=String(id);
      pill.querySelector('[data-content="avatar"]').src=person.avatar_url;
      pill.querySelector('[data-content="label"]').textContent=person.name;
      pill.querySelector('[data-content="screenReaderLabel"]').textContent=person.name;
      const remove=pill.querySelector('button');remove.dataset.value=String(id);remove.addEventListener('click',()=>{selected.delete(id);renderSelected();input.focus();});
      pillContainer.insertBefore(pill,input);
    }
    select.required=false;input.required=!selected.size;
  };
  const choose=person=>{selected.set(person.value,person);renderSelected();input.value='';input.setCustomValidity('');generation++;clearTimeout(timer);hide();input.focus();};
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
    if(event.key==='Escape'){event.preventDefault();pingForm.querySelector('[data-form-target="cancel"]')?.click();return;}
    if(event.key==='Backspace'&&!input.value&&selected.size){selected.delete([...selected.keys()].at(-1));renderSelected();return;}
    if(suggestionBox.hidden||!options.length)return;
    if(event.key==='ArrowDown'||event.key==='ArrowUp'){event.preventDefault();active=(active+(event.key==='ArrowDown'?1:-1)+options.length)%options.length;markActive();}
    if(event.key==='Enter'||event.key==='Tab'){event.preventDefault();choose(options[active]);}
  });
  input.addEventListener('blur',()=>setTimeout(()=>{if(!suggestionBox.contains(document.activeElement))hide();},150));
  pingForm.addEventListener('submit',event=>{if(!selected.size){event.preventDefault();input.setCustomValidity('Choose a person from the list');input.reportValidity();input.focus();}});
  renderSelected();input.focus();
}
initPingForm();
const newRoomPanel=document.querySelector('section.panel[style="view-transition-name: new-room"],section.panel[style^="view-transition-name: edit-room-"]');
if(newRoomPanel){
  const nameInput=newRoomPanel.querySelector('#room_name');
  const savedName=sessionStorage.getItem('rustfire-new-room-name');
  if(savedName!==null&&nameInput){nameInput.value=savedName;sessionStorage.removeItem('rustfire-new-room-name');}
  const filterInput=newRoomPanel.querySelector('menu #search');
  if(filterInput){
    const people=[...newRoomPanel.querySelectorAll('[data-filter-target="list"] > li[data-value]')];
    filterInput.addEventListener('input',()=>{
      const query=filterInput.value.trim().toLocaleLowerCase();
      for(const person of people)person.hidden=!person.dataset.value.includes(query);
    });
  }
  newRoomPanel.querySelector('[data-turbo-action="replace"]')?.addEventListener('click',event=>{
    event.preventDefault();
    if(nameInput)sessionStorage.setItem('rustfire-new-room-name',nameInput.value);
    location.href=event.currentTarget.href;
  });
}
const chat = document.querySelector('.chat');
if(!chat){
  document.addEventListener('click',event=>{
    if(event.target.closest('#direct_rooms_control [data-form-target="cancel"]')){
      event.preventDefault();
      location.href='/';
    }
  });
}
if (chat) {
  const roomId = Number(chat.dataset.roomId);
  const messages = chat.querySelector('.messages');
  const messageById=id=>messages.querySelector(`.message[data-message-id="${Number(id)}"]`);
  const decorateOwn=()=>{
    messages.querySelectorAll('.message').forEach(node=>node.classList.toggle('own',node.dataset.userId===document.body.dataset.userId));
    messages.querySelectorAll('.boost-item').forEach(node=>{
      const mine=node.dataset.boostDeleteBoosterIdValue===document.body.dataset.userId;
      node.classList.toggle('mine',mine);
      const content=node.querySelector('[data-boost-delete-target="content"]');
      if(mine){content?.setAttribute('tabindex','0');content?.setAttribute('aria-describedby','delete_boost_accessible_label');}
      else{content?.removeAttribute('tabindex');content?.removeAttribute('aria-describedby');}
    });
    messages.querySelectorAll('[data-controller~="web-share"]').forEach(node=>{node.hidden=typeof navigator.canShare!=='function';});
  };
  const formatMessageGroups=()=>{
    let previous=null,previousDay=null;
    for(const message of messages.querySelectorAll('.message')){
      const time=Number(message.dataset.messageTimestamp);
      const priorTime=previous?Number(previous.dataset.messageTimestamp):NaN;
      message.classList.toggle('threaded',!!previous&&message.dataset.userId===previous.dataset.userId&&Number.isFinite(time)&&Number.isFinite(priorTime)&&Math.abs(time-priorTime)<=300000);
      if(Number.isFinite(time)){
        const date=new Date(time);
        const day=`${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}`;
        message.classList.toggle('message--first-of-day',day!==previousDay);
        previousDay=day;
      }else message.classList.remove('message--first-of-day');
      previous=message;
    }
  };
  decorateOwn();
  formatMessageGroups();
  let historyMode=chat.dataset.historyMode==='true';
  const atMessage=chat.dataset.atMessage;
  if(atMessage)messageById(atMessage)?.scrollIntoView({block:'center'});
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
  let cursor = Number(Array.from(messages.querySelectorAll('.message[data-message-id]')).at(-1)?.dataset.messageId)||0;
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
      for(;;){
        const response=await fetch(`/rooms/${roomId}/refresh_state?after=${cursor}`,{headers:{Accept:'application/json'}});
        if(!response.ok)throw Error(`Missed messages returned ${response.status}`);
        const page=await response.json();
        for(const entry of page.messages)if(!messageById(entry.id))messages.insertAdjacentHTML('beforeend',entry.html);
        cursor=page.next_after;
        formatLocalTimes(messages);decorateOwn();formatMessageGroups();
        if(!page.has_more||!page.messages.length)break;
      }
      const refreshed=await fetch(`/rooms/${roomId}/refresh?since=${lastRefreshAt}`,{headers:{Accept:'text/vnd.turbo-stream.html'}});
      if(!refreshed.ok)throw Error(`Room refresh returned ${refreshed.status}`);
      applyRoomStream(await refreshed.text());
      lastRefreshAt=Math.max(lastRefreshAt,...Array.from(messages.querySelectorAll('.message[data-message-updated-at]'),node=>Number(node.dataset.messageUpdatedAt)||0));
      cursor=Number(Array.from(messages.querySelectorAll('.message[data-message-id]')).at(-1)?.dataset.messageId)||cursor;
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
  const sidebarStreamIdents=[...document.querySelectorAll('.sidebar turbo-cable-stream-source[channel="Turbo::StreamsChannel"]')].map(source=>JSON.stringify({channel:'Turbo::StreamsChannel',signed_stream_name:source.getAttribute('signed-stream-name')}));
  const markRoom=(rid,unread)=>document.querySelectorAll(`.sidebar a[href='/rooms/${rid}']`).forEach(link=>{link.classList.toggle('unread',unread);if(unread&&link.parentElement?.id==='direct_rooms')link.parentElement.prepend(link)});
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
  document.addEventListener('click',async event=>{
    const newPing=event.target.closest('.sidebar .direct__new');
    if(newPing){
      event.preventDefault();
      try{
        const response=await fetch(newPing.href,{headers:{'Turbo-Frame':'direct_rooms_control'}});
        if(!response.ok)throw Error(`Ping form returned ${response.status}`);
        const frame=new DOMParser().parseFromString(await response.text(),'text/html').querySelector('#direct_rooms_control');
        if(!frame)throw Error('Ping form frame missing');
        document.querySelector('.sidebar #direct_rooms_control')?.replaceWith(frame);
        initPingForm();
      }catch(error){console.error('Could not open ping form',error);location.href=newPing.href}
      return;
    }
    const cancelPing=event.target.closest('.sidebar #direct_rooms_control [data-form-target="cancel"]');
    if(cancelPing){event.preventDefault();await refreshSidebar();}
  });
  function updateDirectRoom(data){
    const nav=document.getElementById('direct_rooms');
    if(!nav||!Number.isInteger(data?.room_id)||typeof data.html!=='string')return false;
    const link=new DOMParser().parseFromString(data.html,'text/html').querySelector('.direct[id^="list_rooms_direct_"]');
    if(!link||link.getAttribute('href')!==`/rooms/${data.room_id}`)return false;
    link.classList.toggle('active',data.room_id===roomId);
    nav.querySelector(`.direct[href='/rooms/${data.room_id}']`)?.remove();
    nav.prepend(link);
    if(sidebarRefreshPending)sidebarRefreshAgain=true;
    return true;
  }
  function applySidebarStream(html){
    const parsed=new DOMParser().parseFromString(html,'text/html');
    for(const stream of parsed.querySelectorAll('turbo-stream')){
      const target=document.getElementById(stream.getAttribute('target'));
      if(!target||!target.closest('.sidebar'))continue;
      const action=stream.getAttribute('action');
      if(action==='remove'){target.remove();continue;}
      const fragment=stream.querySelector('template')?.content.cloneNode(true);
      if(!fragment)continue;
      for(const link of fragment.querySelectorAll('a[id^="list_rooms_"]')){
        if(link.id)document.getElementById(link.id)?.remove();
        link.classList.toggle('active',link.getAttribute('href')===`/rooms/${roomId}`);
      }
      if(action==='prepend')target.prepend(fragment);
      else if(action==='replace')target.replaceWith(fragment);
    }
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
        const scrollToLatest=target===messages&&(nearBottom()||[...fragment.querySelectorAll('.message')].some(node=>node.dataset.userId===document.body.dataset.userId));
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
        if(target.closest('.composer--edit'))continue;
        target.replaceWith(fragment);
        formatLocalTimes(messages);decorateOwn();formatMessageGroups();
      }
    }
  }
  function connect() {
    socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/cable`, 'actioncable-v1-json');
    socket.addEventListener('open', () => { catchingUp = false; for(const identifier of [messageIdent,typingIdent,presenceIdent,unreadIdent,readIdent,roomListIdent,...sidebarStreamIdents])socket.send(JSON.stringify({command:'subscribe',identifier})); });
    socket.addEventListener('message', e => {
      try { const frame = JSON.parse(e.data); if (frame.type === 'confirm_subscription') { if(frame.identifier===messageIdent)catchUp(); if(frame.identifier===presenceIdent&&document.hidden)sendPresence('absent'); if(frame.identifier===roomListIdent)refreshSidebar(); return; } const data = frame.message; if(frame.identifier===messageIdent){if(typeof data==='string')applyRoomStream(data);return;} if(sidebarStreamIdents.includes(frame.identifier)){if(typeof data==='string')applySidebarStream(data);return;} if(frame.identifier===typingIdent){typingFrame(data);return;} if(frame.identifier===unreadIdent){markRoom(data?.roomId,true);return;} if(frame.identifier===readIdent){markRoom(data?.room_id,false);return;} if(frame.identifier===roomListIdent){if(data?.type!=='direct_room_added'||!updateDirectRoom(data))refreshSidebar();return;}
      } catch {}
    });
    socket.addEventListener('close', () => {for(const person of typingPeople.values())clearTimeout(person.timer);typingPeople.clear();renderTyping();setTimeout(connect, 1500);});
  }
  connect();
  document.addEventListener('click',event=>{if(event.target.closest('[data-toggle-sidebar], .sidebar__toggle'))document.querySelector('.sidebar')?.classList.toggle('open')});
  const composer=document.getElementById('composer');
  async function restoreMessage(article){
    const response=await fetch(`/rooms/${roomId}/messages/${article.dataset.messageId}`,{headers:{'X-Rustfire-Fragment':'1'}});
    if(!response.ok)throw Error('Could not load message');
    article.outerHTML=await response.text();
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
  const fileInput=composer.querySelector('input[type=file]');
  const fileList=document.getElementById('composer-filelist');
  const queuedFiles=[];
  const renderQueuedFiles=()=>{
    fileList.replaceChildren();
    fileList.hidden=queuedFiles.length===0;
    queuedFiles.forEach((entry,index)=>{
      const card=document.createElement('div');card.className='composer-file';
      const thumb=document.createElement('img');thumb.className='composer-file-thumb';thumb.src=entry.url||'/static/icons/common-file-text.svg';thumb.alt='';
      const name=document.createElement('span');name.className='composer-file-name';name.textContent=entry.file.name;
      const remove=document.createElement('button');remove.type='button';remove.className='composer-file-remove';remove.textContent='×';remove.setAttribute('aria-label',`Remove ${entry.file.name}`);remove.disabled=!!entry.uploading;
      remove.addEventListener('click',()=>{if(entry.url)URL.revokeObjectURL(entry.url);queuedFiles.splice(index,1);renderQueuedFiles()});
      card.append(thumb,name,remove);
      if(entry.uploading){const status=document.createElement('small');status.textContent='Uploading…';card.append(status)}
      fileList.append(card);
    });
  };
  const addFiles=files=>{
    for(const file of files)queuedFiles.push({file,url:file.type.startsWith('image/')?URL.createObjectURL(file):null,uploading:false});
    queuedFiles.sort((a,b)=>a.file.name.localeCompare(b.file.name));
    renderQueuedFiles();
  };
  fileInput.addEventListener('change',()=>{addFiles(fileInput.files);fileInput.value=''});
  composer.addEventListener('paste',event=>{if(event.clipboardData?.files?.length){event.preventDefault();addFiles(event.clipboardData.files)}});
  composer.addEventListener('dragover',event=>{if(Array.from(event.dataTransfer?.types||[]).includes('Files'))event.preventDefault()});
  composer.addEventListener('drop',event=>{if(event.dataTransfer?.files?.length){event.preventDefault();addFiles(event.dataTransfer.files)}});
  let sending=false;
  composer.addEventListener('submit', async e => {
    e.preventDefault(); const form=e.currentTarget;
    if(sending||(!typingInput.editor?.getDocument().toString().trim()&&!queuedFiles.length))return;
    sending=true;
    const sendingFiles=[...queuedFiles];sendingFiles.forEach(entry=>entry.uploading=true);renderQueuedFiles();
    clearTimeout(typingTimer);sendTyping('stop');
    try{
      if(typingInput.editor?.getDocument().toString().trim()){
        const body=new FormData(form);body.delete('message[attachment]');
        const response=await fetch(form.action,{method:'POST',body,headers:{Accept:'application/json','X-CSRF-Token':csrfToken}});
        if(!response.ok)throw Error('Could not send message');
        bodyInput.value='';typingInput.editor.loadHTML('');form.querySelector('[name="message[client_message_id]"]').value=crypto.randomUUID();
      }
      for(const entry of sendingFiles){
        const body=new FormData();body.append('message[attachment]',entry.file,entry.file.name);body.append('message[client_message_id]',crypto.randomUUID());
        const response=await fetch(form.action,{method:'POST',body,headers:{Accept:'application/json','X-CSRF-Token':csrfToken}});
        if(!response.ok)throw Error(`Could not upload ${entry.file.name}`);
        queuedFiles.splice(queuedFiles.indexOf(entry),1);if(entry.url)URL.revokeObjectURL(entry.url);renderQueuedFiles();
      }
      if(historyMode)location.href=`/rooms/${roomId}`;
    }catch(error){sendingFiles.forEach(entry=>entry.uploading=false);renderQueuedFiles();alert(error.message||'Could not send message')}
    finally{sending=false}
  });
  function revealBoost(node){
    const boost=node.closest('.boost-item');
    if(!boost?.classList.contains('mine'))return;
    boost.classList.toggle('expanded');
    boost.querySelector('[data-boost-delete-target="button"]')?.focus();
  }
  messages.addEventListener('keydown',event=>{
    const content=event.target.closest('[data-boost-delete-target="content"]');
    if(content&&(event.key==='Enter'||event.key===' ')){event.preventDefault();revealBoost(content);}
  });
  messages.addEventListener('click',event=>{
    const summary=event.target.closest('.message__actions details summary');
    if(!summary)return;
    requestAnimationFrame(()=>{
      const details=summary.parentElement;
      if(!details.open){details.classList.remove('opens-up');return;}
      const menu=details.querySelector('.message__actions-menu');
      details.classList.toggle('opens-up',menu.getBoundingClientRect().bottom>messages.getBoundingClientRect().bottom-8);
    });
  });
  messages.addEventListener('click', async e => {
    const boostDelete=e.target.closest('[data-boost-delete-target="button"]');
    if(boostDelete){
      e.preventDefault();
      const response=await fetch(boostDelete.closest('form').action,{method:'DELETE',headers:{'X-CSRF-Token':csrfToken,Accept:'text/vnd.turbo-stream.html'}});
      if(response.ok)boostDelete.closest('.boost-item')?.remove();
      else alert('Could not delete boost');
      return;
    }
    const boostReveal=e.target.closest('[data-boost-delete-target="content"]');
    if(boostReveal){revealBoost(boostReveal);return;}
    const edit=e.target.closest('.message__edit-btn');
    if(edit){
      e.preventDefault();const article=edit.closest('.message');
      const frame=article.querySelector('turbo-frame[id^="edit_message_"]');
      if(!frame){alert('Could not open editor');return;}
      const response=await fetch(edit.href,{headers:{'Turbo-Frame':frame.id}});
      if(!response.ok){alert('Could not edit message');return;}
      const returned=new DOMParser().parseFromString(await response.text(),'text/html').getElementById(frame.id);
      if(!returned){alert('Could not open editor');return;}
      frame.replaceWith(returned);
      article.classList.add('editing');edit.closest('details').open=false;
      article.querySelector('.composer--edit trix-editor')?.focus();return;
    }
    const cancel=e.target.closest('.message__edit-close-btn,[data-form-target="cancel"]');
    if(cancel){e.preventDefault();try{await restoreMessage(cancel.closest('.message'))}catch{alert('Could not restore message')}return;}
    const cancelBoost=e.target.closest('[data-cancel-custom-boost]');
    if(cancelBoost){
      e.preventDefault();const frame=cancelBoost.closest('turbo-frame');
      if(frame?.dataset.originalHtml){frame.innerHTML=frame.dataset.originalHtml;delete frame.dataset.originalHtml;}
      return;
    }
    const customBoost=e.target.closest('a[href$="/boosts/new"]');
    if(customBoost){
      e.preventDefault();
      const frameId=customBoost.dataset.turboFrame||customBoost.closest('turbo-frame')?.id;
      const frame=frameId&&document.getElementById(frameId);
      if(!frame){alert('Could not open boost form');return;}
      const details=customBoost.closest('details');
      const response=await fetch(customBoost.href,{headers:{'Turbo-Frame':frameId}});
      if(!response.ok){alert('Could not open boost form');return;}
      const returned=new DOMParser().parseFromString(await response.text(),'text/html').getElementById(frameId);
      if(!returned){alert('Could not open boost form');return;}
      if(!frame.dataset.originalHtml)frame.dataset.originalHtml=frame.innerHTML;
      frame.innerHTML=returned.innerHTML;
      if(details)details.open=false;
      frame.querySelector('[name="boost[content]"]')?.focus();
      return;
    }
    const lightbox=e.target.closest('[data-action~="lightbox#open"],[data-lightbox]');
    if(lightbox){e.preventDefault();let dialog=document.querySelector('.image-lightbox');if(!dialog){dialog=document.createElement('dialog');dialog.className='image-lightbox';dialog.innerHTML='<button type="button" aria-label="Close image">×</button><img alt="">';dialog.querySelector('button').addEventListener('click',()=>dialog.close());dialog.addEventListener('click',event=>{if(event.target===dialog)dialog.close()});document.body.append(dialog)}dialog.querySelector('img').src=lightbox.href;dialog.showModal();return;}
    const share=e.target.closest('[data-action~="web-share#share"]');
    if(share){
      e.preventDefault();
      try{
        const response=await fetch(share.dataset.webShareFilesValue);
        if(!response.ok)throw Error('Could not load attachment');
        const blob=await response.blob();
        const filename=`Campfire_${Math.random().toString(36).slice(2)}.${blob.type.split('/').pop()}`;
        await navigator.share({title:share.dataset.webShareTitleValue||'',files:[new File([blob],filename,{type:blob.type})]});
      }catch(error){if(error.name!=='AbortError')alert('Could not share attachment');}
      return;
    }
    const sound=e.target.closest('[data-sound]'); if(sound) { new Audio(sound.dataset.sound).play().catch(()=>{}); return; }
    const reply=e.target.closest('[data-action~="reply#reply"]');
    if(reply){
      const article=reply.closest('.message');const body=article.querySelector('[id^="presentation_message_"]').cloneNode(true);
      const preview=body.querySelector('.og-embed a')?.href;
      body.querySelectorAll('.og-embed').forEach(node=>node.remove());
      body.querySelectorAll('.mention').forEach(node=>node.replaceWith(document.createTextNode(node.textContent.trim())));
      const quoted=body.querySelector('.trix-content')?.innerHTML||body.innerHTML||preview||'';
      const block=document.createElement('blockquote');block.innerHTML=quoted;
      const cite=document.createElement('cite');cite.textContent=article.querySelector('.message__meta strong')?.textContent+' ';
      const link=document.createElement('a');link.href=article.querySelector('.message__meta a')?.href||'#';link.textContent='#';cite.append(link);
      typingInput.editor.loadHTML(block.outerHTML+cite.outerHTML+'<br>');typingInput.focus();reply.closest('details').open=false;return;
    }
  });
  messages.addEventListener('submit', async e => {
    const deleteForm=e.target.closest('form[id^="delete_form_message_"]');
    if(deleteForm){
      e.preventDefault();if(!confirm('Are you sure you want to delete this message?'))return;
      const response=await fetch(deleteForm.action,{method:'DELETE',headers:{'X-CSRF-Token':csrfToken}});
      if(response.ok){deleteForm.closest('.message').remove();formatMessageGroups()}
      else alert('Could not delete message');
      return;
    }
    const editForm=e.target.closest('form[id^="form_message_"]');
    if(editForm){
      e.preventDefault();const article=editForm.closest('.message');
      const body=new URLSearchParams(new FormData(editForm));body.set('message[format]','html');
      const response=await fetch(editForm.action,{method:'PATCH',body,headers:{Accept:'application/json','X-CSRF-Token':csrfToken}});
      if(response.ok){try{await restoreMessage(article)}catch{alert('Message saved, but could not reload it')}}
      else alert('Could not save message');
      return;
    }
    const quickBoost=e.target.closest('.quick-boosts form');
    if(quickBoost){
      e.preventDefault();
      const response=await fetch(quickBoost.action,{method:'POST',body:new URLSearchParams(new FormData(quickBoost)),headers:{'X-CSRF-Token':csrfToken}});
      if(response.ok)quickBoost.closest('details').open=false;
      else alert('Could not boost message');
      return;
    }
    const form=e.target.closest('.custom-boost-form');if(!form)return;
    e.preventDefault();
    const response=await fetch(form.action,{method:'POST',body:new URLSearchParams(new FormData(form)),headers:{'X-CSRF-Token':csrfToken}});
    if(response.ok){
      form.reset();const frame=form.closest('turbo-frame');
      if(frame?.dataset.originalHtml){frame.innerHTML=frame.dataset.originalHtml;delete frame.dataset.originalHtml;}
    }
    else alert('Could not boost message');
  });
}
const accountUsers=document.getElementById('account_users');
if(accountUsers){
  const observer='IntersectionObserver' in window?new IntersectionObserver(entries=>{
    for(const entry of entries)if(entry.isIntersecting)loadPage(entry.target);
  },{rootMargin:'300px'}):null;
  const observeNext=()=>{
    const frame=accountUsers.querySelector('#next_page_container[src]');
    if(frame){if(observer)observer.observe(frame);else loadPage(frame);}
  };
  async function loadPage(frame){
    if(frame.dataset.loading)return;
    frame.dataset.loading='true';
    observer?.unobserve(frame);
    try{
      const response=await fetch(frame.getAttribute('src'),{headers:{Accept:'text/vnd.turbo-stream.html'}});
      if(!response.ok)throw Error(`Could not load people (${response.status})`);
      const documentStream=new DOMParser().parseFromString(await response.text(),'text/html');
      for(const stream of documentStream.querySelectorAll('turbo-stream')){
        const target=document.getElementById(stream.getAttribute('target'));
        const fragment=stream.querySelector('template')?.content.cloneNode(true);
        if(!target||!fragment)continue;
        if(stream.getAttribute('action')==='replace')target.replaceWith(fragment);
        if(stream.getAttribute('action')==='append')target.append(fragment);
      }
      observeNext();
    }catch{
      frame.dataset.loading='';
      frame.replaceChildren();
      const retry=document.createElement('button');
      retry.type='button';retry.textContent='Load more people';
      retry.addEventListener('click',()=>loadPage(frame),{once:true});
      frame.append(retry);
    }
  }
  observeNext();
}
const notificationsControl=document.querySelector('.button_to_change_notifying[data-controller="notifications"]');
if(notificationsControl){
  let frame=notificationsControl.querySelector('turbo-frame[id^="involvement_rooms_"]');
  const roomBell=frame?.querySelector('[data-notifications-target="bell"]');
  const dialog=notificationsControl.querySelector('[data-notifications-target="notAllowedNotice"]');
  let installPrompt=null;
  window.addEventListener('beforeinstallprompt',event=>{
    event.preventDefault();
    installPrompt=event;
    dialog?.querySelector('.pwa__instructions')?.classList.add('pwa--can-install');
  });
  const showHelp=()=>{
    if(dialog instanceof HTMLDialogElement&&!dialog.open){
      dialog.showModal();
      const visible=[...dialog.querySelectorAll('[data-notifications-target="details"]')].filter(item=>item.getClientRects().length);
      if(visible.length===1)visible[0].open=true;
    }
  };
  const showAlert=()=>{
    roomBell?.querySelectorAll('img').forEach(image=>image.hidden=!image.hidden);
    if(!document.cookie.includes('notifications-first-run-seen='))roomBell?.classList.add('btn--pulsing');
  };
  const markSeen=()=>{
    roomBell?.classList.remove('btn--pulsing');
    document.cookie='notifications-first-run-seen=true; SameSite=Lax; Path=/; Max-Age=31536000';
  };
  const loadFrame=async()=>{
    const response=await fetch(frame.dataset.turboFrameUrlParam,{headers:{'Turbo-Frame':frame.id,'Accept':'text/html'}});
    if(!response.ok)throw Error(`Could not load notifications (${response.status})`);
    const page=new DOMParser().parseFromString(await response.text(),'text/html');
    const replacement=page.querySelector(`turbo-frame#${frame.id}`);
    if(!replacement)throw Error('Notification frame missing');
    frame.replaceWith(replacement);
    frame=replacement;
  };
  const hasSubscription=async()=>{
    if(!('serviceWorker' in navigator)||!('PushManager' in window)||!('Notification' in window)||Notification.permission!=='granted')return false;
    const registration=await navigator.serviceWorker.getRegistration(window.location.origin);
    return !!(await registration?.pushManager?.getSubscription());
  };
  hasSubscription().then(enabled=>enabled?loadFrame():showAlert()).catch(showAlert);
  const subscribe=async()=>{
    if(!('serviceWorker' in navigator)||!('PushManager' in window)||!('Notification' in window))return false;
    const permission=Notification.permission==='granted'?'granted':await Notification.requestPermission();
    if(permission!=='granted')return false;
    const registration=await navigator.serviceWorker.getRegistration()||await navigator.serviceWorker.register('/service-worker');
    let subscription=await registration.pushManager.getSubscription();
    if(!subscription){
      const key=document.querySelector('meta[name="vapid-public-key"]')?.content||'';
      const padded=(key+'='.repeat((4-key.length%4)%4)).replace(/-/g,'+').replace(/_/g,'/');
      subscription=await registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:Uint8Array.from(atob(padded),char=>char.charCodeAt(0))});
    }
    const {endpoint,keys:{p256dh,auth}}=subscription.toJSON();
    const response=await fetch('/users/me/push_subscriptions',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify({push_subscription:{endpoint,p256dh_key:p256dh,auth_key:auth}})});
    if(!response.ok)throw Error(`Subscription failed (${response.status})`);
    return true;
  };
  roomBell?.addEventListener('click',async()=>{
    roomBell.disabled=true;
    markSeen();
    try{
      if((await hasSubscription())||await subscribe())await loadFrame();
      else showHelp();
    }catch(error){showHelp()}
    finally{roomBell.disabled=false}
  });
  notificationsControl.addEventListener('click',async event=>{
    if(!event.target.closest('[data-action="pwa-install#promptInstall"]')||!installPrompt)return;
    await installPrompt.prompt();
    installPrompt=null;
  });
  notificationsControl.addEventListener('submit',async event=>{
    const form=event.target;
    if(!(form instanceof HTMLFormElement)||form.closest('turbo-frame')!==frame)return;
    event.preventDefault();
    const button=form.querySelector('button[type="submit"]');
    if(button)button.disabled=true;
    try{
      const response=await fetch(form.action,{method:'POST',headers:{'X-CSRF-Token':csrfToken,'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams(new FormData(form))});
      if(!response.ok)throw Error(`Could not update notifications (${response.status})`);
      await loadFrame();
    }catch(error){showHelp();if(button)button.disabled=false}
  });
  dialog?.addEventListener('click',event=>{if(event.target===dialog)dialog.close()});
}
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/service-worker').catch(() => {});
document.addEventListener('change',event=>{
  const control=event.target;
  if(control instanceof HTMLInputElement){
    if(control.matches('form[data-auto-submit-file] input[type=file]')&&control.files?.length)control.form.requestSubmit();
    if(control.matches('form[data-auto-submit-switch] input[type=checkbox],.account-settings input[data-action="change->form#submit"]'))control.form.requestSubmit();
    if(control.matches('#account_users input[data-action="form#submit"][name="user[role]"]'))control.form.requestSubmit();
  }
});
document.addEventListener('submit',event=>{
  const confirmation=event.target instanceof HTMLFormElement?event.target.querySelector('button[data-turbo-confirm]'):null;
  if(confirmation&&!window.confirm(confirmation.dataset.turboConfirm))event.preventDefault();
},true);
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
