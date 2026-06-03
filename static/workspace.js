async function api(path,opts={}){
  // Strip leading slash so URL resolves relative to location.href (supports subpath mounts)
  const rel = path.startsWith('/') ? path.slice(1) : path;
  const url=new URL(rel,document.baseURI||location.href);
  const timeoutMs=Object.prototype.hasOwnProperty.call(opts,'timeoutMs')?opts.timeoutMs:30000;
  const timeoutToast=opts.timeoutToast!==false;
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
          if(res.status===401){window.location.href='login?next='+encodeURIComponent(window.location.pathname+window.location.search);return;}
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
    if(!path) return;
    const current = byPath.get(path) || {};
    const merged = {...current, ..._cloneManifestValue(row)};
    const hits = [];
    const seen = new Set();
    [...(current.hits||[]), ...(row.hits||[])].forEach(hit=>{
      if(!hit||typeof hit!=='object') return;
      const key = [hit.path||path, hit.source_tool||'', hit.tid||'', hit.assistant_msg_idx??'', hit.tool_msg_idx??''].join('|');
      if(seen.has(key)) return;
      seen.add(key);
      hits.push(_cloneManifestValue(hit));
    });
    if(hits.length){
      merged.hits = hits;
      merged.hit_count = hits.length;
    }
    byPath.set(path, merged);
  };
  (existing||[]).forEach(add);
  (incoming||[]).forEach(add);
  return [...byPath.values()].sort((a,b)=>String(a.path||'').localeCompare(String(b.path||'')));
}

function _currentLiveTurnKey(streamId){
  if(streamId && S._manifestLiveTurnKeys && S._manifestLiveTurnKeys[streamId]) return S._manifestLiveTurnKeys[streamId];
  let idx = -1;
  const msgs = S.messages || [];
  for(let i=msgs.length-1;i>=0;i--){
    if(msgs[i]&&msgs[i].role==='user'){ idx=i; break; }
  }
  const key = idx>=0 ? `turn:${idx}` : (streamId ? `live:${streamId}` : '');
  if(streamId){
    S._manifestLiveTurnKeys = S._manifestLiveTurnKeys || {};
    S._manifestLiveTurnKeys[streamId] = key;
  }
  return key;
}

function _normalizeDeltaTurnKey(delta){
  const streamId = delta && delta.stream_id;
  const key = String(delta && delta.turn_key || '');
  if(key.startsWith('live:')) return _currentLiveTurnKey(streamId);
  return key || _currentLiveTurnKey(streamId);
}

function _mergeManifestTurns(existingTurns, incomingTurns){
  const byKey = new Map();
  const add = (turn) => {
    if(!turn||typeof turn!=='object') return;
    const key = String(turn.turn_key||'').trim();
    if(!key) return;
    const current = byKey.get(key) || {turn_key:key, artifacts:[], references:[]};
    byKey.set(key, {
      ...current,
      ..._cloneManifestValue(turn),
      artifacts: _mergeManifestRows(current.artifacts, turn.artifacts),
      references: _mergeManifestRows(current.references, turn.references),
    });
  };
  (existingTurns||[]).forEach(add);
  (incomingTurns||[]).forEach(add);
  return [...byKey.values()].sort((a,b)=>{
    const ai = Number.isInteger(a.user_msg_idx) ? a.user_msg_idx : 1000000000;
    const bi = Number.isInteger(b.user_msg_idx) ? b.user_msg_idx : 1000000000;
    return ai===bi ? String(a.turn_key||'').localeCompare(String(b.turn_key||'')) : ai-bi;
  });
}

function _manifestCounts(manifest){
  const todos = manifest && manifest.todos && Array.isArray(manifest.todos.items) ? manifest.todos.items : [];
  return {
    todos: todos.length,
    artifacts: Array.isArray(manifest&&manifest.artifacts) ? manifest.artifacts.length : 0,
    references: Array.isArray(manifest&&manifest.references) ? manifest.references.length : 0,
    turns: Array.isArray(manifest&&manifest.turns) ? manifest.turns.length : 0,
  };
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
      session_id: S.session.session_id,
      workspace: S.session.workspace || '',
      todos: {items:[]},
      artifacts: [],
      references: [],
      turns: [],
      counts: {todos:0, artifacts:0, references:0, turns:0},
    };
    _sessionManifestSid = S.session.session_id;
  }
  const turnKey = _normalizeDeltaTurnKey(delta);
  S._lastManifestDeltaTurnKey = turnKey;
  const incomingTurns = Array.isArray(delta.turns) ? delta.turns : (turnKey ? [{
    turn_key: turnKey,
    artifacts: delta.artifacts || [],
    references: delta.references || [],
    ...(delta.todos ? {todo_snapshot: delta.todos} : {}),
  }] : []);
  if(delta.todos && Array.isArray(delta.todos.items)){
    _sessionManifest.todos = _cloneManifestValue(delta.todos);
  }
  _sessionManifest.artifacts = _mergeManifestRows(_sessionManifest.artifacts, delta.artifacts);
  _sessionManifest.references = _mergeManifestRows(_sessionManifest.references, delta.references);
  _sessionManifest.turns = _mergeManifestTurns(_sessionManifest.turns, incomingTurns);
  _sessionManifest.live = delta.stream_id ? {stream_id: delta.stream_id, source:'sse'} : _sessionManifest.live;
  _sessionManifest.counts = _manifestCounts(_sessionManifest);
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
    `<div class="turn-artifacts-list">${items.map(item=>{
      const path = item.path || '';
      const source = item.source_tool || '';
      const disabled = item.preview && item.preview.previewable === false;
      return `<button type="button" class="turn-artifact-chip${disabled?' is-disabled':''}" data-path="${esc(path)}"${disabled?' disabled':''}>${esc(path)}${source?`<span>${esc(source)}</span>`:''}</button>`;
    }).join('')}</div>`;
  root.querySelectorAll('.turn-artifact-chip:not(.is-disabled)').forEach(btn=>{
    btn.onclick=()=>openArtifactPath(btn.dataset.path||'');
  });
}

function _inspectorFileMeta(item){
  const preview = item && item.preview;
  if(!preview) return '';
  if(preview.kind === 'dir') return _workspaceInspectorLabel('workspace_ref_dir', 'directory');
  if(!preview.in_workspace) return _workspaceInspectorLabel('workspace_outside_workspace', 'outside workspace');
  if(preview.exists === false) return _workspaceInspectorLabel('workspace_file_missing', 'not found');
  if(preview.previewable) return _workspaceInspectorLabel('workspace_preview_ready', 'preview');
  return '';
}

function _renderInspectorFileList(root, items, emptyKey, emptyFallback, onClickAttr){
  if(!root) return;
  if(!S.session){
    root.innerHTML = `<div class="workspace-inspector-empty">${esc(_workspaceInspectorLabel('workspace_inspector_no_session', 'Open a conversation to inspect session files.'))}</div>`;
    return;
  }
  if(!items.length){
    root.innerHTML = `<div class="workspace-inspector-empty">${esc(_workspaceInspectorLabel(emptyKey, emptyFallback))}</div>`;
    return;
  }
  root.innerHTML = items.map(item=>{
    const path = item.path || '';
    const metaBits = [item.source_tool || '', _inspectorFileMeta(item)].filter(Boolean);
    const previewable = !!(item.preview && item.preview.previewable);
    const cls = previewable ? 'workspace-inspector-item' : 'workspace-inspector-item is-disabled';
    const onclick = previewable ? ` ${onClickAttr}="${esc(path)}"` : '';
    return `<button type="button" class="${cls}" data-path="${esc(path)}"${onclick}><div class="workspace-inspector-path">${esc(path)}</div><div class="workspace-inspector-meta">${esc(metaBits.join(' · '))}</div></button>`;
  }).join('');
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
    preview: {previewable: true, in_workspace: true, exists: true, kind: 'file'},
  }));
  if(count) count.textContent = String(items.length);
  _renderInspectorFileList(
    root,
    items,
    'workspace_artifacts_empty',
    'No artifacts detected yet. Files created or edited during this session will appear here.',
    'onclick',
  );
  if(root){
    root.querySelectorAll('.workspace-inspector-item:not(.is-disabled)').forEach(btn=>{
      btn.onclick = ()=> openArtifactPath(btn.dataset.path || btn.getAttribute('data-path'));
    });
  }
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
    'onclick',
  );
  if(root){
    root.querySelectorAll('.workspace-inspector-item:not(.is-disabled)').forEach(btn=>{
      btn.onclick = ()=> openInspectorReferencePath(btn.dataset.path || btn.getAttribute('data-path'));
    });
  }
}

function renderSessionInspector(){
  renderSessionTasks();
  renderSessionArtifacts();
  renderSessionReferences();
}

async function openInspectorReferencePath(path){
  if(!path) return;
  const manifest = _manifestForActiveSession();
  const row = (manifest && manifest.references || []).find(item=>item.path === path);
  const previewable = row && row.preview && row.preview.previewable;
  if(!previewable){
    setStatus(_workspaceInspectorLabel('workspace_preview_unavailable', 'Preview unavailable for this path.'));
    return;
  }
  switchWorkspacePanelTab('files');
  await openArtifactPath(path);
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
};

const ARTIFACT_IGNORE_RE = /(^|\/)(?:\.git|\.hg|\.svn|node_modules|\.venv|venv|__pycache__|dist|build|\.next|\.cache)(?:\/|$)/;
// Canonical Hermes mutators plus MCP filesystem aliases that can create/edit files.
const ARTIFACT_MUTATION_TOOLS = new Set(['write_file','patch','edit_file','create_file','mcp_filesystem_write_file','mcp_filesystem_edit_file']);

function _normalizeArtifactPath(path){
  if(!path) return '';
  path = String(path).trim().replace(/[\`"'<>),.;:]+$/g,'').replace(/^[\`"'(<]+/g,'');
  if(!path || path.length > 240 || path.includes('://')) return '';
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

function collectSessionArtifacts(){
  const items = [];
  const seen = new Set();
  const push = (path, source) => {
    path = _normalizeArtifactPath(path);
    if(!path || seen.has(path)) return;
    seen.add(path); items.push({path, source});
  };
  for(const tc of (S.toolCalls || [])){
    for(const a of _artifactCandidatesFromToolCall(tc)) push(a.path, a.kind || tc.name || 'tool');
  }
  for(const msg of (S.messages || [])){
    const text = msg && (msg.content || msg.text || msg.message || '');
    for(const a of _artifactCandidatesFromText(text)) push(a.path, a.kind);
  }
  return items.slice(0, 50);
}

async function _workspacePathExists(path){
  if(!S.session||!path) return false;
  const parts=String(path).split('/').filter(Boolean);
  const name=parts.pop();
  if(!name) return false;
  const dir=parts.length?parts.join('/'):'.';
  const data=await api(`/api/list?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(dir)}`);
  return (data.entries||[]).some(entry=>entry&&((entry.path===path)||entry.name===name));
}

async function openArtifactPath(path){
  if(!path) return;
  switchWorkspacePanelTab('files');
  const rel = path.replace(/^~\//,'').replace(/^\.\//,'');
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

async function loadDir(path){
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
            .then(dc=>({dirPath,entries:dc.entries||[]}))
            .catch(()=>({dirPath,entries:[]}))
        ));
        if(!S.session||S.session.session_id!==sessionId)return;
        for(const {dirPath,entries} of results) S._dirCache[dirPath]=entries;
      }
      if(expanded.size>0)renderFileTree();
    }
    if(typeof clearPreview==='function'){
      if(typeof _previewDirty!=='undefined'&&_previewDirty){
        showConfirmDialog({title:t('unsaved_confirm'),message:'',confirmLabel:'Discard',danger:true,focusCancel:true}).then(ok=>{if(ok)clearPreview({keepPanelOpen:true});});
      }else{
        clearPreview({keepPanelOpen:true});
      }
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
const MD_PREVIEW_RICH_RENDER_MAX_BYTES = 64 * 1024;
const MD_PREVIEW_RICH_RENDER_MAX_LINES = 1500;
// Binary formats that should download rather than preview
const DOWNLOAD_EXTS = new Set([
  '.docx','.doc','.xlsx','.xls','.pptx','.ppt','.odt','.ods','.odp',
  '.zip','.tar','.gz','.bz2','.7z','.rar',
  '.exe','.dmg','.pkg','.deb','.rpm',
  '.woff','.woff2','.ttf','.otf','.eot',
  '.bin','.dat','.db','.sqlite','.pyc','.class','.so','.dylib','.dll',
]);

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
  return `Large markdown file (${sizeLabel}, ${lines} lines) shown as plain text. Click Edit to view raw.`;
}

let _previewCurrentPath = '';  // relative path of currently previewed file
let _previewCurrentMode = '';  // 'code' | 'md' | 'image' | 'html' | 'pdf' | 'audio' | 'video'
let _previewDirty = false;     // true when edits are unsaved

function showPreview(mode){
  // mode: 'code' | 'image' | 'md' | 'html' | 'pdf' | 'audio' | 'video'
  $('previewCode').style.display     = mode==='code'  ? '' : 'none';
  $('previewImgWrap').style.display  = mode==='image' ? '' : 'none';
  const mediaWrap=$('previewMediaWrap'); if(mediaWrap) mediaWrap.style.display = (mode==='audio'||mode==='video') ? '' : 'none';
  const pdfWrap=$('previewPdfWrap'); if(pdfWrap) pdfWrap.style.display = mode==='pdf' ? '' : 'none';
  $('previewMd').style.display       = mode==='md'    ? '' : 'none';
  $('previewHtmlWrap').style.display = mode==='html'  ? '' : 'none';
  $('previewEditArea').style.display = 'none';  // start in read-only
  const badge=$('previewBadge');
  badge.className='preview-badge '+mode;
  badge.textContent = mode==='image'?'image':mode==='audio'?'audio':mode==='video'?'video':mode==='pdf'?'pdf':mode==='md'?'md':mode==='html'?'html':fileExt($('previewPathText').textContent)||'text';
  _previewCurrentMode = mode;
  _previewDirty = false;
  updateEditBtn();
  // Show "Open in browser" button for iframe-backed document previews
  const openBtn=$('btnOpenInBrowser');
  if(openBtn) openBtn.style.display = (mode==='html'||mode==='pdf')?'inline-flex':'none';
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
      // Update read-only views
      if(_previewCurrentMode==='code') $('previewCode').textContent=content;
      else { $('previewMd').innerHTML=renderMd(content); requestAnimationFrame(()=>{if(typeof renderKatexBlocks==='function')renderKatexBlocks();}); }
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

function cancelEditMode(){
  // Discard changes and return to read-only view
  $('previewEditArea').style.display='none';
  $('previewEditArea').onkeydown=null;
  if(_previewCurrentMode==='code') $('previewCode').style.display='';
  else $('previewMd').style.display='';
  _previewDirty=false;
  updateEditBtn();
}

async function openFile(path){
  if(!S.session)return;
  const ext=fileExt(path);

  // Binary/download-only formats: trigger browser download, don't preview
  if(DOWNLOAD_EXTS.has(ext)){
    downloadFile(path);
    return;
  }

  $('previewPathText').textContent=path;
  $('previewArea').classList.add('visible');
  $('fileTree').style.display='none';

  _previewCurrentPath = path;
  renderFileBreadcrumb(path);
  if(IMAGE_EXTS.has(ext)){
    // Image: load via raw endpoint, show as <img>
    showPreview('image');
    const url=`api/file/raw?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(path)}`;
    $('previewImg').alt=path;
    $('previewImg').src=url;
    $('previewImg').onerror=()=>setStatus(t('image_load_failed'));
  } else if(AUDIO_EXTS.has(ext)||VIDEO_EXTS.has(ext)){
    const mode=VIDEO_EXTS.has(ext)?'video':'audio';
    showPreview(mode);
    const url=`api/file/raw?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(path)}&inline=1`;
    const wrap=$('previewMediaWrap');
    if(wrap){
      wrap.innerHTML=(typeof _mediaPlayerHtml==='function')
        ? _mediaPlayerHtml(mode,url,path.split('/').pop()||path)
        : `<${mode} src="${url.replace(/"/g,'%22')}" controls preload="metadata"></${mode}>`;
      if(typeof _applyMediaPlaybackPreferences==='function') _applyMediaPlaybackPreferences(wrap);
    }
  } else if(PDF_EXTS.has(ext)){
    showPreview('pdf');
    const url=`api/file/raw?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(path)}&inline=1`;
    const frame=$('previewPdfFrame');
    if(frame){
      frame.src=''; // clear first to avoid stale content
      frame.src=url;
      frame.title=`PDF preview: ${path.split('/').pop()||path}`;
    }
  } else if(MD_EXTS.has(ext)){
    // Markdown: fetch text, render with renderMd, display as formatted HTML
    try{
      const data=await api(`/api/file?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(path)}`);
      _previewRawContent = data.content;
      if(shouldRenderMarkdownPreviewAsPlainText(data.content)){
        showPreview('code');
        $('previewCode').textContent=data.content;
        setStatus(largeMarkdownPlainTextStatus(data.content));
        return;
      }
      showPreview('md');
      $('previewMd').innerHTML=renderMd(data.content);
      requestAnimationFrame(()=>{if(typeof renderKatexBlocks==='function')renderKatexBlocks();});
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
    const url=`api/file/raw?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(path)}&inline=1`;
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
      $('previewCode').textContent=data.content;
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
function renderFileBreadcrumb(filePath) {
  const bar = $('breadcrumbBar');
  if (!bar) return;
  bar.style.display = 'flex';
  const upBtn = $('btnUpDir');
  if (upBtn) upBtn.style.display = '';

  bar.innerHTML = '';
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
  if(!_previewCurrentPath||!S.session) return;
  const url=`api/file/raw?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(_previewCurrentPath)}`;
  window.open(url,'_blank');
}
