async function api(path,opts={}){
  // Strip leading slash so URL resolves relative to location.href (supports subpath mounts)
  const rel = path.startsWith('/') ? path.slice(1) : path;
  const url=new URL(rel,document.baseURI||location.href);
  const timeoutMs=Object.prototype.hasOwnProperty.call(opts,'timeoutMs')?opts.timeoutMs:30000;
  const timeoutToast=opts.timeoutToast!==false;
  const redirect401=opts.redirect401!==false;
  // Retry up to 2 times on network errors (e.g. stale keep-alive after long idle).
  // Server errors (4xx/5xx) and client-side timeouts are NOT retried.
  let lastErr;
  for(let attempt=0;attempt<3;attempt++){
    let controller=null;
    let timeoutId=null;
    let didTimeout=false;
    let upstreamSignal=null;
    let upstreamAbort=null;
    try{
      const fetchOpts={...opts};
      delete fetchOpts.timeoutMs;
      delete fetchOpts.timeoutToast;
      delete fetchOpts.redirect401;

      const useTimeout=Number.isFinite(Number(timeoutMs))&&Number(timeoutMs)>0;
      if(useTimeout&&typeof AbortController!=='undefined'){
        controller=new AbortController();
        upstreamSignal=fetchOpts.signal||null;
        if(upstreamSignal){
          upstreamAbort=()=>controller.abort(upstreamSignal.reason);
          if(upstreamSignal.aborted) upstreamAbort();
          else upstreamSignal.addEventListener('abort',upstreamAbort,{once:true});
        }
        fetchOpts.signal=controller.signal;
      }
      const requestPromise=(async()=>{
        const res=await fetch(url.href,{credentials:'include',headers:{'Content-Type':'application/json'},...fetchOpts});
        if(!res.ok){
          // 401 means the auth session expired. Redirect to login so the user can
          // re-authenticate. This is especially important for iOS PWA (standalone mode)
          // and for subpath mounts like /hermes/, where /login escapes to the site root.
if(res.status===401){
            if(redirect401){
              const _p=(window.location.pathname||'').replace(/\/+$/,'');
              if(/(?:^|\/)login$/.test(_p)){
                window.location.href='login';
              }else{
                window.location.href='login?next='+encodeURIComponent(window.location.pathname+window.location.search);
              }
            }
            return;
          }
          const text=await res.text();
          // Parse JSON error body and surface the human-readable message,
          // rather than showing raw JSON like {"error":"Profile 'x' does not exist."}
          let message=text;
          try{const j=JSON.parse(text);message=j.error||j.message||text;}catch(e){}
          // Attach the raw HTTP context so callers can branch on status (404 stale-session
          // cleanup, 401 redirect, 503 retry, etc.) without re-parsing the message string.
          const err=new Error(message);
          err.status=res.status;
          err.statusText=res.statusText;
          err.body=text;
          throw err;
        }
        const ct=res.headers.get('content-type')||'';
        return ct.includes('application/json')?await res.json():await res.text();
      })();
      return useTimeout?await Promise.race([
        requestPromise,
        new Promise((_,reject)=>{
          timeoutId=setTimeout(()=>{
            didTimeout=true;
            if(controller) controller.abort();
            const err=new Error('Request timed out. Please try again.');
            err.name='TimeoutError';
            err.timeout=true;
            reject(err);
          },Number(timeoutMs));
        })
      ]):await requestPromise;
    }catch(e){
      lastErr=e;
      const isTimeout=didTimeout||(e&&(e.timeout===true||e.name==='TimeoutError'));
      if(isTimeout){
        const err=(e&&e.name==='TimeoutError')?e:new Error('Request timed out. Please try again.');
        err.name='TimeoutError';
        err.timeout=true;
        if(timeoutToast&&typeof showToast==='function') showToast('Request timed out. Please try again.',5000,'error');
        throw err;
      }
      // Only retry on network errors (TypeError from fetch), not on HTTP errors
      // that were already thrown above. Re-throw 401 redirects immediately.
      if(e.message&&/401/.test(e.message)) throw e;
      if(attempt<2 && e instanceof TypeError) continue;
      throw e;
    }finally{
      if(timeoutId) clearTimeout(timeoutId);
      if(upstreamSignal&&upstreamAbort) upstreamSignal.removeEventListener('abort',upstreamAbort);
    }
  }
  throw lastErr;
}

function recordClientSSEError(source, details={}){
  try{
    const payload={
      event:'sse_error',
      source:String(source||'unknown'),
      ready_state:details.ready_state,
      session_id:details.session_id||null,
      stream_id:details.stream_id||null,
      visibility_state:(typeof document!=='undefined'&&document.visibilityState)||'unknown',
      online:(typeof navigator!=='undefined'&&typeof navigator.onLine==='boolean')?navigator.onLine:null,
      url_path:(typeof location!=='undefined'&&location.pathname)||'/',
      reason:details.reason||'EventSource.onerror',
    };
    void api('/api/client-events/log',{method:'POST',body:JSON.stringify(payload),timeoutMs:3000,timeoutToast:false}).catch(()=>{});
  }catch(_){}
}

// Persist/restore expanded directory state per workspace in localStorage
function _wsExpandKey(){
  const ws=S.session&&S.session.workspace;
  return ws?'hermes-webui-expanded:'+ws:null;
}
function _saveExpandedDirs(){
  const key=_wsExpandKey();if(!key)return;
  try{localStorage.setItem(key,JSON.stringify([...(S._expandedDirs||new Set())]));}catch(e){}
}
function _restoreExpandedDirs(){
  const key=_wsExpandKey();
  if(!key){S._expandedDirs=new Set();return;}
  try{
    const raw=localStorage.getItem(key);
    S._expandedDirs=raw?new Set(JSON.parse(raw)):new Set();
  }catch(e){S._expandedDirs=new Set();}
}

function _escapeGrantStore(){
  if(!S._escapeGrants) S._escapeGrants = Object.create(null);
  return S._escapeGrants;
}

function _normalizeWorkspaceRelPath(path){
  let raw = String(path || '').trim().replace(/\\/g, '/');
  if(!raw || raw === '.') return '.';
  if(raw.startsWith('/')) return '';
  const parts = [];
  for(const part of raw.split('/')){
    if(!part || part === '.') continue;
    if(part === '..'){
      if(parts.length) parts.pop();
      else return '';
      continue;
    }
    parts.push(part);
  }
  return parts.length ? parts.join('/') : '.';
}

function _isSameOrChildPath(base, path){
  const normalizedBase = _normalizeWorkspaceRelPath(base);
  const normalizedPath = _normalizeWorkspaceRelPath(path);
  if(!normalizedBase || !normalizedPath) return false;
  if(normalizedBase === '.') return true;
  return normalizedPath === normalizedBase || normalizedPath.startsWith(`${normalizedBase}/`);
}

function _workspaceEscapeGrantForPath(path){
  const grants = _escapeGrantStore();
  const normalizedPath = _normalizeWorkspaceRelPath(path);
  if(!normalizedPath || !S.session || !S.session.session_id) return null;
  const sessionId = S.session.session_id;
  let best = null;
  for(const root of Object.keys(grants)){
    const grant = grants[root];
    if(!grant || grant.sessionId !== sessionId) continue;
    if(grant.expiresAt && Date.now() >= grant.expiresAt){
      delete grants[root];
      continue;
    }
    if(!_isSameOrChildPath(root, normalizedPath)) continue;
    if(!best || root.length > best.root.length) best = {root, grant};
  }
  return best ? best.grant : null;
}

function _workspaceEscapeExactGrant(path){
  const normalizedPath = _normalizeWorkspaceRelPath(path);
  const grant = _workspaceEscapeGrantForPath(normalizedPath);
  if(!grant) return null;
  return grant.path === normalizedPath ? grant : null;
}

function _storeWorkspaceEscapeGrant(data){
  if(!S.session || !data || !data.token) return null;
  const grants = _escapeGrantStore();
  const root = _normalizeWorkspaceRelPath(data.path || '');
  if(!root) return null;
  const grant = {
    sessionId: S.session.session_id,
    path: root,
    token: String(data.token),
    expiresAt: Number(data.expires_at || 0) * 1000,
    isDir: !!data.is_dir,
  };
  grants[root] = grant;
  return grant;
}

function _clearWorkspaceEscapeGrant(path){
  const grants = S._escapeGrants;
  if(!grants) return;
  const root = _normalizeWorkspaceRelPath(path);
  if(root && grants[root]) delete grants[root];
}

function _workspacePathIsReadOnly(path){
  return !!_workspaceEscapeGrantForPath(path || S.currentDir || '.');
}

function _workspaceRouteForPath(path, kind, opts={}){
  const route=_workspaceRouteForPathRel(path, kind, opts);
  if(!route) return route;
  const base=(typeof document!=='undefined'&&document.baseURI)||(typeof location!=='undefined'&&location.href)||'';
  if(!base||!/^https?:\/\//i.test(base)) return route;
  const rel=route.startsWith('/') ? route.slice(1) : route;
  return new URL(rel, base).href;
}

function _workspaceRouteForPathRel(path, kind, opts={}){
  if(!S.session) return '';
  const normalizedPath = _normalizeWorkspaceRelPath(path);
  const grant = _workspaceEscapeGrantForPath(normalizedPath);
  const sessionId = encodeURIComponent(S.session.session_id);
  const params = new URLSearchParams({session_id:S.session.session_id, path:normalizedPath || '.'});
  if(grant){
    params.set('token', grant.token);
    if(kind === 'raw' && opts.download) params.set('download', '1');
    if(kind === 'raw' && opts.inline) params.set('inline', '1');
    if(kind === 'list') return `/api/escape/list?${params.toString()}`;
    if(kind === 'read') return `/api/escape/file/read?${params.toString()}`;
    if(kind === 'raw') return `/api/escape/file/raw?${params.toString()}`;
  }
  if(kind === 'list') return `/api/list?session_id=${sessionId}&path=${encodeURIComponent(normalizedPath || '.')}`;
  if(kind === 'read') return `/api/file?session_id=${sessionId}&path=${encodeURIComponent(normalizedPath || '.')}`;
  if(kind === 'raw'){
    const extra = [];
    if(opts.download) extra.push('download=1');
    if(opts.inline) extra.push('inline=1');
    const suffix = extra.length ? `&${extra.join('&')}` : '';
    return `/api/file/raw?session_id=${sessionId}&path=${encodeURIComponent(normalizedPath || '.')}${suffix}`;
  }
  return '';
}

async function authorizeWorkspaceEscapeNavigation(item){
  if(!S.session || !item || !item.path) return null;
  const normalizedPath = _normalizeWorkspaceRelPath(item.path);
  const exactGrant = _workspaceEscapeExactGrant(normalizedPath);
  if(!exactGrant){
    const ok = await showConfirmDialog({
      title: item.name || normalizedPath,
      message: t('external_link_open_confirm'),
      confirmLabel: t('dialog_confirm_btn'),
      danger: false,
      hideCancel: true,
      focusCancel: false,
    });
    if(!ok) return null;
  }
  try{
    const data = await api('/api/escape/authorize', {
      method: 'POST',
      body: JSON.stringify({
        session_id: S.session.session_id,
        path: normalizedPath,
      }),
    });
    const grant = _storeWorkspaceEscapeGrant(data);
    if(!grant) throw new Error('Missing escape authorization token');
    showToast(t('external_link_read_only'), 2000);
    return grant;
  }catch(e){
    showToast(t('external_link_grant_expired') || (e && e.message ? e.message : String(e)), 5000, 'error');
    return null;
  }
}

let _workspacePanelActiveTab = 'files';
let _sessionManifest = null;
let _sessionManifestSid = null;
let _sessionManifestTimer = null;
let _sessionManifestInflight = null;
let _sessionManifestInflightSid = null;

const WORKSPACE_INSPECTOR_TABS = new Set(['files', 'tasks', 'artifacts', 'references']);

function _setWorkspacePanelTabDataset(){
  const panel = document.querySelector('.rightpanel');
  if(panel) panel.dataset.activeTab = _workspacePanelActiveTab;
}

function _workspaceInspectorLabel(key, fallback){
  return (typeof t === 'function' && t(key)) || fallback;
}

function scheduleRefreshSessionManifest(){
  if(_sessionManifestTimer) clearTimeout(_sessionManifestTimer);
  _sessionManifestTimer = setTimeout(()=>{
    _sessionManifestTimer = null;
    void loadSessionManifest();
  }, 120);
}

function scheduleRenderSessionArtifacts(){
  renderSessionInspector();
}

async function loadSessionManifest(){
  if(!S.session||!S.session.session_id) return null;
  const sid = S.session.session_id;
  if(_sessionManifestInflight && _sessionManifestInflightSid === sid) return _sessionManifestInflight;
  try{
    _sessionManifestInflightSid = sid;
    _sessionManifestInflight = api(`/api/session/manifest?session_id=${encodeURIComponent(sid)}`);
    const data = await _sessionManifestInflight;
    if(!S.session||S.session.session_id!==sid) return null;
    _sessionManifest = data && data.manifest ? data.manifest : null;
    _sessionManifestSid = sid;
    renderSessionInspector();
    refreshTurnArtifactsInChat();
    return _sessionManifest;
  }catch(e){
    console.warn('loadSessionManifest', e);
    if(S.session&&S.session.session_id===sid){
      _sessionManifest = null;
      _sessionManifestSid = sid;
      renderSessionInspector();
    }
    return null;
  }finally{
    if(_sessionManifestInflightSid === sid){
      _sessionManifestInflight = null;
      _sessionManifestInflightSid = null;
    }
  }
}

function _syncWorkspaceInspectorTabs(){
  const active = _workspacePanelActiveTab;
  const map = {
    files: $('workspaceFilesTab'),
    tasks: $('workspaceTasksTab'),
    artifacts: $('workspaceArtifactsTab'),
    references: $('workspaceReferencesTab'),
  };
  Object.entries(map).forEach(([name, el])=>{
    if(!el) return;
    const on = active === name;
    el.classList.toggle('active', on);
    el.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  const lists = {
    tasks: $('workspaceTasks'),
    artifacts: $('workspaceArtifacts'),
    references: $('workspaceReferences'),
  };
  Object.entries(lists).forEach(([name, el])=>{
    if(el) el.hidden = active !== name;
  });
}

if(typeof document !== 'undefined'){
  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _setWorkspacePanelTabDataset, {once:true});
  else _setWorkspacePanelTabDataset();
}

function switchWorkspacePanelTab(tab){
  _workspacePanelActiveTab = WORKSPACE_INSPECTOR_TABS.has(tab) ? tab : 'files';
  _setWorkspacePanelTabDataset();
  _syncWorkspaceInspectorTabs();
  if(_workspacePanelActiveTab === 'files'){
    const tree = $('fileTree');
    if(tree) tree.style.display = '';
  }else{
    const tree = $('fileTree');
    if(tree) tree.style.display = 'none';
  }
  if(_workspacePanelActiveTab !== 'files') renderSessionInspector();
}

function _manifestForActiveSession(){
  if(!S.session||!S.session.session_id) return null;
  if(_sessionManifestSid !== S.session.session_id) return null;
  return _sessionManifest;
}

function _cloneManifestValue(value){
  if(value===undefined||value===null) return value;
  try{return JSON.parse(JSON.stringify(value));}
  catch(_){return value;}
}

function _mergeManifestRows(existing, incoming){
  const byPath = new Map();
  const add = (row) => {
    if(!row||typeof row!=='object') return;
    const path = String(row.path||'').trim();
    const preview = String(row.preview||'').trim();
    const source_tool = String(row.source_tool||'').trim();
    if(!path || (preview !== 'file' && preview !== 'skill') || !source_tool) return;
    const merged = {path, preview, source_tool};
    const profile = String(row.profile||'').trim();
    if(profile) merged.profile = profile;
    const key = `${profile}\u0000${path}`;
    byPath.set(key, merged);
  };
  (existing||[]).forEach(add);
  (incoming||[]).forEach(add);
  return [...byPath.values()].sort((a,b)=>{
    const ap = String(a.profile||'');
    const bp = String(b.profile||'');
    return ap===bp ? String(a.path||'').localeCompare(String(b.path||'')) : ap.localeCompare(bp);
  });
}

function _normalizeDeltaTurnKey(delta){
  const key = String(delta && delta.turn_key || '').trim();
  return key.startsWith('turn:') ? key : '';
}

function _turnKeySortValue(turnKey){
  const key = String(turnKey || '');
  if(!key.startsWith('turn:')) return 1000000000;
  const idx = Number(key.slice(5));
  return Number.isInteger(idx) ? idx : 1000000000;
}

function _mergeManifestTurns(existingTurns, incomingTurns){
  const byKey = new Map();
  const add = (turn) => {
    if(!turn||typeof turn!=='object') return;
    const key = String(turn.turn_key||'').trim();
    if(!key) return;
    const current = byKey.get(key) || {turn_key:key, artifacts:[], references:[]};
    byKey.set(key, {
      turn_key: key,
      artifacts: _mergeManifestRows(current.artifacts, turn.artifacts),
      references: _mergeManifestRows(current.references, turn.references),
    });
  };
  (existingTurns||[]).forEach(add);
  (incomingTurns||[]).forEach(add);
  return [...byKey.values()].sort((a,b)=>{
    const ai = _turnKeySortValue(a.turn_key);
    const bi = _turnKeySortValue(b.turn_key);
    return ai===bi ? String(a.turn_key||'').localeCompare(String(b.turn_key||'')) : ai-bi;
  });
}

function applySessionManifestDelta(delta){
  if(!delta||typeof delta!=='object'||!S.session||!S.session.session_id) return false;
  if(delta.session_id && delta.session_id !== S.session.session_id) return false;
  if(delta.sequence!==undefined){
    const seqKey = `${delta.stream_id||''}:${delta.sequence}`;
    S._manifestDeltaSequences = S._manifestDeltaSequences || new Set();
    if(S._manifestDeltaSequences.has(seqKey)) return false;
    S._manifestDeltaSequences.add(seqKey);
  }
  if(!_sessionManifest || _sessionManifestSid !== S.session.session_id){
    _sessionManifest = {
      todos: {items:[]},
      artifacts: [],
      references: [],
      turns: [],
    };
    _sessionManifestSid = S.session.session_id;
  }
  const turnKey = _normalizeDeltaTurnKey(delta);
  S._lastManifestDeltaTurnKey = turnKey;
  const incomingTurns = Array.isArray(delta.turns) ? delta.turns : (turnKey ? [{
    turn_key: turnKey,
    artifacts: delta.artifacts || [],
    references: delta.references || [],
  }] : []);
  if(delta.todos && Array.isArray(delta.todos.items)){
    _sessionManifest.todos = {items: _cloneManifestValue(delta.todos.items)};
  }
  _sessionManifest.artifacts = _mergeManifestRows(_sessionManifest.artifacts, delta.artifacts);
  _sessionManifest.references = _mergeManifestRows(_sessionManifest.references, delta.references);
  _sessionManifest.turns = _mergeManifestTurns(_sessionManifest.turns, incomingTurns);
  _sessionManifest.live = delta.stream_id ? {stream_id: delta.stream_id, source:'sse'} : _sessionManifest.live;
  renderSessionInspector();
  return true;
}

function getTurnArtifacts(turnKey){
  const manifest = _manifestForActiveSession();
  if(!manifest||!Array.isArray(manifest.turns)||!turnKey) return [];
  const turn = manifest.turns.find(row=>row&&row.turn_key===turnKey);
  return turn&&Array.isArray(turn.artifacts) ? turn.artifacts : [];
}

function renderTurnArtifacts(turnKey, root){
  if(!root) return;
  const items = getTurnArtifacts(turnKey);
  if(!items.length){
    root.remove();
    return;
  }
  root.innerHTML = `<div class="turn-artifacts-label">${esc(_workspaceInspectorLabel('turn_artifacts_label', 'Files changed this turn'))}</div>`+
    `<div class="turn-artifacts-list">${items.map((item, idx)=>{
      const path = item.path || '';
      const source = item.source_tool || '';
      const profile = item.profile || '';
      const expired = isManifestExpired(item);
      const metaParts = [source, profile];
      if(expired) metaParts.push(_manifestExpiredLabel());
      const meta = metaParts.filter(Boolean).join(' · ');
      const disabledCls = expired ? ' is-disabled' : '';
      return `<button type="button" class="turn-artifact-chip${disabledCls}" data-turn-artifact-idx="${idx}" data-path="${esc(path)}">${esc(path)}${meta?`<span>${esc(meta)}</span>`:''}</button>`;
    }).join('')}</div>`;
  root.querySelectorAll('.turn-artifact-chip').forEach(btn=>{
    const idx = Number(btn.dataset.turnArtifactIdx);
    const item = Number.isInteger(idx) ? items[idx] : null;
    btn.onclick=()=>openManifestPreview(item || {path: btn.dataset.path||''});
  });
}

function refreshTurnArtifactsInChat(){
  if(typeof renderTurnArtifacts!=='function') return;
  const inner=document.querySelector('.messages-inner');
  if(!inner) return;
  inner.querySelectorAll('.assistant-turn[data-turn-key]').forEach(turn=>{
    if(turn.id==='liveAssistantTurn'||turn.querySelector('[data-live-assistant="1"]')) return;
    const key=turn.dataset.turnKey;
    const blocks=typeof _assistantTurnBlocks==='function'?_assistantTurnBlocks(turn):turn.querySelector('.assistant-turn-blocks');
    if(!key||!blocks) return;
    let host=turn.querySelector('.turn-artifacts');
    if(!host){
      host=document.createElement('div');
      host.className='turn-artifacts';
      blocks.appendChild(host);
    }
    renderTurnArtifacts(key, host);
  });
}

function _manifestRowByPath(path, collection){
  const manifest = _manifestForActiveSession();
  if(!manifest || !path) return null;
  const rows = Array.isArray(manifest[collection]) ? manifest[collection] : [];
  return rows.find(item=>item && item.path === path) || null;
}

function isManifestExpired(item){
  return item?.status === 'expired';
}

function isManifestPreviewable(item){
  if(isManifestExpired(item)) return false;
  return item?.preview === 'file' || item?.preview === 'skill';
}

function _manifestExpiredLabel(){
  return typeof t === 'function' ? t('manifest_file_expired') : 'File expired';
}

function _inspectorFileMeta(item){
  if(item && item.preview === 'skill') return _workspaceInspectorLabel('workspace_preview_skill', 'skill');
  if(isManifestExpired(item)) return _manifestExpiredLabel();
  return '';
}

function _renderInspectorFileList(root, items, emptyKey, emptyFallback){
  if(!root) return;
  if(!S.session){
    root.innerHTML = `<div class="workspace-inspector-empty">${esc(_workspaceInspectorLabel('workspace_inspector_no_session', 'Open a conversation to inspect session files.'))}</div>`;
    return;
  }
  if(!items.length){
    root.innerHTML = `<div class="workspace-inspector-empty">${esc(_workspaceInspectorLabel(emptyKey, emptyFallback))}</div>`;
    return;
  }
  root.innerHTML = items.map((item, idx)=>{
    const path = item.path || '';
    const expired = isManifestExpired(item);
    const metaBits = [item.source_tool || '', item.profile || '', _inspectorFileMeta(item)].filter(Boolean);
    const disabledCls = expired ? ' is-disabled' : '';
    return `<button type="button" class="workspace-inspector-item${disabledCls}" data-manifest-idx="${idx}" data-path="${esc(path)}"><div class="workspace-inspector-path">${esc(path)}</div><div class="workspace-inspector-meta">${esc(metaBits.join(' · '))}</div></button>`;
  }).join('');
}

function _bindInspectorFileList(root, items){
  if(!root) return;
  root.querySelectorAll('.workspace-inspector-item').forEach(btn=>{
    const idx = Number(btn.dataset.manifestIdx);
    const item = Number.isInteger(idx) ? items[idx] : _manifestRowByPath(btn.dataset.path || '', 'artifacts');
    btn.onclick = ()=> openManifestPreview(item || {path: btn.dataset.path || ''});
  });
}

function renderSessionTasks(){
  const root = $('workspaceTasks');
  const count = $('workspaceTasksCount');
  const manifest = _manifestForActiveSession();
  const items = manifest && manifest.todos && Array.isArray(manifest.todos.items) ? manifest.todos.items : [];
  if(count) count.textContent = String(items.length);
  if(!root) return;
  if(!S.session){
    root.innerHTML = `<div class="workspace-inspector-empty">${esc(_workspaceInspectorLabel('workspace_inspector_no_session', 'Open a conversation to inspect session files.'))}</div>`;
    return;
  }
  if(!items.length){
    root.innerHTML = `<div class="workspace-inspector-empty">${esc(_workspaceInspectorLabel('todos_no_active', 'No active task list in this session.'))}</div>`;
    return;
  }
  const statusIcon = {pending:'□', in_progress:'◔', completed:'✓', cancelled:'✕', unknown:'·'};
  const statusColor = {pending:'var(--muted)', in_progress:'var(--blue)', completed:'rgba(100,200,100,.8)', cancelled:'rgba(200,100,100,.5)', unknown:'var(--muted)'};
  root.innerHTML = items.map(task=>{
    const status = String(task.status || '').toLowerCase();
    const done = status === 'completed';
    const titleCls = done ? 'workspace-task-title is-done' : 'workspace-task-title';
    return `<div class="workspace-task-item"><span class="workspace-task-status" style="color:${statusColor[status]||statusColor.unknown}">${statusIcon[status]||statusIcon.unknown}</span><div class="workspace-task-body"><div class="${titleCls}">${esc(task.content || '')}</div><div class="workspace-task-sub">${esc((task.id || '') + (status ? ' · ' + status : ''))}</div></div></div>`;
  }).join('');
}

function renderSessionArtifacts(){
  const root = $('workspaceArtifacts');
  const count = $('workspaceArtifactsCount');
  const manifest = _manifestForActiveSession();
  const items = manifest && Array.isArray(manifest.artifacts) ? manifest.artifacts : collectSessionArtifacts().map(row=>({
    path: row.path,
    source_tool: row.source || row.kind || 'session',
    preview: 'file',
  }));
  if(count) count.textContent = String(items.length);
  _renderInspectorFileList(
    root,
    items,
    'workspace_artifacts_empty',
    'No artifacts detected yet. Files created or edited during this session will appear here.',
  );
  _bindInspectorFileList(root, items);
}

function renderSessionReferences(){
  const root = $('workspaceReferences');
  const count = $('workspaceReferencesCount');
  const manifest = _manifestForActiveSession();
  const items = manifest && Array.isArray(manifest.references) ? manifest.references : [];
  if(count) count.textContent = String(items.length);
  _renderInspectorFileList(
    root,
    items,
    'workspace_references_empty',
    'No referenced files yet. Files read or searched during this session will appear here.',
  );
  _bindInspectorFileList(root, items);
}

function renderSessionInspector(){
  renderSessionTasks();
  renderSessionArtifacts();
  renderSessionReferences();
}

async function openInspectorReferencePath(path){
  const row = _manifestRowForPath(path);
  if(row && row.preview) return openManifestPreview(row);
  const skillParts = _hermesSkillsPathParts(path);
  if(skillParts){
    if(skillParts.filePath) return openSkillFilePreview(skillParts.skillName, skillParts.filePath);
    return openSkillContentPreview(skillParts.skillName);
  }
  await openManifestPreview(row || {path});
}

async function openManifestPreview(item){
  if(!item || !item.path || !item.preview) return;
  if(isManifestExpired(item)){
    if(typeof showToast === 'function') showToast(_manifestExpiredLabel());
    return;
  }
  if(item.preview === 'skill') return openSkillContentPreview(item.path, item.profile);
  if(item.preview === 'file') return openIntegrationFilePreview(item.path);
}

let _previewSource = 'workspace';

async function openSkillContentPreview(skillName, profile){
  if(!S.session || !skillName) return;
  if(typeof ensureWorkspacePreviewVisible==='function') ensureWorkspacePreviewVisible();
  else if(typeof openWorkspacePanel==='function') openWorkspacePanel('preview');
  switchWorkspacePanelTab('files');
  let url = `/api/skillhub/content?name=${encodeURIComponent(skillName)}`;
  const profileName = String(profile || '').trim();
  if(profileName) url += `&profile=${encodeURIComponent(profileName)}`;
  try{
    const data = await api(url);
    const content = data.content || data.body || '';
    const title = data.name || skillName;
    $('previewPathText').textContent = title;
    $('previewArea').classList.add('visible');
    $('fileTree').style.display = 'none';
    _previewCurrentPath = title;
    _previewRawContent = content;
    _previewSource = 'skill';
    renderFileBreadcrumb(title, {skill: true});
    if(shouldRenderMarkdownPreviewAsPlainText(content)){
      showPreview('code');
      $('previewCode').textContent = content;
      setStatus(largeMarkdownPlainTextStatus(content));
      return;
    }
    showPreview('md');
    $('previewMd').innerHTML = renderMd(content);
    requestAnimationFrame(()=>{if(typeof renderKatexBlocks==='function')renderKatexBlocks();});
  }catch(e){
    setStatus(t('file_open_failed'));
  }
}

async function openSkillFilePreview(skillName, filePath){
  if(!S.session || !skillName || !filePath) return;
  if(typeof ensureWorkspacePreviewVisible==='function') ensureWorkspacePreviewVisible();
  else if(typeof openWorkspacePanel==='function') openWorkspacePanel('preview');
  switchWorkspacePanelTab('files');
  const url = `/api/skillhub/file?name=${encodeURIComponent(skillName)}&path=${encodeURIComponent(filePath)}`;
  try{
    const data = await api(url);
    const content = data.content || data.body || '';
    const title = `${skillName}/${filePath}`;
    $('previewPathText').textContent = title;
    $('previewArea').classList.add('visible');
    $('fileTree').style.display = 'none';
    _previewCurrentPath = title;
    _previewRawContent = content;
    _previewSource = 'skill';
    renderFileBreadcrumb(title, {skill: true});
    const ext = fileExt(filePath);
    if(MD_EXTS.has(ext)){
      if(shouldRenderMarkdownPreviewAsPlainText(content)){
        showPreview('code');
        $('previewCode').textContent = content;
        setStatus(largeMarkdownPlainTextStatus(content));
        return;
      }
      showPreview('md');
      $('previewMd').innerHTML = renderMd(content);
      requestAnimationFrame(()=>{if(typeof renderKatexBlocks==='function')renderKatexBlocks();});
      return;
    }
    showPreview('code');
    $('previewCode').textContent = content;
  }catch(e){
    setStatus(t('file_open_failed'));
  }
}

function clearSessionManifest(){
  _sessionManifest = null;
  _sessionManifestSid = null;
  if(typeof renderSessionInspector==='function') renderSessionInspector();
}

window.HermesSessionInspector = {
  manifest: () => _manifestForActiveSession(),
  refresh: () => loadSessionManifest(),
  clear: () => clearSessionManifest(),
  applyDelta: (delta) => applySessionManifestDelta(delta),
  getTurnArtifacts: (turnKey) => getTurnArtifacts(turnKey),
  renderTurnArtifacts: (turnKey, root) => renderTurnArtifacts(turnKey, root),
  refreshTurnArtifactsInChat: () => refreshTurnArtifactsInChat(),
  isManifestPreviewable: (item) => isManifestPreviewable(item),
  openManifestPreview: (item) => openManifestPreview(item),
};

const ARTIFACT_IGNORE_RE = /(^|\/)(?:\.git|\.hg|\.svn|node_modules|\.venv|venv|__pycache__|dist|build|\.next|\.cache)(?:\/|$)/;
// Canonical Hermes mutators plus MCP filesystem aliases that can create/edit files.
const ARTIFACT_MUTATION_TOOLS = new Set(['write_file','patch','edit_file','create_file','mcp_filesystem_write_file','mcp_filesystem_edit_file']);

function _normalizeArtifactPath(path){
  if(!path) return '';
  path = String(path).trim().replace(/[\`"'<>),.;:]+$/g,'').replace(/^[\`"'(<]+/g,'');
  if(!path || path.length > 240 || path.includes('://')) return '';
  // Canonicalize workspace-relative prefixes so a file-tree open ("foo.md") and a
  // tool arg recorded as "./foo.md" or "~/foo.md" compare equal for mutation
  // tracking; otherwise an agent edit via a ./-prefixed path leaves the open
  // preview stale (#3262 / pre-release regression-gate finding).
  path = path.replace(/^~\//,'').replace(/^(?:\.\/)+/,'');
  if(!path) return '';
  if(ARTIFACT_IGNORE_RE.test(path)) return '';
  if(!/[./]/.test(path)) return '';
  return path;
}

function _artifactCandidatesFromText(text){
  if(!text || typeof text !== 'string') return [];
  const out = [];
  const seen = new Set();
  const add = (path) => {
    path = _normalizeArtifactPath(path);
    if(!path || seen.has(path)) return;
    seen.add(path); out.push({path, kind:'diff'});
  };
  // Fallback text mining is intentionally narrow: only diff/patch fences imply
  // the session changed a file. Prose mentions such as "edited package.json" are
  // too noisy for an Artifacts list that should track write/edit outputs.
  const fenced = /```(?:diff|patch)\s*\n[\s\S]*?```/gi;
  let m;
  while((m = fenced.exec(text))){
    const block = m[0];
    const fm = block.match(/(?:^|\n)(?:\+\+\+|---)\s+(?:[ab]\/)?([^\n\t]+)/);
    if(fm) add(fm[1].trim());
  }
  return out;
}

function _artifactCandidatesFromToolCall(tc){
  if(!tc) return [];
  const name = String(tc.name || '').replace(/^functions\./,'');
  const args = tc.arguments || tc.args || tc.input || {};
  const result = tc.result || tc.output || tc.snippet || '';
  const out = [];
  const add = (path, source=name || 'tool') => {
    path = _normalizeArtifactPath(path);
    if(path) out.push({path, kind:source});
  };
  if(ARTIFACT_MUTATION_TOOLS.has(name) && args && typeof args === 'object'){
    for(const key of ['path','file_path','source','destination']) add(args[key]);
    if(Array.isArray(args.paths)) args.paths.forEach(p=>add(p));
    if(Array.isArray(args.edits)) args.edits.forEach(e=>add(e&&e.path));
  }
  const resultText = typeof result === 'string' ? result : (result ? JSON.stringify(result) : '');
  // Tool results may include unified diffs from patch-style tools; scan those
  // narrowly after structured args so diff headers can still contribute paths.
  for(const a of _artifactCandidatesFromText(resultText)) out.push(a);
  if(!out.length && ARTIFACT_MUTATION_TOOLS.has(name)){
    const argsText = typeof args === 'string' ? args : JSON.stringify(args || {});
    for(const a of _artifactCandidatesFromText(argsText)) out.push(a);
  }
  return out;
}

const _turnMutatedPreviewPaths = new Set();

function resetTurnWorkspaceMutations(){
  _turnMutatedPreviewPaths.clear();
}

function noteWorkspaceMutationsFromToolCall(tc){
  for(const a of _artifactCandidatesFromToolCall(tc)){
    const path=_normalizeArtifactPath(a.path);
    if(path) _turnMutatedPreviewPaths.add(path);
  }
}

function noteWorkspaceMutationsFromToolCalls(toolCalls){
  if(!Array.isArray(toolCalls)) return;
  for(const tc of toolCalls) noteWorkspaceMutationsFromToolCall(tc);
}

function _isOpenPreviewPathMutated(){
  if(!_previewCurrentPath) return false;
  const current=_normalizeArtifactPath(_previewCurrentPath);
  return !!(current&&_turnMutatedPreviewPaths.has(current));
}

async function refreshOpenPreviewIfMutated(){
  if(typeof _previewDirty!=='undefined'&&_previewDirty) return;
  if(!_isOpenPreviewPathMutated()) return;
  if(!_previewCurrentPath||!S.session) return;
  await openFile(_previewCurrentPath, { bustCache: true });
}

function collectSessionArtifacts(){
  const items = [];
  const seen = new Set();
  const push = (path, source) => {
    path = _normalizeArtifactPath(path);
    if(!path || seen.has(path)) return;
    seen.add(path); items.push({path, source});
  };
  // Source 1: session-level tool call summaries (may be empty when messages
  // carry their own tool metadata — see _syncToolCallsForLoadedMessages).
  for(const tc of (S.toolCalls || [])){
    for(const a of _artifactCandidatesFromToolCall(tc)) push(a.path, a.kind || tc.name || 'tool');
  }
  // Source 2 & 3: message-level data — both text-mined diffs and structured
  // tool_calls / tool_use content blocks that survive the S.toolCalls clear.
  for(const msg of (S.messages || [])){
    if(!msg) continue;
    const text = msg.content || msg.text || msg.message || '';
    // Text-mined diff/patch fences (existing path).
    if(typeof text === 'string'){
      for(const a of _artifactCandidatesFromText(text)) push(a.path, a.kind);
    }
    // Structured tool_calls array (OpenAI format: {function:{name,arguments}}).
    if(Array.isArray(msg.tool_calls)){
      for(const tc of msg.tool_calls){
        if(!tc || typeof tc !== 'object') continue;
        const fn = (tc.function && typeof tc.function === 'object') ? tc.function : tc;
        const name = fn.name || tc.name || '';
        let args = fn.arguments || tc.arguments || tc.args || tc.input || {};
        if(typeof args === 'string'){ try{ args = JSON.parse(args); }catch(_){} }
        const fakeTc = {name, args, result: tc.result || tc.output || ''};
        for(const a of _artifactCandidatesFromToolCall(fakeTc)) push(a.path, a.kind || name || 'tool');
      }
    }
    // Structured content array with tool_use blocks (Anthropic format).
    if(Array.isArray(msg.content)){
      for(const block of msg.content){
        if(!block || block.type !== 'tool_use') continue;
        let inp = block.input || {};
        if(typeof inp === 'string'){ try{ inp = JSON.parse(inp); }catch(_){} }
        const fakeTc = {name: block.name || '', args: inp, result: block.result || ''};
        for(const a of _artifactCandidatesFromToolCall(fakeTc)) push(a.path, a.kind || block.name || 'tool');
      }
    }
  }
  return items.slice(0, 50);
}

function _normalizeOsPath(path){
  return String(path || '').trim().replace(/\\/g, '/').replace(/^~\//, '');
}

function _isHermesSkillsPath(path){
  return /(?:^|\/)\.hermes\/skills\//.test(_normalizeOsPath(path));
}

function _hermesSkillsPathParts(path){
  const p = _normalizeOsPath(path);
  const m = p.match(/(?:^|\/)\.hermes\/skills\/(.+)$/);
  if(!m) return null;
  const rest = m[1].replace(/\/+$/, '');
  if(!rest) return null;
  if(rest.endsWith('/SKILL.md') || rest === 'SKILL.md'){
    const skillName = rest.slice(0, -'/SKILL.md'.length).replace(/\/+$/, '');
    return skillName ? {skillName, filePath: null} : null;
  }
  const subMatch = rest.match(/^(.+?)\/(scripts|references)(?:\/(.*))?$/);
  if(subMatch){
    return {
      skillName: subMatch[1],
      filePath: subMatch[3] ? `${subMatch[2]}/${subMatch[3]}` : null,
    };
  }
  return {skillName: rest, filePath: null};
}

function _manifestRowForPath(path){
  return _manifestRowByPath(path, 'artifacts')
    || _manifestRowByPath(path, 'references');
}

function _toWorkspaceRelativePath(path){
  if(!path) return null;
  if(_isHermesSkillsPath(path)) return null;
  let rel = _normalizeOsPath(path).replace(/^(?:\.\/)+/, '');
  const ws = S.session && S.session.workspace;
if(!ws) return rel || '.';
  const normWs = ws.replace(/\/+$/,'');
  const normWsSlash = normWs + '/';
  if(rel.startsWith('/') || /^[A-Za-z]:/.test(rel)){
    if(rel.startsWith(normWsSlash) || rel === normWs) return rel === normWs ? '.' : rel.slice(normWsSlash.length) || '.';
    return null;
  }
  if(rel.startsWith(normWsSlash)) rel = rel.slice(normWsSlash.length);
  else if(rel === normWs) rel = '.';
  return rel || '.';
}

async function _workspacePathExists(path){
  const rel = _toWorkspaceRelativePath(path);
  if(rel === null || !S.session) return false;
  const parts = rel.split('/').filter(Boolean);
  const name = parts.pop();
  if(!name) return false;
  const dir = parts.length ? parts.join('/') : '.';
  const data = await api(`/api/list?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(dir)}`);
  return (data.entries || []).some(entry => entry && ((entry.path === rel) || entry.name === name));
}

async function openArtifactPath(path){
  if(!path) return;
  const manifestRow = _manifestRowForPath(path);
  if(manifestRow && manifestRow.preview) return openManifestPreview(manifestRow);
  const skillParts = _hermesSkillsPathParts(path);
  if(skillParts){
    if(skillParts.filePath) return openSkillFilePreview(skillParts.skillName, skillParts.filePath);
    return openSkillContentPreview(skillParts.skillName);
  }
  const rel = _toWorkspaceRelativePath(path);
  if(rel === null){
    if(_isManifestAbsolutePath(path)) return openIntegrationFilePreview(path);
    setStatus(t('file_open_failed'));
    return;
  }
  if(typeof ensureWorkspacePreviewVisible==='function') ensureWorkspacePreviewVisible();
  else if(typeof openWorkspacePanel==='function') openWorkspacePanel('preview');
  switchWorkspacePanelTab('files');
  try{
    if(!(await _workspacePathExists(rel))){
      setStatus(t('file_open_failed'));
      return;
    }
  }catch(_){
    setStatus(t('file_open_failed'));
    return;
  }
  openFile(rel);
}

function _isBrowserPreviewOpen(){
  return _previewCurrentMode==='browser'&&!!_previewBrowserUrl;
}

async function loadDir(path, opts={}){
  const preservePreview=!!(opts&&opts.preservePreview)||_isBrowserPreviewOpen();
  if(!S.session)return;
  const sessionId=S.session.session_id;
  try{
    if(!path||path==='.'){
      S._dirCache={};
      _restoreExpandedDirs();  // restore per-workspace expanded state on root load
    }
    S.currentDir=path||'.';
    const data=await api(`/api/list?session_id=${encodeURIComponent(sessionId)}&path=${encodeURIComponent(path)}`);
    if(!S.session||S.session.session_id!==sessionId)return;
    S.entries=data.entries||[];renderBreadcrumb();renderFileTree();
    if(_workspacePanelActiveTab !== 'files' && typeof scheduleRefreshSessionManifest==='function') scheduleRefreshSessionManifest();
    // Pre-fetch contents of restored expanded dirs so they render without a second click
    // (parallelized — avoids serial waterfall when multiple dirs are expanded)
    if(!path||path==='.'){
      const expanded=S._expandedDirs||new Set();
      const pending=[...expanded].filter(dirPath=>!S._dirCache[dirPath]);
      if(pending.length){
        const results=await Promise.all(pending.map(dirPath=>
          api(`/api/list?session_id=${encodeURIComponent(sessionId)}&path=${encodeURIComponent(dirPath)}`)
            .then(dc=>({dirPath,entries:dc.entries||[],missing:false}))
            .catch(error=>({dirPath,entries:[],missing:!!(error&&error.status===404)}))
        ));
        if(!S.session||S.session.session_id!==sessionId)return;
        let prunedExpandedDirs=false;
        for(const {dirPath,entries,missing} of results){
          if(missing){
            // Expanded directories are persisted by workspace, not session. A
            // directory removed on disk must not make each future session in
            // this workspace repeat its stale /api/list request.
            expanded.delete(dirPath);
            prunedExpandedDirs=true;
            continue;
          }
          S._dirCache[dirPath]=entries;
        }
        if(prunedExpandedDirs)_saveExpandedDirs();
      }
      if(expanded.size>0)renderFileTree();
    }
    if(!preservePreview&&typeof clearPreview==='function'){
      if(typeof _previewDirty!=='undefined'&&_previewDirty){
        showConfirmDialog({title:t('unsaved_confirm'),message:'',confirmLabel:'Discard',danger:true,focusCancel:true}).then(ok=>{if(ok)clearPreview({keepPanelOpen:true});});
      }else{
        clearPreview({keepPanelOpen:true});
      }
    }else if(preservePreview){
      await refreshOpenPreviewIfMutated();
    }
    // Fetch git info for workspace root (non-blocking)
    if(!path||path==='.') _refreshGitBadge();
  }catch(e){console.warn('loadDir',e);}
}

async function _refreshGitBadge(){
  const badge=$('gitBadge');
  if(!badge||!S.session)return;
  const sessionId=S.session.session_id;
  try{
    const data=await api(`/api/git-info?session_id=${encodeURIComponent(sessionId)}`);
    if(!S.session||S.session.session_id!==sessionId)return;
    if(data.git&&data.git.is_git){
      const g=data.git;
      let text=g.branch||'git';
      if(g.dirty>0) text+=` \u00b7 ${g.dirty}\u2206`; // middot + delta
      if(g.behind>0) text+=` \u2193${g.behind}`;
      if(g.ahead>0) text+=` \u2191${g.ahead}`;
      badge.textContent=text;
      badge.className='git-badge'+(g.dirty>0?' dirty':'');
      badge.style.display='';
    } else {
      badge.style.display='none';
      badge.textContent='';
    }
  }catch(e){
    if(!S.session||S.session.session_id!==sessionId)return;
    badge.style.display='none';
  }
}

function navigateUp(){
  if(!S.session||S.currentDir==='.')return;
  const parts=S.currentDir.split('/');
  parts.pop();
  loadDir(parts.length?parts.join('/'):'.');
}

// File extension sets for preview routing (must match server-side sets)
const IMAGE_EXTS  = new Set(['.png','.jpg','.jpeg','.gif','.svg','.webp','.ico','.bmp']);
const MD_EXTS     = new Set(['.md','.markdown','.mdown']);
const HTML_EXTS   = new Set(['.html','.htm']);
const PDF_EXTS    = new Set(['.pdf']);
const AUDIO_EXTS  = new Set(['.mp3','.wav','.m4a','.aac','.ogg','.oga','.opus','.flac']);
const VIDEO_EXTS  = new Set(['.mp4','.mov','.m4v','.webm','.ogv','.avi','.mkv']);
const MD_PREVIEW_RICH_RENDER_MAX_BYTES = 256 * 1024;
const MD_PREVIEW_RICH_RENDER_MAX_LINES = 5000;
// Binary formats that should download rather than preview
const DOWNLOAD_EXTS = new Set([
  '.docx','.doc','.xlsx','.xls','.pptx','.ppt','.odt','.ods','.odp',
  '.zip','.tar','.gz','.bz2','.7z','.rar',
  '.exe','.dmg','.pkg','.deb','.rpm',
  '.woff','.woff2','.ttf','.otf','.eot',
  '.bin','.dat','.db','.sqlite','.pyc','.class','.so','.dylib','.dll',
]);

const INTEGRATION_WORKSPACE_API = '/api/integration/workspace';
const INTEGRATION_HTML_PREVIEW_SANDBOX = 'sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox';

function _isManifestAbsolutePath(path){
  const p = String(path || '');
  return p.startsWith('/') || /^[A-Za-z]:[\\/]/.test(p);
}

function _manifestFilePreviewUrl(path, opts){
  opts = opts || {};
  if(_isManifestAbsolutePath(path)){
    const sid = (S.session && S.session.session_id) ? String(S.session.session_id) : '';
    let url = 'api/media?path=' + encodeURIComponent(path);
    if(sid) url += '&session_id=' + encodeURIComponent(sid);
    if(opts.inline) url += '&inline=1';
    if(opts.download) url += '&download=1';
    return url;
  }
  return _integrationFileUrl(path);
}

function _integrationFileUrl(path){
  return `${INTEGRATION_WORKSPACE_API}/file?path=${encodeURIComponent(path)}`;
}

async function _fetchIntegrationFileBlob(path){
  const res = await fetch(_integrationFileUrl(path), {credentials: 'same-origin'});
  if(!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.blob();
}

async function _fetchManifestFileBlob(path){
  const res = await fetch(_manifestFilePreviewUrl(path), {credentials: 'same-origin'});
  if(!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.blob();
}

async function _fetchIntegrationFileText(path){
  const blob = await _fetchIntegrationFileBlob(path);
  return blob.text();
}

async function _fetchManifestFileText(path){
  const blob = await _fetchManifestFileBlob(path);
  return blob.text();
}

async function _downloadManifestFile(path){
  if(_isManifestAbsolutePath(path)){
    const url = _manifestFilePreviewUrl(path, {download: true});
    const filename = path.split('/').pop() || path;
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    if(typeof showToast==='function') showToast(t('downloading', filename), 2000);
    return;
  }
  return _downloadIntegrationFile(path);
}

async function _downloadIntegrationFile(path){
  const blob = await _fetchIntegrationFileBlob(path);
  const filename = path.split('/').pop() || path;
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  setTimeout(()=>{
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }, 100);
  if(typeof showToast==='function') showToast(t('downloading', filename), 2000);
}

function _showIntegrationHtmlPreview(iframe, html){
  if(!iframe) return;
  iframe.removeAttribute('src');
  iframe.srcdoc = html || '';
  iframe.setAttribute('sandbox', INTEGRATION_HTML_PREVIEW_SANDBOX);
}

async function openIntegrationFilePreview(path){
  if(!path) return;
  if(typeof ensureWorkspacePreviewVisible==='function') ensureWorkspacePreviewVisible();
  else if(typeof openWorkspacePanel==='function') openWorkspacePanel('preview');
  switchWorkspacePanelTab('files');
  const ext = fileExt(path);
  if(DOWNLOAD_EXTS.has(ext)){
    try{
      await _downloadManifestFile(path);
    }catch(e){
      setStatus(t('file_open_failed'));
    }
    return;
  }
  $('previewPathText').textContent = path;
  $('previewArea').classList.add('visible');
  $('fileTree').style.display = 'none';
  _previewCurrentPath = path;
  _previewSource = 'workspace';
  renderFileBreadcrumb(path);
  const fileUrl = _manifestFilePreviewUrl(path, {
    inline: AUDIO_EXTS.has(ext) || VIDEO_EXTS.has(ext),
  });
  if(IMAGE_EXTS.has(ext)){
    showPreview('image');
    $('previewImg').alt = path;
    $('previewImg').src = fileUrl;
    $('previewImg').onerror = ()=>setStatus(t('image_load_failed'));
  } else if(AUDIO_EXTS.has(ext) || VIDEO_EXTS.has(ext)){
    const mode = VIDEO_EXTS.has(ext) ? 'video' : 'audio';
    showPreview(mode);
    const wrap = $('previewMediaWrap');
    if(wrap){
      wrap.innerHTML = (typeof _mediaPlayerHtml==='function')
        ? _mediaPlayerHtml(mode, fileUrl, path.split('/').pop()||path)
        : `<${mode} src="${fileUrl.replace(/"/g,'%22')}" controls preload="metadata"></${mode}>`;
      if(typeof _applyMediaPlaybackPreferences==='function') _applyMediaPlaybackPreferences(wrap);
    }
  } else if(PDF_EXTS.has(ext)){
    showPreview('pdf');
    const frame = $('previewPdfFrame');
    if(frame){
      frame.src = '';
      frame.src = fileUrl;
      frame.title = `PDF preview: ${path.split('/').pop()||path}`;
    }
  } else if(MD_EXTS.has(ext)){
    try{
      const content = await _fetchManifestFileText(path);
      _previewRawContent = content;
      if(shouldRenderMarkdownPreviewAsPlainText(content)){
        showPreview('code');
        $('previewCode').textContent = content;
        setStatus(largeMarkdownPlainTextStatus(content));
        return;
      }
      showPreview('md');
      $('previewMd').innerHTML = renderMd(content);
      requestAnimationFrame(()=>{if(typeof renderKatexBlocks==='function')renderKatexBlocks();});
    }catch(e){
      setStatus(t('file_open_failed'));
    }
  } else if(HTML_EXTS.has(ext)){
    try{
      const content = await _fetchManifestFileText(path);
      _previewRawContent = content;
      showPreview('html');
      _showIntegrationHtmlPreview($('previewHtmlIframe'), content);
    }catch(e){
      setStatus(t('file_open_failed'));
    }
  } else {
    try{
      const content = await _fetchManifestFileText(path);
      _previewRawContent = content;
      showPreview('code');
      $('previewCode').textContent = content;
    }catch(e){
      try{
        await _downloadManifestFile(path);
      }catch(_){
        setStatus(t('file_open_failed'));
      }
    }
  }
}

function fileExt(p){ const i=p.lastIndexOf('.'); return i>=0?p.slice(i).toLowerCase():''; }

function markdownPreviewByteLength(content){
  const text=String(content||'');
  if(typeof Blob==='function') return new Blob([text]).size;
  if(typeof TextEncoder==='function') return new TextEncoder().encode(text).length;
  return unescape(encodeURIComponent(text)).length;
}

function markdownPreviewLineCount(content){
  const text=String(content||'');
  if(!text) return 1;
  return text.split('\n').length;
}

function shouldRenderMarkdownPreviewAsPlainText(content){
  return markdownPreviewByteLength(content)>MD_PREVIEW_RICH_RENDER_MAX_BYTES
    || markdownPreviewLineCount(content)>MD_PREVIEW_RICH_RENDER_MAX_LINES;
}

function largeMarkdownPlainTextStatus(content){
  const bytes=markdownPreviewByteLength(content);
  const lines=markdownPreviewLineCount(content);
  const sizeLabel=bytes>=1024?`${Math.round(bytes/1024)} KB`:`${bytes} B`;
  return `Large markdown file (${sizeLabel}, ${lines} lines) shown as plain text. Click "Render as markdown anyway" to force rich rendering, or Edit to view raw.`;
}

function setLargeMarkdownForceRenderVisible(visible){
  const btn=$('btnRenderMarkdownAnyway');
  if(btn) btn.style.display=visible?'inline-flex':'none';
}

function renderMarkdownPreviewContent(data){
  showPreview('md');
  $('previewMd').innerHTML=renderMd(data.content);
  requestAnimationFrame(()=>{if(typeof renderKatexBlocks==='function')renderKatexBlocks();});
}

function forceRenderMarkdownPreview(){
  // #3378 review (Codex): don't force-render from a dirty/open editor — the
  // cached raw content would not reflect the unsaved edit. Require a saved,
  // non-dirty state and cached content that belongs to the current file.
  if(_previewDirty || $('previewEditArea').style.display!=='none') return;
  if(!_previewRawContent || _previewRawContentPath!==_previewCurrentPath) return;
  openFile(_previewCurrentPath,{forceRichMarkdown:true});
  setStatus('Markdown rendered for this file.');
}

let _previewCurrentPath = '';  // relative path of currently previewed file
let _previewCurrentMode = '';  // 'code' | 'md' | 'image' | 'html' | 'pdf' | 'audio' | 'video' | 'browser'
let _previewDirty = false;     // true when edits are unsaved
let _previewBrowserUrl = '';   // remote Camofox/VNC URL when mode === 'browser'

function _rightpanelEl(){
  return document.querySelector('.rightpanel');
}

function _setBrowserPreviewWorkspaceChrome(active){
  const panel=_rightpanelEl();
  if(panel) panel.classList.toggle('browser-preview-active', !!active);
}

function showPreview(mode){
  // mode: 'code' | 'image' | 'md' | 'html' | 'pdf' | 'audio' | 'video' | 'browser'
  $('previewCode').style.display     = mode==='code'  ? '' : 'none';
  $('previewImgWrap').style.display  = mode==='image' ? '' : 'none';
  const mediaWrap=$('previewMediaWrap'); if(mediaWrap) mediaWrap.style.display = (mode==='audio'||mode==='video') ? '' : 'none';
  const pdfWrap=$('previewPdfWrap'); if(pdfWrap) pdfWrap.style.display = mode==='pdf' ? '' : 'none';
  $('previewMd').style.display       = mode==='md'    ? '' : 'none';
  $('previewHtmlWrap').style.display = mode==='html'  ? '' : 'none';
  const browserWrap=$('previewBrowserWrap'); if(browserWrap) browserWrap.style.display = mode==='browser' ? '' : 'none';
  $('previewEditArea').style.display = 'none';  // start in read-only
  const badge=$('previewBadge');
  badge.className='preview-badge '+mode;
  badge.textContent = mode==='image'?'image':mode==='audio'?'audio':mode==='video'?'video':mode==='pdf'?'pdf':mode==='md'?'md':mode==='html'?'html':mode==='browser'?'browser':fileExt($('previewPathText').textContent)||'text';
  _previewCurrentMode = mode;
  _previewDirty = false;
  updateEditBtn();
  // Show "Open in browser" button for iframe-backed document previews
  const openBtn=$('btnOpenInBrowser');
  if(openBtn) openBtn.style.display = (mode==='html'||mode==='pdf'||mode==='browser')?'inline-flex':'none';
  const downloadBtn=$('btnDownloadFile');
  if(downloadBtn) downloadBtn.style.display = mode==='browser' ? 'none' : 'inline-flex';
  setLargeMarkdownForceRenderVisible(false);
  _setBrowserPreviewWorkspaceChrome(mode==='browser');
}

function openBrowserPreview(url, opts={}){
  url=String(url||'').trim();
  if(!/^https?:\/\//i.test(url)) return false;
  if(typeof ensureWorkspacePreviewVisible==='function') ensureWorkspacePreviewVisible();
  else if(typeof openWorkspacePanel==='function') openWorkspacePanel('preview');
  let host=url;
  try{ host=new URL(url).host; }catch(_){}
  const tool=String(opts.tool||'').trim();
  $('previewPathText').textContent=tool?`Browser (${tool}) — ${host}`:`Browser — ${host}`;
  $('previewArea').classList.add('visible');
  const ft=$('fileTree'); if(ft) ft.style.display='none';
  _previewCurrentPath='';
  _previewRawContent='';
  _previewRawContentPath='';
  _previewSource='browser';
  _previewBrowserUrl=url;
  showPreview('browser');
  const iframe=$('previewBrowserIframe');
  if(iframe){
    iframe.src='';
    iframe.src=url;
  }
  if(typeof syncWorkspacePanelUI==='function') syncWorkspacePanelUI();
  return true;
}

function clearBrowserPreviewEmbed(){
  _previewBrowserUrl='';
  const iframe=$('previewBrowserIframe');
  if(iframe) iframe.src='';
  _setBrowserPreviewWorkspaceChrome(false);
}

function updateEditBtn(){
  const btn=$('btnEditFile');
  if(!btn)return;
  const editable = _previewCurrentMode==='code'||_previewCurrentMode==='md';
  btn.style.display = editable?'':'none';
  const editing = $('previewEditArea').style.display!=='none';
  btn.innerHTML = editing ? `&#128190; ${t('save')}` : `&#9998; ${t('edit')}`;
  btn.title = editing ? t('save_title') : t('edit_title');
  btn.style.color = editing ? 'var(--blue)' : '';
  if(_previewDirty) btn.innerHTML = '&#128190; Save*';
}

async function toggleEditMode(){
  const editing = $('previewEditArea').style.display!=='none';
  if(editing){
    // Save
    if(!S.session||!_previewCurrentPath)return;
    const content=$('previewEditArea').value;
    try{
      await api('/api/file/save',{method:'POST',body:JSON.stringify({
        session_id:S.session.session_id, path:_previewCurrentPath, content
      })});
      _previewDirty=false;
      // Update read-only views AND the cached raw content so a later
      // "Render as markdown anyway" force-render reflects the just-saved text
      // (not the stale pre-edit fetch). #3378 review (Codex).
      _previewRawContent = content;
      _previewRawContentPath = _previewCurrentPath;
      if(_previewCurrentMode==='code') $('previewCode').textContent=content;
      else renderMarkdownPreviewContent({content});
      $('previewEditArea').style.display='none';
      if(_previewCurrentMode==='code') $('previewCode').style.display='';
      else $('previewMd').style.display='';
      showToast(t('saved'));
    }catch(e){setStatus(t('save_failed')+e.message);}
  }else{
    // Enter edit mode: populate textarea with current content
    const currentText = _previewCurrentMode==='code'
      ? $('previewCode').textContent
      : _previewRawContent||'';
    $('previewEditArea').value=currentText;
    $('previewEditArea').style.display='';
    if(_previewCurrentMode==='code') $('previewCode').style.display='none';
    else $('previewMd').style.display='none';
    // Escape cancels the edit without saving
    $('previewEditArea').onkeydown=e=>{
      if(e.key==='Escape'){e.preventDefault();cancelEditMode();}
    };
  }
  updateEditBtn();
}

let _previewRawContent = '';  // raw text for md files (to populate editor)
let _previewRawContentPath = '';  // path that _previewRawContent belongs to (#3378 force-render cache guard)

function cancelEditMode(){
  // Discard changes and return to read-only view
  $('previewEditArea').style.display='none';
  $('previewEditArea').onkeydown=null;
  if(_previewCurrentMode==='code') $('previewCode').style.display='';
  else $('previewMd').style.display='';
  _previewDirty=false;
  updateEditBtn();
}

// Map file extensions to Prism.js language identifiers.
// Prism autoloader fetches missing language components from CDN on demand.
const _PRISM_LANG_MAP={
  js:'javascript',mjs:'javascript',jsx:'jsx',ts:'typescript',tsx:'tsx',
  py:'python',pyw:'python',pyi:'python',
  rb:'ruby',go:'go',rs:'rust',java:'java',kt:'kotlin',kts:'kotlin',
  c:'c',h:'c',cpp:'cpp',cxx:'cpp',hpp:'cpp',cc:'cpp',
  cs:'csharp',swift:'swift',scala:'scala',
  php:'php',pl:'perl',pm:'perl',r:'r',lua:'lua',
  sh:'bash',bash:'bash',zsh:'bash',fish:'bash',
  ps1:'powershell',psm1:'powershell',
  sql:'sql',graphql:'graphql',
  json:'json',yaml:'yaml',yml:'yaml',toml:'toml',xml:'xml',
  html:'markup',htm:'markup',svg:'markup',vue:'markup',
  css:'css',scss:'scss',sass:'sass',less:'less',
  md:'markdown',markdown:'markdown',
  dockerfile:'docker',makefile:'makefile',cmake:'cmake',
  ini:'ini',cfg:'ini',conf:'ini',properties:'properties',
  diff:'diff',patch:'diff',
  txt:'',log:'',csv:'',tsv:'',
};
const _PRISM_BASENAME_LANG_MAP={
  'dockerfile':'docker','makefile':'makefile','gnumakefile':'makefile',
  'cmakelists.txt':'cmake',
  '.gitignore':'ignore','.dockerignore':'ignore',
};
function _prismLanguageForPath(path){
  const base=String(path||'').split(/[\\/]/).pop().toLowerCase();
  if(base.startsWith('dockerfile.')) return 'docker';
  if(_PRISM_BASENAME_LANG_MAP[base]!==undefined) return _PRISM_BASENAME_LANG_MAP[base];
  const ext=fileExt(path).replace(/^\./,'');
  return _PRISM_LANG_MAP[ext]!==undefined?_PRISM_LANG_MAP[ext]:'plaintext';
}

async function openFile(path, opts={}){
  if(!S.session)return;
  const ext=fileExt(path);
  const bustCache=!!(opts&&opts.bustCache);
  const forceRichMarkdown=!!(opts&&opts.forceRichMarkdown);
  const cacheBust=bustCache?`&_=${Date.now()}`:'';

  // Binary/download-only formats: trigger browser download, don't preview
  if(DOWNLOAD_EXTS.has(ext)){
    downloadFile(path);
    return;
  }

  $('previewPathText').textContent=path;
  $('previewArea').classList.add('visible');
  $('fileTree').style.display='none';

  _previewCurrentPath = path;
  _previewSource = 'workspace';
  renderFileBreadcrumb(path);
  if(IMAGE_EXTS.has(ext)){
    // Image: load via raw endpoint, show as <img>
    showPreview('image');
    const url=`api/file/raw?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(path)}${cacheBust}`;
    $('previewImg').alt=path;
    $('previewImg').src=url;
    $('previewImg').onerror=()=>setStatus(t('image_load_failed'));
  } else if(AUDIO_EXTS.has(ext)||VIDEO_EXTS.has(ext)){
    const mode=VIDEO_EXTS.has(ext)?'video':'audio';
    showPreview(mode);
    const url=`api/file/raw?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(path)}&inline=1${cacheBust}`;
    const wrap=$('previewMediaWrap');
    if(wrap){
      wrap.innerHTML=(typeof _mediaPlayerHtml==='function')
        ? _mediaPlayerHtml(mode,url,path.split('/').pop()||path)
        : `<${mode} src="${url.replace(/"/g,'%22')}" controls preload="metadata"></${mode}>`;
      if(typeof _applyMediaPlaybackPreferences==='function') _applyMediaPlaybackPreferences(wrap);
    }
  } else if(PDF_EXTS.has(ext)){
    showPreview('pdf');
    const url=`api/file/raw?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(path)}&inline=1${cacheBust}`;
    const frame=$('previewPdfFrame');
    if(frame){
      frame.src=''; // clear first to avoid stale content
      frame.src=url;
      frame.title=`PDF preview: ${path.split('/').pop()||path}`;
    }
  } else if(MD_EXTS.has(ext)){
    // Markdown: fetch text, render with renderMd, display as formatted HTML
    try{
      // #3378 review (Codex): only reuse cached raw content when it actually
      // belongs to the requested path. `path===_previewCurrentPath` is tautological
      // here (_previewCurrentPath was just assigned above), so guard on the
      // dedicated _previewRawContentPath instead — otherwise a force-render after a
      // file switch could re-render the previous file's cached content.
      const data=forceRichMarkdown&&path===_previewRawContentPath&&_previewRawContent
        ? {content:_previewRawContent}
        : await api(`/api/file?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(path)}`);
      _previewRawContent = data.content;
      _previewRawContentPath = path;
      if(!forceRichMarkdown && shouldRenderMarkdownPreviewAsPlainText(data.content)){
        showPreview('code');
        $('previewCode').textContent=data.content;
        setLargeMarkdownForceRenderVisible(true);
        setStatus(largeMarkdownPlainTextStatus(data.content));
        return;
      }
      renderMarkdownPreviewContent(data);
    }catch(e){setStatus(t('file_open_failed'));}
  } else if(HTML_EXTS.has(ext)){
    // HTML: render in sandboxed iframe via raw endpoint.
    // SECURITY TRADEOFF: We use sandbox="allow-scripts" which lets inline JS run
    // but prevents access to the parent frame (origin isolation). This is a
    // deliberate choice — the user is previewing their own workspace files, so
    // blocking scripts entirely would break most HTML documents. The sandbox
    // still prevents the preview from navigating the parent, accessing cookies,
    // or reading other origin data. If a stricter mode is needed, remove
    // allow-scripts (or add sandbox="") to disable all JS execution.
    showPreview('html');
    const url=`api/file/raw?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(path)}&inline=1${cacheBust}`;
    const iframe=$('previewHtmlIframe');
    if(iframe){
      iframe.src=''; // clear first to avoid stale content
      iframe.src=url;
    }
  } else {
    // Plain code / text -- but fall back to download if server signals binary
    try{
      const data=await api(`/api/file?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(path)}`);
      if(data.binary){
        // Server flagged this as binary content
        downloadFile(path);
        return;
      }
      showPreview('code');
      // Syntax highlighting with Prism.js (already loaded on the page).
      const codeEl=document.createElement('code');
      codeEl.textContent=data.content;
      const lang=_prismLanguageForPath(path);
      if(lang) codeEl.className='language-'+lang;
      const pre=$('previewCode');
      pre.textContent='';
      // Prism.highlightElement() propagates the language-* class onto the
      // parent <pre>, so a previously-previewed code file leaves e.g.
      // "language-css" on #previewCode. A subsequent plain-text file builds a
      // class-less <code>, and Prism walks up to that stale ancestor class and
      // mis-highlights prose. Strip any inherited language-* token from the
      // <pre> before each render so highlighting never leaks across files.
      pre.className=pre.className.replace(/\blanguage-\S+/g,'').replace(/\s+/g,' ').trim();
      pre.appendChild(codeEl);
      // Only invoke Prism when we actually assigned a language; otherwise the
      // class-less <code> would inherit any ancestor language-* class.
      if(lang&&typeof Prism!=='undefined'&&typeof Prism.highlightElement==='function'){
        Prism.highlightElement(codeEl);
      }
    }catch(e){
      // If it's a 400/too-large error, offer download instead
      downloadFile(path);
    }
  }
}

function downloadFile(path){
  if(!S.session)return;
  // Trigger browser download via the raw file endpoint with content-disposition attachment
  const url=`api/file/raw?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(path)}&download=1`;
  const filename=path.split('/').pop();
  const a=document.createElement('a');
  a.href=url;a.download=filename;
  document.body.appendChild(a);a.click();
  setTimeout(()=>document.body.removeChild(a),100);
  showToast(t('downloading',filename),2000);
}


// ── Render breadcrumb for file preview mode ──────────────────────────────────
function renderFileBreadcrumb(filePath, opts) {
  opts = opts || {};
  const isSkill = opts.skill || _previewSource === 'skill';
  const bar = $('breadcrumbBar');
  if (!bar) return;
  bar.style.display = 'flex';
  const upBtn = $('btnUpDir');
  if (upBtn) upBtn.style.display = isSkill ? 'none' : '';

  bar.innerHTML = '';
  if(isSkill){
    const parts = filePath.split('/');
    for (let i = 0; i < parts.length; i++) {
      if(i > 0){
        const sep = document.createElement('span');
        sep.className = 'breadcrumb-sep';
        sep.textContent = '/';
        bar.appendChild(sep);
      }
      const seg = document.createElement('span');
      seg.textContent = parts[i];
      seg.className = i < parts.length - 1 ? 'breadcrumb-seg' : 'breadcrumb-seg breadcrumb-current';
      bar.appendChild(seg);
    }
    return;
  }
  // Root
  const root = document.createElement('span');
  root.className = 'breadcrumb-seg breadcrumb-link';
  root.textContent = '~';
  root.onclick = () => { loadDir('.'); };
  bar.appendChild(root);

  const parts = filePath.split('/');
  let accumulated = '';
  for (let i = 0; i < parts.length; i++) {
    const sep = document.createElement('span');
    sep.className = 'breadcrumb-sep';
    sep.textContent = '/';
    bar.appendChild(sep);

    accumulated += (accumulated ? '/' : '') + parts[i];
    const seg = document.createElement('span');
    seg.textContent = parts[i];
    if (i < parts.length - 1) {
      seg.className = 'breadcrumb-seg breadcrumb-link';
      const target = accumulated;
      seg.onclick = () => { loadDir(target); };
    } else {
      seg.className = 'breadcrumb-seg breadcrumb-current';
    }
    bar.appendChild(seg);
  }
}

function openInBrowser(){
  if(_previewCurrentMode==='browser'&&_previewBrowserUrl){
    window.open(_previewBrowserUrl,'_blank','noopener,noreferrer');
    return;
  }
  if(!_previewCurrentPath||!S.session) return;
  const url=`api/file/raw?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(_previewCurrentPath)}&inline=1`;
  window.open(url,'_blank','noopener');
}

async function copyPreviewRelativePath(){
  if(!_previewCurrentPath) return;
  const btn=$('btnCopyPreviewRelPath');
  if(btn&&btn.disabled) return;
  if(btn) btn.disabled=true;
  try{
    const rel=_normalizeWorkspaceRelPath(_previewCurrentPath)||_previewCurrentPath;
    if(typeof _copyTextWithFallback==='function'){
      await _copyTextWithFallback(rel,t('path_copied'),t('path_copy_failed'));
      return;
    }
    try{
      await navigator.clipboard.writeText(rel);
      showToast(t('path_copied'));
    }catch(clipErr){
      const ta=document.createElement('textarea');
      ta.value=rel;
      ta.style.cssText='position:fixed;left:-9999px;top:-9999px;';
      document.body.appendChild(ta);
      ta.select();
      let copied=false;
      try{copied=document.execCommand('copy');}catch(_){}
      ta.remove();
      if(copied) showToast(t('path_copied'));
      else showToast(t('path_copy_failed')+(clipErr&&clipErr.message?clipErr.message:String(clipErr)));
    }
  }catch(err){
    showToast(t('path_copy_failed')+(err.message||err));
  }finally{
    if(btn) btn.disabled=false;
  }
}

// ── Workspace upload ──────────────────────────────────────────────────
function triggerWorkspaceUpload() {
  const input = $('workspaceFileInput');
  if (!input) return;
  input.value = '';
  input.onchange = async () => {
    const files = input.files;
    if (!files || !files.length) return;
    for (const file of files) {
      await uploadToWorkspace(file, S.currentDir || '.');
    }
    if (S.session) loadDir(S.currentDir);
  };
  input.click();
}

async function uploadToWorkspace(file, dir) {
  if (!S.session) return;
  const formData = new FormData();
  formData.append('session_id', S.session.session_id);
  formData.append('path', dir || '.');
  formData.append('file', file, file.name);
  try {
    showToast(t('uploading') || 'Uploading\u2026', 2000);
    const data = await api('/api/workspace/upload', {
      method: 'POST',
      body: formData,
      headers: {},
      timeoutMs: 120000,
    });
    if (data && data.error) {
      showToast(data.error, 5000, 'error');
    } else if (data && (data.extract_error || (Array.isArray(data.files) && data.files.some(function(f){return f && f.extract_error;})))) {
      // Archive was rejected (zip-slip / zip-bomb / corrupt / too-many-members):
      // the file uploaded but extraction failed. Surface it as an error instead
      // of a misleading "Uploaded" success toast.
      var msg = data.extract_error
        || (data.files.find(function(f){return f && f.extract_error;}) || {}).extract_error
        || 'Archive extraction failed';
      showToast(msg, 5000, 'error');
    } else {
      showToast(t('uploaded') || ('Uploaded ' + (data.filename || file.name)), 2000);
    }
  } catch (e) {
    showToast(t('upload_failed') || ('Upload failed: ' + e.message), 5000, 'error');
  }
}

function _isOsFilesDrag(e) {
  return !!(e.dataTransfer && e.dataTransfer.types && e.dataTransfer.types.includes('Files'));
}

function _joinWorkspacePath(base, rel) {
  const b = base || '.';
  const r = (rel || '').replace(/^\/+|\/+$/g, '');
  if (!r) return b;
  return b === '.' ? r : `${b}/${r}`;
}

function _targetDirForRelDir(destDir, relDir) {
  const dirPart = (relDir || '').replace(/\/+$/, '');
  if (!dirPart) return destDir || '.';
  return _joinWorkspacePath(destDir, dirPart);
}

async function _readAllDirectoryEntries(reader) {
  const entries = [];
  while (true) {
    const batch = await new Promise((resolve, reject) => {
      reader.readEntries(resolve, reject);
    });
    if (!batch.length) break;
    entries.push(...batch);
  }
  return entries;
}

async function _collectFilesFromEntry(entry, relPrefix) {
  if (entry.isFile) {
    const file = await new Promise((resolve, reject) => {
      entry.file(resolve, reject);
    });
    return [{ file, relDir: relPrefix || '' }];
  }
  if (!entry.isDirectory) return [];
  const reader = entry.createReader();
  const children = await _readAllDirectoryEntries(reader);
  const dirPrefix = `${relPrefix || ''}${entry.name}/`;
  let out = [];
  for (const child of children) {
    out = out.concat(await _collectFilesFromEntry(child, dirPrefix));
  }
  return out;
}

async function _collectOsDropUploads(dataTransfer) {
  const out = [];
  const items = dataTransfer.items ? [...dataTransfer.items] : [];
  if (items.length && typeof items[0].webkitGetAsEntry === 'function') {
    for (const item of items) {
      if (item.kind !== 'file') continue;
      const entry = item.webkitGetAsEntry();
      if (!entry) continue;
      out.push(...await _collectFilesFromEntry(entry, ''));
    }
    if (out.length) return out;
  }
  for (const file of dataTransfer.files) {
    out.push({ file, relDir: '' });
  }
  return out;
}

async function uploadOsDropToWorkspace(dataTransfer, destDir) {
  if (!S.session || !dataTransfer) return;
  const uploads = await _collectOsDropUploads(dataTransfer);
  for (const { file, relDir } of uploads) {
    await uploadToWorkspace(file, _targetDirForRelDir(destDir, relDir));
  }
  if (S.session) await loadDir(S.currentDir);
}

function _clearWorkspaceOsUploadDragOver() {
  document.querySelectorAll('.file-item.drag-over-upload,.breadcrumb-seg.drag-over-upload').forEach((el) => {
    el.classList.remove('drag-over-upload');
  });
}

function _bindWorkspaceOsUploadDropTarget(el, destDir) {
  // Use addEventListener (not on-property assignment) so these OS-upload
  // handlers COMPOSE with the workspace tree-MOVE handlers bound by
  // _bindWorkspaceMoveDropTarget() on the same element. A property assignment
  // for the drop handler here would overwrite the move handler, and a
  // workspace-file drag would fall through to the document drop (inserting
  // @path into the composer) instead of moving the file. Each handler gates on
  // its own drag type (_isOsFilesDrag vs _isWorkspaceTreeMoveDrag), so only the
  // matching one acts.
  el.addEventListener('dragenter', (e) => {
    if (!_isOsFilesDrag(e)) return;
    e.preventDefault();
    e.stopPropagation();
    el.classList.add('drag-over-upload');
  });
  el.addEventListener('dragover', (e) => {
    if (!_isOsFilesDrag(e)) return;
    e.preventDefault();
    e.stopPropagation();
    e.dataTransfer.dropEffect = 'copy';
    el.classList.add('drag-over-upload');
  });
  el.addEventListener('dragleave', (e) => {
    if (el.contains(e.relatedTarget)) return;
    el.classList.remove('drag-over-upload');
  });
  el.addEventListener('drop', async (e) => {
    if (!_isOsFilesDrag(e)) return;
    e.preventDefault();
    e.stopPropagation();
    el.classList.remove('drag-over-upload');
    await uploadOsDropToWorkspace(e.dataTransfer, destDir);
  });
}

// Drag-and-drop files onto workspace file tree
if (typeof document !== 'undefined') {
  const _wsUploadInit = () => {
    const tree = $('fileTree');
    if (!tree) return;
    tree.addEventListener('dragenter', (e) => {
      if (e.dataTransfer && e.dataTransfer.types && e.dataTransfer.types.includes('Files')) {
        e.preventDefault();
        e.stopPropagation();
      }
    });
    tree.addEventListener('dragover', (e) => {
      if (e.dataTransfer && e.dataTransfer.types && e.dataTransfer.types.includes('Files')) {
        e.preventDefault();
        e.stopPropagation();
        if (e.target.closest('.file-item[data-ws-type="dir"],.breadcrumb-seg')) return;
        e.dataTransfer.dropEffect = 'copy';
        tree.classList.add('drag-over-upload');
      }
    });
    tree.addEventListener('dragleave', (e) => {
      if (tree.contains(e.relatedTarget)) return;
      tree.classList.remove('drag-over-upload');
    });
    tree.addEventListener('drop', async (e) => {
      tree.classList.remove('drag-over-upload');
      if (!e.dataTransfer || !e.dataTransfer.types || !e.dataTransfer.types.includes('Files')) return;
      if (e.target.closest('.file-item[data-ws-type="dir"],.breadcrumb-seg')) return;
      e.preventDefault();
      e.stopPropagation();
      await uploadOsDropToWorkspace(e.dataTransfer, S.currentDir || '.');
    });
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _wsUploadInit, {once: true});
  } else {
    _wsUploadInit();
  }
}
