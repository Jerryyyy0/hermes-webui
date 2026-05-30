(function () {
  'use strict';

  const cfg = window.__HERMES_CONFIG__ || {};
  if (!cfg.integrationCronAllProfiles) return;

  const shared = () => window.HermesCronShared || {};
  const T = (key, ...args) => (typeof t === 'function' ? t(key, ...args) : key);

  let _flat = [];
  let _selected = null;
  let _mode = 'empty';
  let _profiles = [];
  let _profilesCache = null;
  const _unread = new Set();

  function $(id) {
    return document.getElementById(id);
  }

  function esc(s) {
    return typeof escHtml === 'function' ? escHtml(s) : String(s ?? '');
  }

  function jobKey(ownerProfile, jobId) {
    return `${ownerProfile}:${jobId}`;
  }

  function panelExpandKey(ownerProfile, jobId, suffix) {
    return `hermes-webui-integration-cron-${suffix}-${encodeURIComponent(ownerProfile)}-${encodeURIComponent(jobId)}`;
  }

  function runExpandKey(ownerProfile, jobId, filename) {
    return `${panelExpandKey(ownerProfile, jobId, 'run')}-${encodeURIComponent(filename || '')}`;
  }

  function expansionGet(key) {
    try {
      return localStorage.getItem(key) === '1';
    } catch (_) {
      return false;
    }
  }

  function expansionSet(key, expanded) {
    try {
      localStorage.setItem(key, expanded ? '1' : '0');
    } catch (_) {}
  }

  function showNav() {
    ['integrationCronsRailBtn', 'integrationCronsSidebarBtn'].forEach(id => {
      const el = $(id);
      if (!el) return;
      el.hidden = false;
      el.classList.remove('nav-tab-hidden');
    });
    const panel = $('panelIntegrationCrons');
    if (panel) panel.hidden = false;
  }

  function flattenGroups(data) {
    const out = [];
    for (const group of data.profiles || []) {
      const owner = group.profile || 'default';
      for (const job of group.jobs || []) {
        out.push({ ownerProfile: owner, job });
      }
    }
    return out;
  }

  function applyFilters() {
    const q = ($('integrationCronSearch')?.value || '').trim().toLowerCase();
    const profileFilter = $('integrationCronProfileFilter')?.value || '';
    const statusFilter = $('integrationCronStatusFilter')?.value || '';
    return _flat.filter(row => {
      const job = row.job || {};
      if (profileFilter && row.ownerProfile !== profileFilter) return false;
      if (statusFilter === 'enabled' && job.enabled === false) return false;
      if (statusFilter === 'disabled' && job.enabled !== false) return false;
      if (!q) return true;
      const hay = [job.name, job.id, row.ownerProfile, job.profile, job.schedule].join(' ').toLowerCase();
      return hay.includes(q);
    });
  }

  function renderList() {
    const box = $('integrationCronList');
    if (!box) return;
    const rows = applyFilters();
    if (!rows.length) {
      box.innerHTML = `<div style="padding:16px;color:var(--muted);font-size:12px">${esc(T('cron_no_jobs'))}</div>`;
      return;
    }
    box.innerHTML = '';
    const statusMeta = shared().statusMeta;
    const profileLabel = shared().profileLabel;
    const profileTitle = shared().profileTitle;
    for (const row of rows) {
      const job = row.job;
      const key = jobKey(row.ownerProfile, job.id);
      const item = document.createElement('div');
      item.className = 'cron-item';
      item.dataset.key = key;
      const status = statusMeta ? statusMeta(job) : { listClass: '', label: '' };
      const isNewRun = _unread.has(key);
      const isAgentMode = !job.no_agent;
      const execLabel = profileLabel ? profileLabel(job.profile) : (job.profile || T('cron_profile_server_default'));
      const execTitle = profileTitle ? profileTitle(job.profile) : '';
      item.innerHTML = `
        <div class="cron-header">
          ${isNewRun ? '<span class="cron-new-dot" title="New run"></span>' : ''}
          ${isAgentMode ? '<span class="cron-agent-badge" title="Agent mode">🤖</span>' : ''}
          <span class="cron-name" title="${esc(job.name || job.id)}">${esc(job.name || job.id)}</span>
          <span class="cron-profile-badge integration-cron-owner-badge" title="${esc(T('integration_cron_owner_profile') || 'Owner profile')}: ${esc(row.ownerProfile)}">${esc(row.ownerProfile)}</span>
          <span class="cron-profile-badge" title="${esc(execTitle)}">${esc(execLabel)}</span>
          <span class="cron-status ${esc(status.listClass)}">${esc(status.label)}</span>
        </div>`;
      item.onclick = () => openDetail(row);
      if (_selected && _selected.key === key) item.classList.add('active');
      box.appendChild(item);
    }
  }

  async function ensureProfiles() {
    if (_profilesCache) return _profilesCache;
    if (shared().loadProfiles) {
      _profilesCache = await shared().loadProfiles();
    } else {
      try {
        const data = await api('/api/profiles');
        _profilesCache = Array.isArray(data.profiles) ? data.profiles : [];
      } catch (_) {
        _profilesCache = [];
      }
    }
    _profiles = _profilesCache.map(p => p.name).filter(Boolean);
    return _profilesCache;
  }

  async function loadProfilesForFilters() {
    await ensureProfiles();
    const sel = $('integrationCronProfileFilter');
    if (!sel) return;
    const current = sel.value;
    sel.innerHTML = `<option value="">${esc(T('integration_cron_all_profiles') || 'All profiles')}</option>`;
    for (const name of _profiles) {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      sel.appendChild(opt);
    }
    if (current) sel.value = current;
  }

  function ownerProfileOptions(selected) {
    const current = (selected || '').toString().trim();
    return _profiles
      .map(name => `<option value="${esc(name)}"${current === name ? ' selected' : ''}>${esc(name)}</option>`)
      .join('');
  }

  async function load() {
    const box = $('integrationCronList');
    if (!box) return;
    try {
      const data = await api('/api/crons?all_profiles=1');
      _flat = flattenGroups(data);
      renderList();
      await loadProfilesForFilters();
      if (_selected) {
        const refreshed = _flat.find(
          r => r.ownerProfile === _selected.ownerProfile && r.job.id === _selected.job.id
        );
        if (refreshed) {
          _selected = { ...refreshed, key: jobKey(refreshed.ownerProfile, refreshed.job.id) };
          if (_mode === 'read') renderDetailView(_selected);
        } else {
          clearDetail();
        }
      }
    } catch (e) {
      box.innerHTML = `<div style="padding:12px;color:var(--accent);font-size:12px">${esc(T('error_prefix'))}${esc(e.message)}</div>`;
    }
  }

  function setHeaderButtons(mode, job) {
    const runBtn = $('btnRunIntegrationCronDetail');
    const pauseBtn = $('btnPauseIntegrationCronDetail');
    const resumeBtn = $('btnResumeIntegrationCronDetail');
    const editBtn = $('btnEditIntegrationCronDetail');
    const delBtn = $('btnDeleteIntegrationCronDetail');
    const cancelBtn = $('btnCancelIntegrationCronDetail');
    const saveBtn = $('btnSaveIntegrationCronDetail');
    const hide = b => b && (b.style.display = 'none');
    const show = b => b && (b.style.display = '');
    if (mode === 'read') {
      show(runBtn);
      const statusMeta = shared().statusMeta;
      const status = job && statusMeta ? statusMeta(job) : null;
      const resumable =
        job &&
        (job.state === 'paused' ||
          (status && (status.state === 'needs_attention' || status.state === 'schedule_error')));
      if (resumable) {
        hide(pauseBtn);
        show(resumeBtn);
      } else {
        show(pauseBtn);
        hide(resumeBtn);
      }
      show(editBtn);
      show(delBtn);
      hide(cancelBtn);
      hide(saveBtn);
    } else if (mode === 'create' || mode === 'edit') {
      hide(runBtn);
      hide(pauseBtn);
      hide(resumeBtn);
      hide(editBtn);
      hide(delBtn);
      show(cancelBtn);
      show(saveBtn);
    } else {
      [runBtn, pauseBtn, resumeBtn, editBtn, delBtn, cancelBtn, saveBtn].forEach(hide);
    }
  }

  function clearDetail() {
    _selected = null;
    _mode = 'empty';
    const title = $('integrationCronDetailTitle');
    const body = $('integrationCronDetailBody');
    const empty = $('integrationCronDetailEmpty');
    if (title) title.textContent = '';
    if (body) {
      body.innerHTML = '';
      body.style.display = 'none';
    }
    if (empty) empty.style.display = '';
    setHeaderButtons('empty');
  }

  function togglePromptExpanded(ownerProfile, jobId) {
    const key = panelExpandKey(ownerProfile, jobId, 'prompt');
    expansionSet(key, !expansionGet(key));
    if (_selected && _mode === 'read') renderDetailView(_selected);
  }

  function toggleRunExpanded(ownerProfile, jobId, filename, runId) {
    const key = runExpandKey(ownerProfile, jobId, filename);
    const expanded = !expansionGet(key);
    expansionSet(key, expanded);
    const item = document.getElementById(runId);
    const body = item ? item.querySelector('.detail-run-body') : null;
    const btn = item ? item.querySelector('.detail-expand-toggle') : null;
    if (body) body.classList.toggle('expanded', expanded);
    if (btn) {
      btn.textContent = expanded ? '▴' : '▾';
      const label = expanded
        ? T('cron_collapse_output') || 'Collapse output'
        : T('cron_expand_output') || 'Expand output';
      btn.title = label;
      btn.setAttribute('aria-label', label);
    }
  }

  function renderDetailView(row) {
    const job = row.job;
    const title = $('integrationCronDetailTitle');
    const body = $('integrationCronDetailBody');
    const empty = $('integrationCronDetailEmpty');
    if (!title || !body) return;

    const statusMeta = shared().statusMeta;
    const profileLabel = shared().profileLabel;
    const profileTitle = shared().profileTitle;
    const status = statusMeta ? statusMeta(job) : { detailClass: '', label: '' };
    const nextRun = job.next_run_at ? new Date(job.next_run_at).toLocaleString() : T('not_available');
    const lastRun = job.last_run_at ? new Date(job.last_run_at).toLocaleString() : T('never');
    const schedule = job.schedule_display || (job.schedule && job.schedule.expression) || job.schedule || '';
    const skills = Array.isArray(job.skills) && job.skills.length ? job.skills.join(', ') : '—';
    const deliver = job.deliver || 'local';
    const isNoAgent = !!job.no_agent;
    const cronJobMode = isNoAgent ? 'no-agent' : 'agent';
    const modelProvider =
      job.provider && job.model
        ? `${esc(job.provider)}/${esc(job.model)}`
        : job.model
          ? esc(job.model)
          : job.provider
            ? esc(job.provider)
            : isNoAgent
              ? ''
              : 'default';
    const script = job.script || '';
    const execLabel = profileLabel ? profileLabel(job.profile) : job.profile || T('cron_profile_server_default');
    const execTitle = profileTitle ? profileTitle(job.profile) : '';
    const lastError = job.last_error
      ? `<div class="detail-row"><div class="detail-row-label">${esc(T('error_prefix').replace(/:\s*$/, ''))}</div><div class="detail-row-value" style="color:var(--accent-text)">${esc(job.last_error)}</div></div>`
      : '';
    const toastNotifications = job.toast_notifications !== false;
    const promptExpanded = expansionGet(panelExpandKey(row.ownerProfile, job.id, 'prompt'));
    const promptToggleLabel = promptExpanded
      ? T('cron_collapse_prompt') || 'Collapse prompt'
      : T('cron_expand_prompt') || 'Expand prompt';

    title.textContent = job.name || job.schedule_display || job.id;
    body.innerHTML = `
      <div class="main-view-content">
        <div class="detail-card">
          <div class="detail-card-title">${esc(T('cron_status_active').replace(/./, c => c.toUpperCase()))}</div>
          <div class="detail-row"><div class="detail-row-label">${esc(T('integration_cron_owner_profile') || 'Owner profile')}</div><div class="detail-row-value"><span class="detail-badge active">${esc(row.ownerProfile)}</span></div></div>
          <div class="detail-row"><div class="detail-row-label">Status</div><div class="detail-row-value"><span class="detail-badge ${esc(status.detailClass)}">${esc(status.label)}</span></div></div>
          <div class="detail-row"><div class="detail-row-label">Schedule</div><div class="detail-row-value"><code>${esc(schedule)}</code></div></div>
          <div class="detail-row"><div class="detail-row-label">${esc(T('cron_next'))}</div><div class="detail-row-value">${esc(nextRun)}</div></div>
          <div class="detail-row"><div class="detail-row-label">${esc(T('cron_last'))}</div><div class="detail-row-value">${esc(lastRun)}</div></div>
          <div class="detail-row"><div class="detail-row-label">Deliver</div><div class="detail-row-value">${esc(deliver)}</div></div>
          <div class="detail-row"><div class="detail-row-label">Mode</div><div class="detail-row-value"><span class="detail-badge" id="integrationCronJobMode">${esc(cronJobMode)}</span>${modelProvider ? ` <code>${modelProvider}</code>` : ''}</div></div>
          ${isNoAgent ? `<div class="detail-row"><div class="detail-row-label">No-agent script</div><div class="detail-row-value"><code>${esc(script || '—')}</code></div></div>` : ''}
          <div class="detail-row"><div class="detail-row-label">${esc(T('cron_profile_label') || 'Execution profile')}</div><div class="detail-row-value"><span class="detail-badge active" title="${esc(execTitle)}">${esc(execLabel)}</span></div></div>
          <div class="detail-row"><div class="detail-row-label">${esc(T('cron_toast_notifications_label') || 'Completion toasts')}</div><div class="detail-row-value"><span class="detail-badge ${toastNotifications ? 'active' : ''}">${esc(toastNotifications ? T('cron_toast_notifications_enabled') || 'Enabled' : T('cron_toast_notifications_disabled') || 'Disabled')}</span></div></div>
          <div class="detail-row"><div class="detail-row-label">Skills</div><div class="detail-row-value">${esc(skills)}</div></div>
          ${lastError}
        </div>
        <div class="detail-card">
          <div class="detail-card-title detail-card-title-row">
            <span>Prompt</span>
            <button type="button" class="detail-expand-toggle" data-action="toggle-prompt" title="${esc(promptToggleLabel)}" aria-label="${esc(promptToggleLabel)}">${esc(promptExpanded ? '▴' : '▾')}</button>
          </div>
          <div class="detail-prompt ${promptExpanded ? 'expanded' : ''}">${esc(job.prompt || '')}</div>
        </div>
        <div class="detail-card ${_unread.has(row.key) ? 'has-new-run' : ''}" id="integrationCronDetailRuns">
          <div class="detail-card-title">${esc(T('cron_last_output'))}</div>
          <div style="color:var(--muted);font-size:12px">${esc(T('loading'))}</div>
        </div>
      </div>`;
    body.style.display = '';
    if (empty) empty.style.display = 'none';
    _mode = 'read';
    setHeaderButtons('read', job);
    body.querySelector('[data-action="toggle-prompt"]')?.addEventListener('click', () => {
      togglePromptExpanded(row.ownerProfile, job.id);
    });
    loadDetailRuns(row);
    _unread.delete(row.key);
    renderList();
  }

  async function loadDetailRuns(row) {
    const jobId = row.job.id;
    const ownerProfile = row.ownerProfile;
    try {
      const data = await api(
        `/api/crons/history?job_id=${encodeURIComponent(jobId)}&owner_profile=${encodeURIComponent(ownerProfile)}&limit=50`
      );
      if (!_selected || _selected.job.id !== jobId || _selected.ownerProfile !== ownerProfile) return;
      const card = $('integrationCronDetailRuns');
      if (!card) return;
      if (!data.runs || !data.runs.length) {
        card.innerHTML = `<div class="detail-card-title">${esc(T('cron_last_output'))}</div><div style="color:var(--muted);font-size:12px">${esc(T('cron_no_runs_yet'))}</div>`;
        return;
      }
      const formatUsage = shared().formatRunUsageStrip || (() => '');
      const rows = data.runs
        .map((run, i) => {
          const ts = String(run.filename || '').replace('.md', '').replace(/_/g, ' ');
          const sizeStr = run.size > 1024 ? (run.size / 1024).toFixed(1) + ' KB' : run.size + ' B';
          const rid = `integration-cron-run-${encodeURIComponent(ownerProfile)}-${jobId}-${i}`;
          const usageStrip = formatUsage(run.usage);
          const runExpanded = expansionGet(runExpandKey(ownerProfile, jobId, run.filename));
          const runToggleLabel = runExpanded
            ? T('cron_collapse_output') || 'Collapse output'
            : T('cron_expand_output') || 'Expand output';
          const sessionAction = run.session_id
            ? `<button type="button" class="detail-expand-toggle" data-action="embed-session" data-session-id="${esc(run.session_id)}" data-run-id="${esc(rid)}" title="${esc(T('cron_view_session_steps') || 'View session steps')}" aria-label="${esc(T('cron_view_session_steps') || 'View session steps')}">☰</button>`
            : '';
          return `<div class="detail-run-item" id="${rid}">
        <div class="detail-run-head" data-action="load-run" data-filename="${esc(run.filename)}" data-run-id="${esc(rid)}">
          <span><span style="opacity:.7">${esc(ts)}</span> <span style="opacity:.4;font-size:11px">${esc(sizeStr)}</span>${usageStrip ? ` <span class="cron-run-usage-strip">${esc(usageStrip)}</span>` : ''}</span>
          <span class="detail-run-actions">
            ${sessionAction}
            <button type="button" class="detail-expand-toggle" data-action="toggle-run" data-filename="${esc(run.filename)}" data-run-id="${esc(rid)}" title="${esc(runToggleLabel)}" aria-label="${esc(runToggleLabel)}">${esc(runExpanded ? '▴' : '▾')}</button>
            <span style="opacity:.6">▸</span>
          </span>
        </div>
        <div class="detail-run-body ${runExpanded ? 'expanded' : ''}" style="color:var(--muted);font-size:12px">${esc(T('loading'))}</div>
        <div class="integration-cron-session-detail" data-session-host="${esc(rid)}" hidden></div>
      </div>`;
        })
        .join('');
      const countLabel = data.total > 50 ? ` (${data.total} runs, showing latest 50)` : ` (${data.total} runs)`;
      card.innerHTML = `<div class="detail-card-title">${esc(T('cron_last_output'))}${countLabel}</div>${rows}`;
      card.querySelectorAll('[data-action="load-run"]').forEach(el => {
        el.addEventListener('click', () => {
          loadRunContent(row, el.getAttribute('data-filename'), el.getAttribute('data-run-id'));
        });
      });
      card.querySelectorAll('[data-action="toggle-run"]').forEach(btn => {
        btn.addEventListener('click', ev => {
          ev.stopPropagation();
          toggleRunExpanded(
            ownerProfile,
            jobId,
            btn.getAttribute('data-filename'),
            btn.getAttribute('data-run-id')
          );
        });
      });
      card.querySelectorAll('[data-action="embed-session"]').forEach(btn => {
        btn.addEventListener('click', ev => {
          ev.stopPropagation();
          toggleEmbeddedSession(btn.getAttribute('data-session-id'), btn.getAttribute('data-run-id'));
        });
      });
      for (const run of data.runs) {
        const idx = data.runs.indexOf(run);
        const rid = `integration-cron-run-${encodeURIComponent(ownerProfile)}-${jobId}-${idx}`;
        if (expansionGet(runExpandKey(ownerProfile, jobId, run.filename))) {
          loadRunContent(row, run.filename, rid);
        }
      }
    } catch (_) {
      /* ignore */
    }
  }

  async function loadRunContent(row, filename, runId) {
    const body = document.querySelector(`#${CSS.escape(runId)} .detail-run-body`);
    if (!body) return;
    const item = document.getElementById(runId);
    if (item && !item.classList.contains('open')) item.classList.add('open');
    body.classList.toggle('expanded', expansionGet(runExpandKey(row.ownerProfile, row.job.id, filename)));
    body.innerHTML = `<span style="opacity:.5">${esc(T('loading'))}</span>`;
    try {
      const data = await api(
        `/api/crons/run?job_id=${encodeURIComponent(row.job.id)}&filename=${encodeURIComponent(filename)}&owner_profile=${encodeURIComponent(row.ownerProfile)}`
      );
      if (data.error) {
        body.textContent = data.error;
        return;
      }
      const expanded = expansionGet(runExpandKey(row.ownerProfile, row.job.id, filename));
      const output = expanded ? data.content || data.snippet || '' : data.snippet || data.content || '';
      body.classList.toggle('expanded', expanded);
      if (typeof renderMd === 'function') body.innerHTML = renderMd(output);
      else body.textContent = output;
      const formatUsage = shared().formatRunUsageStrip || (() => '');
      const usageStrip = formatUsage(data.usage);
      if (usageStrip) {
        const usage = document.createElement('div');
        usage.className = 'cron-run-usage-strip cron-run-usage-footer';
        usage.textContent = usageStrip;
        body.appendChild(usage);
      }
      if (data.session_id) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn secondary';
        btn.style.marginTop = '8px';
        btn.textContent = T('cron_view_session_steps') || 'View session steps';
        btn.onclick = ev => {
          ev.stopPropagation();
          toggleEmbeddedSession(data.session_id, runId);
        };
        body.appendChild(btn);
      }
      if (!expanded && data.content && data.snippet && data.content.length > data.snippet.length) {
        const btn = document.createElement('button');
        btn.style.cssText =
          'margin-top:8px;padding:4px 12px;border-radius:var(--radius-btn);border:1px solid var(--border-subtle);background:var(--surface-subtle);color:var(--text-secondary);cursor:pointer;font-size:12px';
        btn.textContent = T('cron_view_full_output') || 'View full output';
        btn.onclick = () => {
          expansionSet(runExpandKey(row.ownerProfile, row.job.id, filename), true);
          body.classList.add('expanded');
          body.innerHTML = typeof renderMd === 'function' ? renderMd(data.content) : data.content;
          btn.remove();
        };
        body.appendChild(btn);
      }
    } catch (e) {
      body.textContent = 'Error: ' + e.message;
    }
  }

  function messageText(message) {
    if (!message) return '';
    const content = message.content;
    if (typeof content === 'string') return content;
    if (Array.isArray(content)) {
      return content
        .map(part => {
          if (typeof part === 'string') return part;
          if (!part || typeof part !== 'object') return '';
          return part.text || part.content || part.input || '';
        })
        .filter(Boolean)
        .join('\n');
    }
    if (content && typeof content === 'object') {
      return content.text || content.content || JSON.stringify(content, null, 2);
    }
    return '';
  }

  function messageRoleLabel(role) {
    const normalized = String(role || '').toLowerCase();
    if (normalized === 'assistant') return T('assistant') || 'Assistant';
    if (normalized === 'user') return T('user') || 'User';
    if (normalized === 'tool') return T('tool') || 'Tool';
    if (normalized === 'system') return T('system') || 'System';
    return role || 'Message';
  }

  function renderSessionMessages(session) {
    const messages = Array.isArray(session?.messages) ? session.messages : [];
    if (!messages.length) {
      return `<div class="integration-cron-session-empty">${esc(T('no_messages') || 'No messages')}</div>`;
    }
    return messages
      .map((message, idx) => {
        const role = String(message.role || 'message').toLowerCase();
        const text = messageText(message);
        const html = typeof renderMd === 'function' ? renderMd(text || '') : esc(text || '');
        const ts = message.timestamp ? new Date(message.timestamp * 1000).toLocaleString() : '';
        return `<div class="integration-cron-session-message" data-role="${esc(role)}">
          <div class="integration-cron-session-message-head">
            <span class="detail-badge ${role === 'assistant' ? 'active' : ''}">${esc(messageRoleLabel(role))}</span>
            <span>${esc(ts || `#${idx + 1}`)}</span>
          </div>
          <div class="msg-body integration-cron-session-message-body">${html}</div>
        </div>`;
      })
      .join('');
  }

  async function toggleEmbeddedSession(sessionId, runId) {
    if (!sessionId || !runId) return;
    const host = document.querySelector(`[data-session-host="${CSS.escape(runId)}"]`);
    if (!host) return;
    if (!host.hidden && host.dataset.loadedSessionId === sessionId) {
      host.hidden = true;
      return;
    }
    host.hidden = false;
    host.dataset.loadedSessionId = sessionId;
    host.innerHTML = `<div class="integration-cron-session-loading">${esc(T('loading'))}</div>`;
    try {
      const data = await api(
        `/api/session?session_id=${encodeURIComponent(sessionId)}&messages=1&resolve_model=0`
      );
      const session = data.session || {};
      host.innerHTML = `<div class="integration-cron-session-panel">
        <div class="integration-cron-session-header">
          <div>
            <div class="detail-card-title">${esc(session.title || T('cron_session_steps') || 'Session steps')}</div>
            <div class="integration-cron-session-sub">${esc(sessionId)}${session.message_count ? ` · ${esc(session.message_count)} messages` : ''}</div>
          </div>
          <button type="button" class="btn secondary" data-action="open-full-session">${esc(T('cron_open_session') || 'Open session')}</button>
        </div>
        <div class="integration-cron-session-messages">${renderSessionMessages(session)}</div>
      </div>`;
      host.querySelector('[data-action="open-full-session"]')?.addEventListener('click', ev => {
        ev.stopPropagation();
        openSession(sessionId);
      });
    } catch (e) {
      host.innerHTML = `<div class="detail-form-error" style="display:block">${esc(e.message || String(e))}</div>`;
    }
  }

  function openDetail(row) {
    _selected = { ...row, key: jobKey(row.ownerProfile, row.job.id) };
    renderDetailView(_selected);
    document.querySelectorAll('#integrationCronList .cron-item').forEach(el => {
      el.classList.toggle('active', el.dataset.key === _selected.key);
    });
  }

  function renderForm({ row, isEdit }) {
    const title = $('integrationCronDetailTitle');
    const body = $('integrationCronDetailBody');
    const empty = $('integrationCronDetailEmpty');
    if (!body || !title) return;
    const job = row?.job || {};
    const profileOpts = shared().profileOptions
      ? shared().profileOptions(job.profile || '')
      : '';
    const deliver = job.deliver || 'local';
    const deliverOpt = (v, l) => `<option value="${v}"${deliver === v ? ' selected' : ''}>${esc(l)}</option>`;
    const toastNotifications = job.toast_notifications !== false;
    title.textContent = isEdit
      ? `${T('edit')} · ${job.name || job.schedule || T('scheduled_jobs')}`
      : T('new_job');
    body.innerHTML = `
      <div class="main-view-content">
        <form class="detail-form" id="integrationCronForm">
          ${!isEdit ? `<div class="detail-form-row">
            <label for="integrationCronFormOwner">${esc(T('integration_cron_owner_profile') || 'Owner profile')}</label>
            <select id="integrationCronFormOwner" required>${ownerProfileOptions(row?.ownerProfile || '')}</select>
            <div class="detail-form-hint">${esc(T('integration_cron_owner_profile_hint') || 'Jobs are stored in this profile cron/jobs.json')}</div>
          </div>` : ''}
          <div class="detail-form-row">
            <label for="integrationCronFormName">${esc(T('cron_name_label') || 'Name')}</label>
            <input type="text" id="integrationCronFormName" value="${esc(job.name || '')}" placeholder="${esc(T('cron_name_placeholder') || 'Optional')}" autocomplete="off">
          </div>
          <div class="detail-form-row">
            <label for="integrationCronFormSchedule">${esc(T('cron_schedule_label') || 'Schedule')}</label>
            <input type="text" id="integrationCronFormSchedule" value="${esc(job.schedule_display || job.schedule || '')}" placeholder="0 9 * * *  —  every 1h  —  @daily" autocomplete="off" required>
            <div class="detail-form-hint">${esc(T('cron_schedule_hint') || "Cron expression or shorthand like 'every 1h'.")}</div>
          </div>
          <div class="detail-form-row">
            <label for="integrationCronFormPrompt">${esc(T('cron_prompt_label') || 'Prompt')}</label>
            <textarea id="integrationCronFormPrompt" rows="6" required>${esc(job.prompt || '')}</textarea>
          </div>
          <div class="detail-form-row">
            <label for="integrationCronFormDeliver">${esc(T('cron_deliver_label') || 'Deliver output to')}</label>
            <select id="integrationCronFormDeliver" ${isEdit ? 'disabled' : ''}>
              ${deliverOpt('local', T('cron_deliver_local') || 'Local (save output only)')}
              ${deliverOpt('discord', 'Discord')}
              ${deliverOpt('telegram', 'Telegram')}
              ${deliverOpt('slack', 'Slack')}
            </select>
          </div>
          <div class="detail-form-row">
            <label for="integrationCronFormProfile">${esc(T('cron_profile_label') || 'Execution profile')}</label>
            <select id="integrationCronFormProfile">${profileOpts}</select>
            <div class="detail-form-hint">${esc(T('cron_profile_server_default_hint') || 'Uses the WebUI server default profile at run time')}</div>
          </div>
          <div class="detail-form-row">
            <label for="integrationCronFormToast">${esc(T('cron_toast_notifications_label') || 'Completion toasts')}</label>
            <label class="detail-form-check" for="integrationCronFormToast">
              <input type="checkbox" id="integrationCronFormToast" ${toastNotifications ? 'checked' : ''}>
              <span>${esc(T('cron_toast_notifications_hint') || 'Show a toast when this cron finishes.')}</span>
            </label>
          </div>
          <div id="integrationCronFormError" class="detail-form-error" style="display:none"></div>
        </form>
      </div>`;
    body.style.display = '';
    if (empty) empty.style.display = 'none';
    _mode = isEdit ? 'edit' : 'create';
    setHeaderButtons(_mode, job);
  }

  async function openCreateForm() {
    await ensureProfiles();
    _selected = null;
    renderForm({ row: null, isEdit: false });
  }

  async function openEditForm() {
    if (!_selected) return;
    await ensureProfiles();
    if (shared().loadProfiles) await shared().loadProfiles();
    renderForm({ row: _selected, isEdit: true });
  }

  function cancelForm() {
    if (_selected) renderDetailView(_selected);
    else clearDetail();
  }

  async function saveForm() {
    const errBox = $('integrationCronFormError');
    const showErr = msg => {
      if (!errBox) return;
      errBox.textContent = msg;
      errBox.style.display = msg ? 'block' : 'none';
    };
    showErr('');
    const name = ($('integrationCronFormName')?.value || '').trim();
    const schedule = ($('integrationCronFormSchedule')?.value || '').trim();
    const prompt = ($('integrationCronFormPrompt')?.value || '').trim();
    const deliver = $('integrationCronFormDeliver')?.value || 'local';
    const profile = ($('integrationCronFormProfile')?.value || '').trim();
    const toastNotifications = $('integrationCronFormToast')?.checked !== false;
    if (!schedule || !prompt) {
      showErr(T('cron_form_required') || 'Schedule and prompt are required.');
      return;
    }
    const payload = { name: name || null, schedule, prompt, deliver, toast_notifications: toastNotifications };
    if (profile) payload.profile = profile;
    try {
      if (_mode === 'create') {
        const owner = ($('integrationCronFormOwner')?.value || '').trim();
        if (!owner) {
          showErr(T('integration_cron_owner_required') || 'Owner profile is required.');
          return;
        }
        payload.owner_profile = owner;
        await api('/api/integration/crons/create', { method: 'POST', body: JSON.stringify(payload) });
        if (typeof showToast === 'function') showToast(T('cron_job_created') || 'Job created');
        await load();
        clearDetail();
        return;
      }
      if (!_selected) return;
      payload.owner_profile = _selected.ownerProfile;
      payload.job_id = _selected.job.id;
      await api('/api/integration/crons/update', { method: 'POST', body: JSON.stringify(payload) });
      if (typeof showToast === 'function') showToast(T('cron_job_updated') || 'Job updated');
      await load();
      const refreshed = _flat.find(
        r => r.ownerProfile === _selected.ownerProfile && r.job.id === _selected.job.id
      );
      if (refreshed) openDetail(refreshed);
    } catch (e) {
      showErr(e.message || String(e));
    }
  }

  async function runCurrent() {
    if (!_selected) return;
    try {
      await api('/api/integration/crons/run', {
        method: 'POST',
        body: JSON.stringify({ job_id: _selected.job.id, owner_profile: _selected.ownerProfile }),
      });
      if (typeof showToast === 'function') showToast(T('cron_run_started') || 'Cron run started', 3000);
    } catch (e) {
      if (typeof showToast === 'function') showToast((T('error_prefix') || 'Error: ') + e.message, 4000);
    }
  }

  async function pauseCurrent() {
    if (!_selected) return;
    try {
      await api('/api/integration/crons/pause', {
        method: 'POST',
        body: JSON.stringify({ job_id: _selected.job.id, owner_profile: _selected.ownerProfile }),
      });
      await load();
    } catch (e) {
      if (typeof showToast === 'function') showToast((T('error_prefix') || 'Error: ') + e.message, 4000);
    }
  }

  async function resumeCurrent() {
    if (!_selected) return;
    try {
      await api('/api/integration/crons/resume', {
        method: 'POST',
        body: JSON.stringify({ job_id: _selected.job.id, owner_profile: _selected.ownerProfile }),
      });
      await load();
    } catch (e) {
      if (typeof showToast === 'function') showToast((T('error_prefix') || 'Error: ') + e.message, 4000);
    }
  }

  async function deleteCurrent() {
    if (!_selected) return;
    const ok =
      typeof showConfirmDialog === 'function'
        ? await showConfirmDialog({
            title: T('cron_delete_confirm_title'),
            message: T('cron_delete_confirm_message'),
            confirmLabel: T('delete_title'),
            danger: true,
            focusCancel: true,
          })
        : confirm(T('cron_delete_confirm_message') || 'Delete this job?');
    if (!ok) return;
    try {
      await api('/api/integration/crons/delete', {
        method: 'POST',
        body: JSON.stringify({ job_id: _selected.job.id, owner_profile: _selected.ownerProfile }),
      });
      if (typeof showToast === 'function') showToast(T('cron_job_deleted') || 'Job deleted');
      clearDetail();
      await load();
    } catch (e) {
      if (typeof showToast === 'function') showToast((T('delete_failed') || 'Delete failed: ') + e.message, 4000);
    }
  }

  function markUnreadFromCompletion(c) {
    const owner = c.owner_profile || '';
    const id = c.job_id || '';
    if (!owner || !id) return;
    _unread.add(jobKey(owner, id));
    renderList();
  }

  function wireControls() {
    $('integrationCronRefreshBtn')?.addEventListener('click', () => load());
    $('integrationCronCreateBtn')?.addEventListener('click', () => openCreateForm());
    $('integrationCronSearch')?.addEventListener('input', () => renderList());
    $('integrationCronProfileFilter')?.addEventListener('change', () => renderList());
    $('integrationCronStatusFilter')?.addEventListener('change', () => renderList());
    $('btnRunIntegrationCronDetail')?.addEventListener('click', () => runCurrent());
    $('btnPauseIntegrationCronDetail')?.addEventListener('click', () => pauseCurrent());
    $('btnResumeIntegrationCronDetail')?.addEventListener('click', () => resumeCurrent());
    $('btnEditIntegrationCronDetail')?.addEventListener('click', () => openEditForm());
    $('btnDeleteIntegrationCronDetail')?.addEventListener('click', () => deleteCurrent());
    $('btnCancelIntegrationCronDetail')?.addEventListener('click', () => cancelForm());
    $('btnSaveIntegrationCronDetail')?.addEventListener('click', () => saveForm());
  }

  async function openSession(sessionId) {
    if (!sessionId || typeof loadSession !== 'function') return;
    if (typeof switchPanel === 'function') await switchPanel('chat');
    await loadSession(sessionId);
    if (typeof renderSessionList === 'function') renderSessionList();
  }

  window.HermesIntegrationCrons = {
    load,
    showNav,
    markUnreadFromCompletion,
    openSession,
    runCurrent,
    pauseCurrent,
    resumeCurrent,
    deleteCurrent,
    openCreateForm,
    saveForm,
    cancelForm,
  };

  showNav();
  wireControls();
})();
