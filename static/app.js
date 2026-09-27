document.addEventListener('trix-file-accept',event=>event.preventDefault());
let submitMessageEdit;
document.addEventListener('submit',event=>{
  const form=event.target instanceof Element?event.target.closest('form[id^="form_message_"]'):null;
  if(!form||!submitMessageEdit)return;
  event.preventDefault();event.stopImmediatePropagation();
  submitMessageEdit(form);
},true);
document.addEventListener('trix-before-initialize',()=>{Trix.config.blockAttributes.cite={tagName:'cite',inheritable:false};});
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
const currentUserId=document.querySelector('meta[name="current-user-id"]')?.content||document.body.dataset.userId||'';
if('ontouchstart' in window&&navigator.maxTouchPoints>0){
  document.addEventListener('click',event=>{
    const action=event.target.closest('[data-action~="soft-keyboard#open"]');
    const controller=action?.closest('[data-controller~="soft-keyboard"]');
    if(!controller)return;
    const input=document.createElement('input');
    input.type='text';input.className='input--invisible';
    input.addEventListener('focusout',()=>input.remove(),{once:true});
    controller.append(input);input.focus();
  });
}
const centerLoadedForm=root=>{
  const element=root.querySelector('[data-controller~="scroll-into-view"]');
  if(element)requestAnimationFrame(()=>element.scrollIntoView({behavior:'smooth',block:'center'}));
};
document.querySelectorAll('form[data-controller~="auto-submit"]').forEach(form=>form.requestSubmit());
document.addEventListener('keydown',event=>{
  if(event.key!=='Escape'||event.isComposing)return;
  const form=event.target instanceof Element?event.target.closest('form[data-controller~="form"]'):null;
  if(!form?.dataset.action?.includes('keydown.esc->form#cancel'))return;
  event.preventDefault();
  form.querySelector('[data-form-target="cancel"]')?.click();
},true);
document.addEventListener('keydown',event=>{
  const form=event.target instanceof Element?event.target.closest('form[data-controller~="form"]'):null;
  if(!form||event.defaultPrevented||event.isComposing)return;
  const actions=form.dataset.action||'';
  if(event.key==='Escape'&&actions.includes('keydown.esc->form#cancel')){
    form.querySelector('[data-form-target="cancel"]')?.click();
  }else if(event.key==='Enter'){
    const modifier=event.ctrlKey?'ctrl':event.metaKey?'meta':'';
    const action=modifier?`keydown.${modifier}+enter->form#submit`:'keydown.enter->form#submit';
    const targetActions=event.target.dataset?.action||'';
    if(actions.includes(action)||targetActions.includes(action)){
      event.preventDefault();
      form.requestSubmit();
    }
  }
});
const decodeAutocompleteName=value=>{const textarea=document.createElement('textarea');textarea.innerHTML=value;return textarea.value;};
const formatReplyLinks=root=>root.querySelectorAll('[data-reply-target="body"] a').forEach(link=>{link.target=link.href.startsWith(location.origin)?'_top':'_blank';});
const localTimeFormatters={time:new Intl.DateTimeFormat(undefined,{timeStyle:'short'}),date:new Intl.DateTimeFormat(undefined,{dateStyle:'long'}),datetime:new Intl.DateTimeFormat(undefined,{timeStyle:'short',dateStyle:'short'})};
const formatLocalTimes=(root=document)=>root.querySelectorAll('[data-local-time-target]').forEach(node=>{const date=new Date(node.getAttribute('datetime'));const formatter=localTimeFormatters[node.dataset.localTimeTarget];if(!formatter||Number.isNaN(date.getTime()))return;node.textContent=formatter.format(date);node.title=localTimeFormatters.datetime.format(date)});
formatLocalTimes();
const badgeSidebar=document.querySelector('#sidebar');
if(badgeSidebar&&'setAppBadge' in navigator){
  let lastUnreadCount;
  const updateAppBadge=()=>{
    const badgeRoot=badgeSidebar.querySelector('[data-controller~="badge-dot"]');
    if(!badgeRoot)return;
    const unreadClass=badgeRoot.dataset.badgeDotUnreadClass||'unread';
    const count=[...badgeRoot.querySelectorAll('[data-badge-dot-target~="unread"]')].filter(room=>room.classList.contains(unreadClass)).length;
    if(count===lastUnreadCount)return;
    lastUnreadCount=count;
    try{Promise.resolve(count?navigator.setAppBadge(count):navigator.clearAppBadge()).catch(()=>{});}catch{}
  };
  new MutationObserver(updateAppBadge).observe(badgeSidebar,{subtree:true,childList:true,attributes:true,attributeFilter:['class']});
  setTimeout(updateAppBadge,0);
}
const searchResults=document.querySelector('#search-results');
if(searchResults){
  const results=searchResults;
  for(const message of results.querySelectorAll('.message')){
    message.classList.toggle('message--me',message.dataset.userId===currentUserId);
    message.classList.toggle('message--mentioned',!!message.querySelector(`.mention img[src^="/users/${currentUserId}/avatar"]`));
    message.classList.add('message--formatted');
  }
  results.querySelectorAll('[data-controller~="web-share"]').forEach(node=>{node.hidden=typeof navigator.canShare!=='function';});
  formatReplyLinks(results);
  results.scrollTo({top:results.scrollHeight});
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
document.querySelectorAll('[data-controller~="web-share"]').forEach(button=>{button.hidden=typeof navigator.canShare!=='function';});
const lightboxDialog=document.querySelector('dialog[data-lightbox-target="dialog"]');
lightboxDialog?.addEventListener('close',()=>{
  lightboxDialog.querySelector('[data-lightbox-target="zoomedImage"]').src='';
  lightboxDialog.querySelector('[data-lightbox-target="download"]').href='';
  lightboxDialog.querySelector('[data-lightbox-target="share"]').dataset.webShareFilesValue='';
});
document.addEventListener('click',event=>{
  const link=event.target.closest('a[data-action~="lightbox#open"],a[data-lightbox]');
  if(!link||!lightboxDialog)return;
  event.preventDefault();
  lightboxDialog.showModal();
  lightboxDialog.querySelector('[data-lightbox-target="zoomedImage"]').src=link.href;
  lightboxDialog.querySelector('[data-lightbox-target="download"]').href=link.dataset.lightboxUrlValue||'';
  lightboxDialog.querySelector('[data-lightbox-target="share"]').dataset.webShareFilesValue=link.dataset.lightboxUrlValue||'';
});
document.addEventListener('click',async event=>{
  const share=event.target.closest('[data-action~="web-share#share"]');
  if(!share)return;
  event.preventDefault();
  const data={title:share.dataset.webShareTitleValue||'',text:share.dataset.webShareTextValue||''};
  if(share.dataset.webShareUrlValue)data.url=share.dataset.webShareUrlValue;
  try{
    if(share.dataset.webShareFilesValue){
      const response=await fetch(share.dataset.webShareFilesValue);
      if(!response.ok)throw Error('Could not load shared file');
      const blob=await response.blob();
      const filename=`Campfire_${Math.random().toString(36).slice(2)}.${blob.type.split('/').pop()}`;
      data.files=[new File([blob],filename,{type:blob.type})];
    }
    await navigator.share(data);
  }catch(error){if(error.name!=='AbortError')alert('Could not share');}
});
const profileInstallControls=[...document.querySelectorAll('.profile-settings [data-controller~="pwa-install"]')];
let profileInstallPrompt;
if(profileInstallControls.length&&'serviceWorker' in navigator&&!window.matchMedia('(display-mode: standalone)').matches){
  window.addEventListener('beforeinstallprompt',event=>{event.preventDefault();profileInstallPrompt=event;profileInstallControls.forEach(node=>node.classList.add('pwa--can-install'));});
  window.addEventListener('appinstalled',()=>profileInstallControls.forEach(node=>node.classList.remove('pwa--can-install')));
}
document.addEventListener('click',event=>{
  const button=event.target.closest('.profile-settings [data-controller~="pwa-install"] [data-action~="pwa-install#promptInstall"]');
  if(button&&profileInstallPrompt){event.preventDefault();profileInstallPrompt.prompt();}
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
    const list=newRoomPanel.querySelector('[data-filter-target="list"]');
    const menu=filterInput.closest('[data-controller~="filter"]');
    const activeClass=menu?.dataset.filterActiveClass||'filter--active';
    const selectedClass=menu?.dataset.filterSelectedClass||'selected';
    let filterTimer;
    filterInput.addEventListener('input',()=>{
      clearTimeout(filterTimer);
      filterTimer=setTimeout(()=>{
        list.classList.remove(activeClass);
        list.querySelectorAll(`.${selectedClass}`).forEach(person=>person.classList.remove(selectedClass));
        if(filterInput.value==='')return;
        let matches;
        try{matches=list.querySelectorAll(`[data-value*=${filterInput.value.toLowerCase()}]`);}catch{return;}
        matches.forEach(person=>person.classList.add(selectedClass));
        list.classList.add(activeClass);
      },300);
    });
  }
  newRoomPanel.querySelector('[data-turbo-action="replace"]')?.addEventListener('click',event=>{
    event.preventDefault();
    if(nameInput)sessionStorage.setItem('rustfire-new-room-name',nameInput.value);
    location.href=event.currentTarget.href;
  });
}
const chat = document.querySelector('#message-area');
if(!chat){
  document.addEventListener('click',event=>{
    if(event.target.closest('#direct_rooms_control [data-form-target="cancel"]')){
      event.preventDefault();
      location.href='/';
    }
  });
}
if (chat && document.querySelector('meta[name="current-room-id"]')) {
  const roomId = Number(document.querySelector('meta[name="current-room-id"]')?.content);
  const messages = chat.querySelector('.messages');
  const messageById=id=>messages.querySelector(`.message[data-message-id="${Number(id)}"]`);
  const decorateOwn=()=>{
    messages.querySelectorAll('.message').forEach(node=>{
      const mine=node.dataset.userId===currentUserId;
      node.classList.toggle('own',mine);
      node.classList.toggle('message--me',mine);
      node.classList.add('message--formatted');
      node.classList.toggle('message--mentioned',!!node.querySelector(`.mention img[src^="/users/${currentUserId}/avatar"]`));
    });
    messages.querySelectorAll('.boost-item').forEach(node=>{
      const mine=node.dataset.boostDeleteBoosterIdValue===currentUserId;
      node.classList.toggle('mine',mine);
      const content=node.querySelector('[data-boost-delete-target="content"]');
      if(mine){content?.setAttribute('tabindex','0');content?.setAttribute('aria-describedby','delete_boost_accessible_label');}
      else{content?.removeAttribute('tabindex');content?.removeAttribute('aria-describedby');}
    });
    messages.querySelectorAll('[data-controller~="web-share"]').forEach(node=>{node.hidden=typeof navigator.canShare!=='function';});
    formatReplyLinks(messages);
  };
  const formatMessageGroups=()=>{
    let previous=null,previousDay=null;
    for(const message of messages.querySelectorAll('.message')){
      const time=Number(message.dataset.messageTimestamp);
      const priorTime=previous?Number(previous.dataset.messageTimestamp):NaN;
      const threaded=!!previous&&message.dataset.userId===previous.dataset.userId&&Number.isFinite(time)&&Number.isFinite(priorTime)&&Math.abs(time-priorTime)<=300000;
      message.classList.toggle('threaded',threaded);
      message.classList.toggle('message--threaded',threaded);
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
  const atMessage=location.pathname.match(/\/@(\d+)$/)?.[1]||'';
  let historyMode=!!atMessage;
  if(atMessage)messageById(atMessage)?.scrollIntoView({block:'center'});
  else messages.scrollTop=messages.scrollHeight;
  const returnButton=chat.querySelector('.message-area__return-to-latest');
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
  let lastRefreshAt=Number(messages.dataset.refreshRoomLoadedAtValue)||0;
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
  async function catchUp(reason='connection') {
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
      const refreshed=await fetch(`/rooms/${roomId}/refresh?since=${lastRefreshAt}&reason=${encodeURIComponent(reason)}`,{headers:{Accept:'text/vnd.turbo-stream.html'}});
      if(!refreshed.ok)throw Error(`Room refresh returned ${refreshed.status}`);
      applyRoomStream(await refreshed.text());
      lastRefreshAt=Math.max(lastRefreshAt,...Array.from(messages.querySelectorAll('.message[data-message-updated-at]'),node=>Number(node.dataset.messageUpdatedAt)||0));
      cursor=Number(Array.from(messages.querySelectorAll('.message[data-message-id]')).at(-1)?.dataset.messageId)||cursor;
      if(wasNearBottom)messages.scrollTop=messages.scrollHeight;
      updateReturnButton();
    } catch (error) { console.error('Could not refresh room', error); }
    finally { catchingUp = false; }
  }
  let socket;
  let lastSocketBeat=0;
  let reconnectTimer;
  let offlineTimer=null;
  let hiddenAt=null;
  const fields=document.querySelector('#composer [data-composer-target="fields"]');
  const scheduleOffline=()=>{if(fields.disabled||offlineTimer!==null)return;offlineTimer=setTimeout(()=>{offlineTimer=null;fields.disabled=true;window.dispatchEvent(new CustomEvent('refresh-room:offline'));},5000)};
  const heartbeatConnected=()=>{clearTimeout(offlineTimer);offlineTimer=null;fields.disabled=false;window.dispatchEvent(new CustomEvent('refresh-room:online'));catchUp('connection')};
  scheduleOffline();
  window.addEventListener('online',()=>{if(socket?.readyState===WebSocket.OPEN&&Date.now()-lastSocketBeat>6000)socket.close();else if(socket?.readyState!==WebSocket.OPEN&&socket?.readyState!==WebSocket.CONNECTING){clearTimeout(reconnectTimer);connect()}});
  document.addEventListener('visibilitychange',()=>{
    if(document.hidden){hiddenAt=Date.now();return;}
    if(hiddenAt&&Date.now()-hiddenAt>60000){catchUp('visibility');refreshSidebar();window.dispatchEvent(new CustomEvent('refresh-room:visible'))}
    hiddenAt=null;
  });
  const signedStreamName=chat.querySelector('turbo-cable-stream-source[channel="RoomMessagesChannel"]')?.getAttribute('signed-stream-name');
  const messageIdent=JSON.stringify({channel:'RoomMessagesChannel',signed_stream_name:signedStreamName});
  const typingIdent=JSON.stringify({channel:'TypingNotificationsChannel',room_id:roomId});
  const presenceIdent=JSON.stringify({channel:'PresenceChannel',room_id:roomId});
  const heartbeatIdent=JSON.stringify({channel:'HeartbeatChannel'});
  const unreadIdent=JSON.stringify({channel:'UnreadRoomsChannel'});
  const readIdent=JSON.stringify({channel:'ReadRoomsChannel'});
  const roomListIdent=JSON.stringify({channel:'RoomListChannel'});
  let sidebarStreamIdents=[];
  const sortSidebarList=list=>{
    const items=[...list.querySelectorAll(':scope > [data-sorted-list-target~="item"]')];
    items.sort((a,b)=>a.dataset.sortedListNumber?Number(b.dataset.sortedListNumber)-Number(a.dataset.sortedListNumber):a.dataset.sortedListName.toLowerCase().localeCompare(b.dataset.sortedListName.toLowerCase()));
    items.forEach(item=>list.append(item));
  };
  const markRoom=(rid,unread)=>document.querySelectorAll(`#sidebar a[href='/rooms/${rid}']`).forEach(link=>{
    link.classList.toggle('unread',unread&&rid!==roomId);
    if(unread&&link.parentElement?.id==='direct_rooms'){
      link.dataset.sortedListNumber=String(Date.now());
      sortSidebarList(link.parentElement);
    }
  });
  let sidebarRefreshPending=false,sidebarRefreshAgain=false;
  async function refreshSidebar(){
    if(sidebarRefreshPending){sidebarRefreshAgain=true;return;}
    sidebarRefreshPending=true;
    try{
      const response=await fetch(`/users/me/sidebar?active=${roomId}`);
      if(!response.ok)return;
      const html=await response.text();
      const replacement=new DOMParser().parseFromString(html,'text/html').querySelector('#user_sidebar');
      const current=document.querySelector('#sidebar #user_sidebar');
      if(!replacement||!current)return;
      current.replaceWith(replacement);
      const freshIdents=[...replacement.querySelectorAll('turbo-cable-stream-source[channel="Turbo::StreamsChannel"]')].map(source=>JSON.stringify({channel:'Turbo::StreamsChannel',signed_stream_name:source.getAttribute('signed-stream-name')}));
      if(socket?.readyState===WebSocket.OPEN)for(const identifier of freshIdents)if(!sidebarStreamIdents.includes(identifier))socket.send(JSON.stringify({command:'subscribe',identifier}));
      sidebarStreamIdents=freshIdents;
      if(response.headers.get('x-rustfire-active-room-accessible')==='0')location.href='/';
    }catch(error){console.error('Could not refresh room list',error)}
    finally{sidebarRefreshPending=false;if(sidebarRefreshAgain){sidebarRefreshAgain=false;refreshSidebar()}}
  }
  document.addEventListener('click',async event=>{
    const newPing=event.target.closest('#sidebar .direct__new');
    if(newPing){
      event.preventDefault();
      try{
        const response=await fetch(newPing.href,{headers:{'Turbo-Frame':'direct_rooms_control'}});
        if(!response.ok)throw Error(`Ping form returned ${response.status}`);
        const frame=new DOMParser().parseFromString(await response.text(),'text/html').querySelector('#direct_rooms_control');
        if(!frame)throw Error('Ping form frame missing');
        document.querySelector('#sidebar #direct_rooms_control')?.replaceWith(frame);
        initPingForm();
      }catch(error){console.error('Could not open ping form',error);location.href=newPing.href}
      return;
    }
    const cancelPing=event.target.closest('#sidebar #direct_rooms_control [data-form-target="cancel"]');
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
    sortSidebarList(nav);
    if(sidebarRefreshPending)sidebarRefreshAgain=true;
    return true;
  }
  function applySidebarStream(html){
    const parsed=new DOMParser().parseFromString(html,'text/html');
    for(const stream of parsed.querySelectorAll('turbo-stream')){
      const target=document.getElementById(stream.getAttribute('target'));
      if(!target||!target.closest('#sidebar'))continue;
      const action=stream.getAttribute('action');
      if(action==='remove'){target.remove();continue;}
      const fragment=stream.querySelector('template')?.content.cloneNode(true);
      if(!fragment)continue;
      for(const link of fragment.querySelectorAll('a[id^="list_rooms_"]')){
        const existing=link.id&&document.getElementById(link.id);
        if(existing&&existing!==target)existing.remove();
        link.classList.toggle('active',link.getAttribute('href')===`/rooms/${roomId}`);
      }
      const list=target.closest('#direct_rooms,#shared_rooms')||target;
      if(action==='prepend')target.prepend(fragment);
      else if(action==='replace')target.replaceWith(fragment);
      if(list.id==='direct_rooms'||list.id==='shared_rooms')sortSidebarList(list);
    }
  }
  const typingIndicator=document.querySelector('[data-typing-notifications-target="indicator"]');
  const typingPeople=new Map();
  const renderTyping=()=>{const names=[...typingPeople.keys()].sort();typingIndicator.classList.toggle('typing-indicator--active',!!names.length);typingIndicator.querySelector('[data-typing-notifications-target="author"]').textContent=names.join(', ');};
  setInterval(()=>{const cutoff=Date.now()-5000;for(const [name,started] of typingPeople)if(started<=cutoff)typingPeople.delete(name);renderTyping();},1000);
  function typingFrame(data) {
    if(!data?.user || data.user.id===Number(currentUserId))return;
    if(data.action==='start')typingPeople.set(data.user.name,Date.now());
    else typingPeople.delete(data.user.name);
    renderTyping();
  }
  const sendTyping=action=>{if(socket?.readyState===WebSocket.OPEN)socket.send(JSON.stringify({command:'message',identifier:typingIdent,data:JSON.stringify({action})}));};
  const sendPresence=action=>{if(socket?.readyState===WebSocket.OPEN)socket.send(JSON.stringify({command:'message',identifier:presenceIdent,data:JSON.stringify({action})}));};
  let presenceConnected=false;
  let presenceWasVisible=true;
  let presenceRefreshTimer=null;
  const startPresenceRefresh=()=>{if(presenceRefreshTimer===null)presenceRefreshTimer=setInterval(()=>sendPresence('refresh'),50000)};
  const stopPresenceRefresh=()=>{clearInterval(presenceRefreshTimer);presenceRefreshTimer=null};
  let visibilityTimer;
  document.addEventListener('visibilitychange',()=>{
    clearTimeout(visibilityTimer);
    visibilityTimer=setTimeout(()=>{
      if(!presenceConnected)return;
      if(document.hidden&&presenceWasVisible){stopPresenceRefresh();sendPresence('absent');presenceWasVisible=false}
      else if(!document.hidden&&!presenceWasVisible){sendPresence('present');startPresenceRefresh();presenceWasVisible=true;markRoom(roomId,false)}
    },5000);
  });
  function applyRoomStream(html){
    const streamDocument=new DOMParser().parseFromString(html,'text/html');
    for(const stream of streamDocument.querySelectorAll('turbo-stream')){
      const targetId=stream.getAttribute('target');
      const target=targetId&&document.getElementById(targetId);
      if(!target||(target!==messages&&!messages.contains(target)))continue;
      const keepScroll=target!==messages&&stream.hasAttribute('maintain_scroll');
      const previousTop=messages.scrollTop,previousHeight=messages.scrollHeight;
      const aboveFold=keepScroll&&target.getBoundingClientRect().top<messages.clientHeight;
      const restoreScroll=()=>{if(keepScroll)messages.scrollTop=previousTop+(aboveFold?messages.scrollHeight-previousHeight:0)};
      const action=stream.getAttribute('action');
      if(action==='remove'){
        if(target!==messages){target.remove();formatMessageGroups();restoreScroll();}
        continue;
      }
      const fragment=stream.querySelector('template')?.content.cloneNode(true);
      if(!fragment)continue;
      if(action==='append'){
        if(target===messages&&historyMode){updateReturnButton();continue;}
        const scrollToLatest=target===messages&&(nearBottom()||[...fragment.querySelectorAll('.message')].some(node=>node.dataset.userId===currentUserId));
        const replacedMessages=[];
        for(const node of [...fragment.children])if(node.id){
          const existing=document.getElementById(node.id);
          if(existing){
            if(existing.classList.contains('message')&&!existing.dataset.messageId){existing.replaceWith(node);replacedMessages.push(node)}
            else node.remove();
          }
        }
        if(!fragment.childNodes.length&&!replacedMessages.length)continue;
        const addedMessages=[...fragment.querySelectorAll('.message[data-message-id]'),...replacedMessages.filter(node=>node.matches('.message[data-message-id]'))];
        if(fragment.childNodes.length)target.append(fragment);
        if(target===messages){
          formatLocalTimes(messages);decorateOwn();formatMessageGroups();
          if(!catchingUp)for(const node of addedMessages)cursor=Math.max(cursor,Number(node.dataset.messageId));
          if(scrollToLatest)messages.scrollTop=messages.scrollHeight;
          updateReturnButton();
          if(!catchingUp)for(const node of addedMessages){const sound=node.querySelector('.sound[data-sound-url-value]');if(sound)new Audio(sound.dataset.soundUrlValue).play().catch(()=>{});}
        }else{decorateOwn();restoreScroll();}
      }else if(action==='replace'&&target!==messages){
        if(target.closest('.composer--edit'))continue;
        target.replaceWith(fragment);
        formatLocalTimes(messages);decorateOwn();formatMessageGroups();
        restoreScroll();
      }
    }
  }
  function connect() {
    if(socket?.readyState===WebSocket.OPEN||socket?.readyState===WebSocket.CONNECTING)return;
    const connection=socket=new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/cable`, 'actioncable-v1-json');
    const monitor=setInterval(()=>{if(connection.readyState===WebSocket.OPEN&&Date.now()-lastSocketBeat>6000)connection.close()},1000);
    connection.addEventListener('open', () => { lastSocketBeat=Date.now();catchingUp = false; for(const identifier of [messageIdent,typingIdent,presenceIdent,heartbeatIdent,unreadIdent,readIdent,roomListIdent,...sidebarStreamIdents])connection.send(JSON.stringify({command:'subscribe',identifier})); });
    connection.addEventListener('message', e => {
      lastSocketBeat=Date.now();
      try { const frame = JSON.parse(e.data); if (frame.type === 'confirm_subscription') { if(frame.identifier===heartbeatIdent)heartbeatConnected(); if(frame.identifier===presenceIdent){presenceConnected=true;startPresenceRefresh();markRoom(roomId,false)} if(frame.identifier===roomListIdent)refreshSidebar(); return; } const data = frame.message; if(frame.identifier===messageIdent){if(typeof data==='string')applyRoomStream(data);return;} if(sidebarStreamIdents.includes(frame.identifier)){if(typeof data==='string')applySidebarStream(data);return;} if(frame.identifier===typingIdent){typingFrame(data);return;} if(frame.identifier===unreadIdent){markRoom(data?.roomId,true);return;} if(frame.identifier===readIdent){markRoom(data?.room_id,false);return;} if(frame.identifier===roomListIdent){if(data?.type!=='direct_room_added'||!updateDirectRoom(data))refreshSidebar();return;}
      } catch {}
    });
    connection.addEventListener('close', () => {clearInterval(monitor);if(socket!==connection)return;socket=null;presenceConnected=false;stopPresenceRefresh();scheduleOffline();reconnectTimer=setTimeout(connect,1500);});
  }
  refreshSidebar();
  connect();
  document.addEventListener('click',event=>{if(event.target.closest('[data-toggle-sidebar], .sidebar__toggle'))document.querySelector('#sidebar')?.classList.toggle('open')});
  const composer=document.getElementById('composer');
  composer.querySelector('[name="message[client_message_id]"]').value=crypto.randomUUID();
  async function restoreMessage(article){
    const response=await fetch(`/rooms/${roomId}/messages/${article.dataset.messageId}`,{headers:{'X-Rustfire-Fragment':'1'}});
    if(!response.ok)throw Error('Could not load message');
    article.outerHTML=await response.text();
    formatLocalTimes(messages);decorateOwn();formatMessageGroups();
  }
  submitMessageEdit=async editForm=>{
    const article=editForm.closest('.message');
    const body=new URLSearchParams(new FormData(editForm));body.set('message[format]','html');
    try{
      // Campfire redirects HTML edits; reload the message instead of following that navigation.
      const response=await fetch(editForm.action,{method:'PATCH',body,redirect:'manual',headers:{Accept:'text/html','X-CSRF-Token':csrfToken}});
      if(response.status!==302&&response.type!=='opaqueredirect')throw Error('Could not save message');
      await restoreMessage(document.getElementById(article.id)||article);
    }catch{alert('Could not save or reload message')}
  };
  const typingInput=composer.querySelector('trix-editor');
  const bodyInput=composer.querySelector('[name="message[body]"]');
  const mentionBox=document.createElement('div');
  mentionBox.className='mention-suggestions';mentionBox.id='mention-suggestions';mentionBox.setAttribute('role','listbox');mentionBox.setAttribute('aria-label','Mention a person');mentionBox.hidden=true;
  typingInput.parentElement.append(mentionBox);typingInput.setAttribute('aria-controls',mentionBox.id);
  let mentionOptions=[],mentionRange=null,mentionSelected=0,mentionGeneration=0,mentionTimer,ignoreNextMentionChange=false;
  const hideMentions=()=>{mentionGeneration++;clearTimeout(mentionTimer);mentionOptions=[];mentionRange=null;mentionBox.hidden=true;mentionBox.replaceChildren();typingInput.removeAttribute('aria-activedescendant');};
  const markMention=()=>{[...mentionBox.children].forEach((button,index)=>{button.classList.toggle('selected',index===mentionSelected);button.setAttribute('aria-selected',String(index===mentionSelected));});typingInput.setAttribute('aria-activedescendant',`mention-option-${mentionSelected}`);};
  const chooseMention=(person)=>{if(!mentionRange||!typingInput.editor||typeof person.sgid!=='string')return;const span=document.createElement('span');span.className='mention';span.setAttribute('sgid',person.sgid);const avatar=document.createElement('img');avatar.src=person.avatar_url;avatar.className='avatar';avatar.alt=person.name;span.append(avatar,document.createTextNode(person.name));typingInput.editor.setSelectedRange(mentionRange);typingInput.editor.insertAttachment(new Trix.Attachment({content:span.outerHTML,contentType:'application/vnd.campfire.mention',sgid:person.sgid}));typingInput.editor.insertString(' ');ignoreNextMentionChange=true;hideMentions();typingInput.focus();};
  const refreshMentions=()=>{if(ignoreNextMentionChange){ignoreNextMentionChange=false;return;}const editor=typingInput.editor;if(!editor)return;const position=editor.getPosition();const before=editor.getDocument().toString().slice(0,position);const match=before.match(/(?:^|\s)@([^@\n]{0,32})$/);if(!match){hideMentions();return;}const query=match[1].trim();mentionRange=[position-match[1].length-1,position];const generation=++mentionGeneration;clearTimeout(mentionTimer);mentionTimer=setTimeout(async()=>{try{const response=await fetch(`/autocompletable/users?room_id=${roomId}&query=${encodeURIComponent(query)}`);if(!response.ok||generation!==mentionGeneration)return;const people=(await response.json()).map(person=>({...person,name:decodeAutocompleteName(person.name)}));if(generation!==mentionGeneration)return;mentionOptions=people.filter(person=>person.value!==Number(currentUserId));mentionSelected=0;mentionBox.replaceChildren();for(const [index,person] of mentionOptions.entries()){const button=document.createElement('button');button.type='button';button.id=`mention-option-${index}`;button.setAttribute('role','option');button.textContent=person.name;button.addEventListener('mousedown',event=>event.preventDefault());button.addEventListener('click',()=>chooseMention(person));mentionBox.append(button);}mentionBox.hidden=!mentionOptions.length;if(mentionOptions.length)markMention();}catch{hideMentions();}},120);};
  typingInput.addEventListener('trix-change',refreshMentions);
  let unfurlController=null,unfurlFrame=null;
  typingInput.addEventListener('trix-paste',(event)=>{
    const range=event.paste?.range;
    const editor=typingInput.editor;
    if(!range||!editor)return;
    const url=editor.getDocument().getStringAtRange(range).trim();
    if(!/^(?:[a-z0-9]+:\/\/|www\.)[^\s]+$/.test(url))return;
    if(typingInput.hasAttribute('data-permitted-attributes')&&!typingInput.getAttribute('data-permitted-attributes').split(' ').includes('href'))return;
    if(!typingInput.getAttribute('data-permitted-attachment-types')?.includes('application/vnd.actiontext.opengraph-embed'))return;
    unfurlController?.abort();if(unfurlFrame!==null)cancelAnimationFrame(unfurlFrame);
    const controller=new AbortController();unfurlController=controller;
    unfurlFrame=requestAnimationFrame(async()=>{try {
      const response=await fetch('/unfurl_link',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify({url}),signal:controller.signal});
      if(!response.ok||response.status===204)return;
      const {title,description,image,url:canonical}=await response.json();
      if(!editor.getDocument().toString().includes(url))return;
      if(!image)return;
      const embed=document.createElement('actiontext-opengraph-embed');
      embed.className=image.startsWith('https://pbs.twimg.com/profile_images')?'cf-twitter-avatar':'';
      const wrapper=document.createElement('div');wrapper.className='og-embed';
      const content=document.createElement('div');content.className='og-embed__content';
      const heading=document.createElement('div');heading.className='og-embed__title';heading.textContent=title.length>560?title.slice(0,559)+'…':title;
      const text=document.createElement('div');text.className='og-embed__description';text.textContent=description.length>560?description.slice(0,559)+'…':description;
      content.append(heading,text);wrapper.append(content);
      const imageBox=document.createElement('div');imageBox.className='og-embed__image';
      const img=document.createElement('img');img.src=image;img.className='image';img.alt='';imageBox.append(img);
      wrapper.append(imageBox);embed.append(wrapper);
      const prior=editor.getSelectedRange();
      editor.recordUndoEntry('Insert Opengraph preview for Pasted URL');
      editor.insertAttachment(new Trix.Attachment({contentType:'application/vnd.actiontext.opengraph-embed',content:embed.outerHTML,filename:title,href:canonical,url:image,caption:description}));
      editor.setSelectedRange(prior);
    } catch(error) { if(error?.name!=='AbortError')console.debug('Link preview unavailable',error); }
    finally { if(unfurlController===controller){unfurlController=null;unfurlFrame=null;} }
    });
  });
  typingInput.addEventListener('keydown',event=>{if(mentionBox.hidden)return;if(event.key==='Escape'){event.preventDefault();hideMentions();return;}if(event.key==='ArrowDown'||event.key==='ArrowUp'){event.preventDefault();mentionSelected=(mentionSelected+(event.key==='ArrowDown'?1:-1)+mentionOptions.length)%mentionOptions.length;markMention();return;}if(event.key==='Enter'||event.key==='Tab'){event.preventDefault();chooseMention(mentionOptions[mentionSelected]);}},true);
  typingInput.addEventListener('blur',()=>setTimeout(()=>{if(!mentionBox.contains(document.activeElement))hideMentions();},150));
  const richToggle=composer.querySelector('.composer__rich-text-btn');
  richToggle.setAttribute('aria-expanded','false');
  richToggle.addEventListener('click',()=>{const opened=composer.classList.toggle('composer--rich-text');richToggle.setAttribute('aria-expanded',String(opened));typingInput.focus();});
  typingInput.addEventListener('keydown',event=>{
    if(event.defaultPrevented||!mentionBox.hidden)return;
    const touchDevice='ontouchstart' in window||navigator.maxTouchPoints>0||navigator.msMaxTouchPoints>0;
    const modifiedEnter=event.key==='Enter'&&(event.metaKey||event.ctrlKey);
    const plainEnter=event.key==='Enter'&&!event.shiftKey&&!event.isComposing;
    if(touchDevice||!(modifiedEnter||(plainEnter&&!composer.classList.contains('composer--rich-text'))))return;
    event.preventDefault();
    if(composer.querySelector('[data-composer-target="fields"]').disabled)return;
    composer.requestSubmit();
    composer.classList.remove('composer--rich-text');
    richToggle.setAttribute('aria-expanded','false');
    typingInput.focus();
  });
  document.addEventListener('keydown',event=>{
    if(event.key!=='ArrowUp'||historyMode||!typingInput.matches(':empty'))return;
    const mine=messages.querySelectorAll('.message--me');
    mine[mine.length-1]?.querySelector('.message__edit-btn')?.click();
  });
  let lastTypingSent=0;
  typingInput.addEventListener('trix-change',()=>{if(typingInput.value){if(Date.now()-lastTypingSent>=1000){sendTyping('start');lastTypingSent=Date.now();}}else sendTyping('stop');});
  const fileInput=composer.querySelector('input[type=file]');
  const fileList=composer.querySelector('[data-composer-target="fileList"]');
  const queuedFiles=[];
  const renderQueuedFiles=()=>{
    fileList.replaceChildren();
    queuedFiles.forEach((entry,index)=>{
      const card=document.createElement('button');card.type='button';card.setAttribute('style','gap: 0');card.className='btn btn--plain composer__file txt-normal position-relative unpad flex-column';card.dataset.action='composer#fileUnpicked';card.dataset.composerIndexParam=String(index);
      const thumb=entry.url?document.createElement('img'):document.createElement('span');
      thumb.className=entry.url?'flex-item-no-shrink composer__file-thumbnail':'composer__file-thumbnail composer__file-thumbnail--common colorize--black';
      if(entry.url){thumb.src=entry.url;thumb.setAttribute('role','presentation')}
      const caption=document.createElement('span');caption.className='pad-inline txt-small flex align-center max-width composer__file-caption';
      const basename=document.createElement('span');basename.className='overflow-ellipsis';
      const extension=document.createElement('span');extension.className='flex-item-no-shrink';
      const parts=entry.file.name.split('.');extension.textContent=parts.pop();basename.textContent=parts.length?`${parts.join('.')}.`:'';
      caption.append(basename,extension);card.append(thumb,caption);
      card.addEventListener('click',()=>{if(entry.url)URL.revokeObjectURL(entry.url);queuedFiles.splice(index,1);renderQueuedFiles()});
      fileList.append(card);
    });
  };
  const addFiles=files=>{
    for(const file of files)queuedFiles.push({file,url:file.type.startsWith('image/')?URL.createObjectURL(file):null});
    queuedFiles.sort((a,b)=>a.file.name.localeCompare(b.file.name));
    renderQueuedFiles();
  };
  fileInput.addEventListener('change',()=>{addFiles(fileInput.files);fileInput.value=''});
  composer.addEventListener('paste',event=>{if(event.clipboardData?.files?.length){event.preventDefault();addFiles(event.clipboardData.files)}});
  for(const target of [chat,composer]){
    target.addEventListener('dragenter',event=>event.preventDefault());
    target.addEventListener('dragover',event=>{event.preventDefault();if(event.dataTransfer)event.dataTransfer.dropEffect='copy'});
    target.addEventListener('drop',event=>{
      event.preventDefault();
      target.dispatchEvent(new CustomEvent('drop-target:drop',{bubbles:true,cancelable:true,detail:{files:event.dataTransfer?.files}}));
    });
  }
  window.addEventListener('drop-target:drop',event=>{if(event.detail.files?.length)addFiles(event.detail.files)});
  const pendingTemplate=chat.querySelector('script[data-messages-target="template"]')?.innerHTML;
  const escapeHtml=value=>{const span=document.createElement('span');span.textContent=value;return span.innerHTML};
  const pendingUpload=(filename,percent=0)=>`<div class="message__pending-upload flex align-center gap" style="--percentage: ${percent}%"><div class="composer__file-thumbnail composer__file-thumbnail--common colorize--black borderless flex-item-no-shrink"></div><div>${escapeHtml(filename)} - <span>${percent}%</span></div></div>`;
  const insertPending=(id,body,plainText='')=>{
    if(!pendingTemplate)return;
    const now=new Date();
    const values={clientMessageId:id,body,messageTimestamp:String(now.getTime()),messageDatetime:now.toISOString(),messageClasses:/^(\p{Emoji_Presentation}|\p{Extended_Pictographic}|\uFE0F)+$/u.test(plainText)?'message--emoji':''};
    let html=pendingTemplate;
    for(const [key,value] of Object.entries(values))html=html.replaceAll(`$${key}$`,value);
    messages.insertAdjacentHTML('beforeend',html);
    formatLocalTimes(messages);decorateOwn();formatMessageGroups();messages.scrollTop=messages.scrollHeight;
  };
  const failPending=id=>document.getElementById(`message_${id}`)?.classList.add('message--failed');
  const updatePending=(id,body)=>{const content=document.getElementById(`message_${id}`)?.querySelector('.message__body-content');if(content)content.innerHTML=body};
  const postPending=async(id,body)=>{
    const response=await fetch(composer.action,{method:'POST',body,headers:{Accept:'text/vnd.turbo-stream.html','X-CSRF-Token':csrfToken}});
    if(!response.ok)throw Error(`Message returned ${response.status}`);
    applyRoomStream(await response.text());
  };
  const uploadPending=(id,file)=>new Promise((resolve,reject)=>{
    const body=new FormData();body.append('message[attachment]',file,file.name);body.append('message[client_message_id]',id);
    const request=new XMLHttpRequest();request.open('POST',composer.action);request.setRequestHeader('X-CSRF-Token',csrfToken);request.setRequestHeader('Accept','text/vnd.turbo-stream.html');
    request.upload.addEventListener('progress',event=>{if(event.lengthComputable)updatePending(id,pendingUpload(file.name,Math.round(event.loaded/event.total*100)))});
    request.addEventListener('load',()=>{if(request.status>=200&&request.status<400){applyRoomStream(request.responseText);resolve()}else reject(Error(`Upload returned ${request.status}`))});
    request.addEventListener('error',()=>reject(Error('Upload failed')));request.send(body);
  });
  let sending=false;
  composer.addEventListener('submit', async e => {
    e.preventDefault(); const form=e.currentTarget;
    if(sending||(!typingInput.editor?.getDocument().toString().trim()&&!queuedFiles.length))return;
    sending=true;
    const sendingFiles=queuedFiles.splice(0);renderQueuedFiles();
    sendTyping('stop');
    const fileTask=(async()=>{for(const entry of sendingFiles){
      const id=crypto.randomUUID();insertPending(id,pendingUpload(entry.file.name));
      try{await uploadPending(id,entry.file)}catch(error){failPending(id);throw error}
      finally{if(entry.url)URL.revokeObjectURL(entry.url)}
    }})();
    const textTask=(async()=>{
      const plain=typingInput.textContent.trim();if(!plain)return;
      const id=crypto.randomUUID();const html=typingInput.innerHTML;
      insertPending(id,`<div class="trix-content">${html}</div>`,plain);
      const body=new FormData(form);body.delete('message[attachment]');body.set('message[client_message_id]',id);
      bodyInput.value='';typingInput.editor.loadHTML('');form.querySelector('[name="message[client_message_id]"]').value=crypto.randomUUID();
      try{await postPending(id,body)}catch(error){failPending(id);throw error}
    })();
    const outcomes=await Promise.allSettled([fileTask,textTask]);
    sending=false;
    if(historyMode)location.href=`/rooms/${roomId}`;
    const failed=outcomes.find(result=>result.status==='rejected');if(failed)alert(failed.reason.message||'Could not send message');
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
      centerLoadedForm(returned);
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
      centerLoadedForm(frame);
      frame.querySelector('[name="boost[content]"]')?.focus();
      return;
    }
    const sound=e.target.closest('[data-action~="sound#play"]')?.closest('.sound[data-sound-url-value]'); if(sound) { new Audio(sound.dataset.soundUrlValue).play().catch(()=>{}); return; }
    const reply=e.target.closest('[data-action~="reply#reply"]');
    if(reply){
      const article=reply.closest('.message');
      const body=article?.querySelector('[data-reply-target="body"] .trix-content')?.cloneNode(true);
      const author=article?.querySelector('[data-reply-target="author"]');
      const original=article?.querySelector('[data-reply-target="link"]');
      if(!body||!author||!original||!typingInput.editor)return;
      const preview=body.querySelector('.og-embed a')?.href;
      body.querySelectorAll('.og-embed').forEach(node=>node.remove());
      body.querySelectorAll('.mention').forEach(node=>node.replaceWith(document.createTextNode(node.textContent.trim())));
      if(preview&&!body.textContent.trim())body.textContent=preview;
      const block=document.createElement('blockquote');block.innerHTML=body.innerHTML;
      const cite=document.createElement('cite');cite.innerHTML=author.innerHTML+' ';
      const link=document.createElement('a');link.href=original.href;link.textContent='#';cite.append(link);
      const editor=typingInput.editor;
      editor.recordUndoEntry('Format reply');
      editor.setSelectedRange([0,editor.getDocument().toString().length]);
      editor.deleteInDirection('forward');
      editor.insertHTML(block.outerHTML+cite.outerHTML+'<br>');
      editor.setSelectedRange([editor.getDocument().toString().length-1]);
      typingInput.focus();reply.closest('details')?.removeAttribute('open');return;
    }
  });
  messages.addEventListener('submit', async e => {
    const deleteForm=e.target.closest('form[id^="delete_form_message_"]');
    if(deleteForm){
      e.preventDefault();
      const response=await fetch(deleteForm.action,{method:'DELETE',headers:{'X-CSRF-Token':csrfToken}});
      if(response.ok){deleteForm.closest('.message').remove();formatMessageGroups()}
      else alert('Could not delete message');
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
    const form=e.target.closest('form.boost__form,form.custom-boost-form');if(!form)return;
    e.preventDefault();
    const response=await fetch(form.action,{method:'POST',body:new URLSearchParams(new FormData(form)),headers:{'X-CSRF-Token':csrfToken}});
    if(response.ok){
      const target=document.getElementById(form.dataset.turboFrame);
      const updated=new DOMParser().parseFromString(await response.text(),'text/html').getElementById(form.dataset.turboFrame);
      if(target&&updated){target.replaceWith(updated);decorateOwn();}
      else{form.reset();const frame=form.closest('turbo-frame');if(frame?.dataset.originalHtml){frame.innerHTML=frame.dataset.originalHtml;delete frame.dataset.originalHtml;}}
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
  const installControl=dialog?.querySelector('.pwa__instructions[data-controller~="pwa-install"]');
  let installPrompt=null;
  if(installControl&&'serviceWorker' in navigator&&!window.matchMedia('(display-mode: standalone)').matches){
    window.addEventListener('beforeinstallprompt',event=>{
      event.preventDefault();
      installPrompt=event;
      installControl.classList.add('pwa--can-install');
    });
    window.addEventListener('appinstalled',()=>installControl.classList.remove('pwa--can-install'));
  }
  const showHelp=()=>{
    if(dialog instanceof HTMLDialogElement&&!dialog.open){
      dialog.showModal();
      const visible=[...dialog.querySelectorAll('[data-notifications-target="details"]')].filter(item=>item.getClientRects().length);
      if(visible.length===1)visible[0].open=true;
    }
  };
  const firstRunCookie=()=>window.matchMedia('(display-mode: standalone)').matches?'notifications-pwa-first-run-seen':'notifications-first-run-seen';
  const pulseBell=()=>{
    if(!document.cookie.split('; ').some(cookie=>cookie.startsWith(`${firstRunCookie()}=`)))roomBell?.classList.add('btn--pulsing');
  };
  const showAlert=()=>{
    roomBell?.querySelectorAll('img').forEach(image=>image.hidden=!image.hidden);
  };
  const markSeen=()=>{
    roomBell?.classList.remove('btn--pulsing');
    document.cookie=`${firstRunCookie()}=true; path=/; expires=${new Date(Date.now()+20*365*24*60*60*1000).toUTCString()}`;
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
    if(!('serviceWorker' in navigator)||!('Notification' in window))return false;
    const registration=await navigator.serviceWorker.getRegistration(window.location.origin);
    const existingSubscription=await registration?.pushManager?.getSubscription();
    return Notification.permission==='granted'&&!!registration&&!!existingSubscription;
  };
  hasSubscription().then(enabled=>{pulseBell();return enabled?loadFrame():showAlert()}).catch(showAlert);
  const subscribe=async()=>{
    if(!('serviceWorker' in navigator)||!('Notification' in window))return false;
    const registration=await navigator.serviceWorker.getRegistration(window.location.origin)||await navigator.serviceWorker.register('/service-worker.js');
    if(Notification.permission==='denied')return false;
    const permission=Notification.permission==='granted'?'granted':await Notification.requestPermission();
    if(permission!=='granted')return null;
    const key=document.querySelector('meta[name="vapid-public-key"]')?.content||'';
    const padded=(key+'='.repeat((4-key.length%4)%4)).replace(/-/g,'+').replace(/_/g,'/');
    const subscription=await registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:Uint8Array.from(atob(padded),char=>char.charCodeAt(0))});
    const {endpoint,keys:{p256dh,auth}}=subscription.toJSON();
    fetch('/users/me/push_subscriptions',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify({push_subscription:{endpoint,p256dh_key:p256dh,auth_key:auth}})}).then(response=>{
      if(!response.ok)subscription.unsubscribe();
    });
    return true;
  };
  roomBell?.addEventListener('click',async()=>{
    roomBell.disabled=true;
    markSeen();
    try{
      const subscribed=await subscribe();
      if(subscribed)await loadFrame();
      else if(subscribed===false)showHelp();
    }catch(error){showHelp()}
    finally{roomBell.disabled=false}
  });
  notificationsControl.addEventListener('click',async event=>{
    const button=event.target.closest('[data-action="pwa-install#promptInstall"]');
    if(!button||!installControl?.contains(button)||!installPrompt)return;
    await installPrompt.prompt();
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
document.addEventListener('change',event=>{
  const control=event.target;
  if(control instanceof HTMLInputElement){
    if(control.matches('form[data-auto-submit-file] input[type=file]')&&control.files?.length)control.form.requestSubmit();
    if(control.matches('form[data-auto-submit-switch] input[type=checkbox],input[data-action~="change->form#submit"]'))control.form.requestSubmit();
    if(control.matches('#account_users input[data-action="form#submit"][name="user[role]"]'))control.form.requestSubmit();
  }
});
document.addEventListener('submit',event=>{
  const confirmation=event.submitter?.getAttribute('data-turbo-confirm');
  if(confirmation&&!window.confirm(confirmation)){event.preventDefault();event.stopImmediatePropagation();}
},true);
async function unsubscribeOnLogout(form){
  if(!('serviceWorker' in navigator))return;
  const registration=await navigator.serviceWorker.getRegistration(window.location.origin);
  const subscription=await registration?.pushManager?.getSubscription();
  if(subscription){
    let field=form.querySelector('[name="push_subscription_endpoint"]');
    if(!field){field=document.createElement('input');field.type='hidden';field.name='push_subscription_endpoint';form.append(field)}
    field.value=subscription.endpoint;
    await subscription.unsubscribe();
  }
}
document.addEventListener('click',async event=>{
  const button=event.target.closest('button[data-action~="sessions#logout:prevent"]');
  const form=button?.closest('form[data-controller~="sessions"]');
  if(!form)return;
  event.preventDefault();
  await unsubscribeOnLogout(form);
  form.requestSubmit();
});
document.addEventListener('submit',async event=>{
  const form=event.target;
  if(!(form instanceof HTMLFormElement)||new URL(form.action).pathname!=='/session/logout')return;
  event.preventDefault();
  try{await unsubscribeOnLogout(form)}catch{}
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
