(function () {
  'use strict';

  const cfg = window.__HERMES_CONFIG__ || {};
  if (!cfg.integrationSkills && !cfg.skillhubEnabled) return;

  const hubReady = !!cfg.skillhubEnabled;
  const integrationReady = !!cfg.integrationSkills;
  const STORAGE_SCOPE = 'hermes.skillhub.scope';
  const STORAGE_CATEGORY = 'hermes.skillhub.category';
  const STORAGE_SORT = 'hermes.skillhub.sort';
  const CATEGORY_ALL = '';
  const VALID_SCOPES = new Set(['hub', 'installed', 'not_installed', 'custom']);
  const SORT_OPTIONS = [
    { sort: 'name', order: 'asc', i18n: 'skillhub_sort_name_asc', fallback: 'Name A→Z' },
    { sort: 'name', order: 'desc', i18n: 'skillhub_sort_name_desc', fallback: 'Name Z→A' },
    { sort: 'mtime', order: 'desc', i18n: 'skillhub_sort_mtime_desc', fallback: 'Modified (newest)' },
    { sort: 'mtime', order: 'asc', i18n: 'skillhub_sort_mtime_asc', fallback: 'Modified (oldest)' },
  ];

  let _skillhubScope = 'hub';
  let _skillhubCategory = CATEGORY_ALL;
  let _skillhubSort = 'name';
  let _skillhubOrder = 'asc';
  let _skillhubCategories = [];
  let _skillhubStats = null;
  let _skillhubData = null;
  let _skillhubPage = 1;
  let _skillhubPageSize = 20;
  let _skillhubTotal = 0;
  let _currentSkillhubItem = null;
  let _skillhubMode = 'empty'; // 'empty' | 'read' | 'edit'
  let _skillhubPreFormDetail = null;
  let _searchTimer = null;
  let _pendingUploadFile = null;
  let _pendingDetailJson = null;
  let _batchMode = false;
  let _selectedSkills = new Set();
  let _uploadProfilesCache = null;

  function showSkillHubNav() {
    ['skillhubRailBtn', 'skillhubSidebarBtn'].forEach(id => {
      const el = document.getElementById(id);
      if (!el) return;
      el.hidden = false;
      el.classList.remove('nav-tab-hidden');
    });
    revealSkillHubPanels();
  }

  function revealSkillHubPanels() {
    const panel = document.getElementById('panelSkillhub');
    const main = document.getElementById('mainSkillhub');
    if (panel) panel.hidden = false;
    if (main) main.hidden = false;
  }

  function readStoredScope() {
    try {
      const saved = localStorage.getItem(STORAGE_SCOPE);
      if (saved && VALID_SCOPES.has(saved)) return saved;
    } catch (_) {}
    return 'hub';
  }

  function readStoredCategory(categories) {
    try {
      const saved = localStorage.getItem(STORAGE_CATEGORY);
      if (saved === CATEGORY_ALL) return CATEGORY_ALL;
      if (saved && categories.includes(saved)) return saved;
    } catch (_) {}
    return CATEGORY_ALL;
  }

  function readStoredSort() {
    try {
      const saved = localStorage.getItem(STORAGE_SORT);
      if (!saved) return { sort: 'name', order: 'asc' };
      const [sort, order] = saved.split(':');
      const valid = SORT_OPTIONS.some(opt => opt.sort === sort && opt.order === order);
      if (valid) return { sort, order };
    } catch (_) {}
    return { sort: 'name', order: 'asc' };
  }

  function persistSort() {
    try {
      localStorage.setItem(STORAGE_SORT, `${_skillhubSort}:${_skillhubOrder}`);
    } catch (_) {}
  }

  function sortOptionLabel(opt) {
    return typeof t === 'function' ? t(opt.i18n) : opt.fallback;
  }

  function renderSortSelect() {
    const select = $('skillhubSort');
    if (!select) return;
    const current = `${_skillhubSort}:${_skillhubOrder}`;
    select.innerHTML = '';
    for (const opt of SORT_OPTIONS) {
      const el = document.createElement('option');
      el.value = `${opt.sort}:${opt.order}`;
      el.textContent = sortOptionLabel(opt);
      if (el.value === current) el.selected = true;
      select.appendChild(el);
    }
  }

  function setSortFromValue(raw) {
    const value = String(raw || 'name:asc');
    const [sort, order] = value.split(':');
    const valid = SORT_OPTIONS.some(opt => opt.sort === sort && opt.order === order);
    if (!valid) return;
    if (_skillhubSort === sort && _skillhubOrder === order) return;
    _skillhubSort = sort;
    _skillhubOrder = order;
    persistSort();
    _skillhubPage = 1;
    _skillhubData = null;
    loadSkillHub(true);
  }

  function persistScope() {
    try {
      localStorage.setItem(STORAGE_SCOPE, _skillhubScope);
    } catch (_) {}
  }

  function persistCategory() {
    try {
      localStorage.setItem(STORAGE_CATEGORY, _skillhubCategory);
    } catch (_) {}
  }

  function updateScopeTabs() {
    document.querySelectorAll('#skillhubScopeTabs .skillhub-scope-btn').forEach(btn => {
      const active = btn.dataset.scope === _skillhubScope;
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    updateCustomUploadVisibility();
  }

  function updateCustomUploadVisibility() {
    const show = integrationReady && _skillhubScope === 'custom';
    const zone = $('skillhubUploadDropzone');
    if (zone) zone.hidden = !show;
  }

  function isAllowedSkillUploadFile(file) {
    const name = String(file && file.name || '').toLowerCase();
    return name.endsWith('.md') || name.endsWith('.zip');
  }

  function setUploadDropzoneBusy(busy) {
    const zone = $('skillhubUploadDropzone');
    if (zone) zone.classList.toggle('is-uploading', !!busy);
  }

  function renderScopeStats() {
    const stats = _skillhubStats || {};
    document.querySelectorAll('#skillhubScopeTabs .skillhub-scope-count').forEach(el => {
      const key = el.dataset.stat;
      const value = stats[key] != null ? stats[key] : 0;
      el.textContent = `(${value})`;
    });
  }

  function renderCategoryChips() {
    const box = $('skillhubCategoryChips');
    if (!box) return;
    box.innerHTML = '';
    const allLabel = typeof t === 'function' ? t('skillhub_category_all') : 'All';
    const chips = [{ value: CATEGORY_ALL, label: allLabel }];
    for (const cat of _skillhubCategories) {
      chips.push({ value: cat, label: cat });
    }
    for (const chip of chips) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'skillhub-category-chip' + (chip.value === _skillhubCategory ? ' active' : '');
      btn.textContent = chip.label;
      btn.dataset.category = chip.value;
      btn.addEventListener('click', () => setCategory(chip.value));
      box.appendChild(btn);
    }
  }

  async function ensureCategories() {
    if (_skillhubCategories.length) return;
    const data = await api('/api/skillhub/categories');
    _skillhubCategories = Array.isArray(data) ? data.filter(Boolean) : [];
    _skillhubScope = readStoredScope();
    _skillhubCategory = readStoredCategory(_skillhubCategories);
    const storedSort = readStoredSort();
    _skillhubSort = storedSort.sort;
    _skillhubOrder = storedSort.order;
    renderCategoryChips();
    renderSortSelect();
    updateScopeTabs();
  }

  showSkillHubNav();

  async function loadSkillHub(force) {
    revealSkillHubPanels();
    const box = $('skillhubList');
    if (!hubReady) {
      if (box) {
        box.innerHTML = '<div style="padding:12px;color:var(--muted);font-size:12px">SkillHub 未配置：请设置环境变量 <code>HERMES_INTEGRATION=1</code> 与 <code>SKILLHUB_URL</code> 后重启 WebUI。</div>';
      }
      return;
    }
    try {
      await ensureCategories();
    } catch (e) {
      if (box) {
        box.innerHTML = `<div style="padding:12px;color:var(--accent);font-size:12px">Error: ${esc(e.message)}</div>`;
      }
      return;
    }
    if (!force && _skillhubData) {
      renderSkillHubList(_skillhubData);
      return;
    }
    const q = ($('skillhubSearch') && $('skillhubSearch').value) || '';
    const params = new URLSearchParams({
      scope: _skillhubScope,
      category: _skillhubCategory,
      page: String(_skillhubPage),
      page_size: String(_skillhubPageSize),
      sort: _skillhubSort,
      order: _skillhubOrder,
    });
    if (q.trim()) params.set('q', q.trim());
    try {
      const data = await api(`/api/skillhub/skills?${params}`);
      _skillhubData = data.skills || [];
      _skillhubTotal = data.total != null ? data.total : _skillhubData.length;
      if (data.page) _skillhubPage = data.page;
      if (data.scope && VALID_SCOPES.has(data.scope)) _skillhubScope = data.scope;
      if (data.stats && typeof data.stats === 'object') _skillhubStats = data.stats;
      updateScopeTabs();
      renderScopeStats();
      renderSkillHubList(_skillhubData);
      renderSkillHubPager();
    } catch (e) {
      if (box) {
        box.innerHTML = `<div style="padding:12px;color:var(--accent);font-size:12px">Error: ${esc(e.message)}</div>`;
      }
    }
  }

  function renderSkillHubPager() {
    const pager = $('skillhubPager');
    if (!pager) return;
    const totalPages = Math.max(1, Math.ceil(_skillhubTotal / _skillhubPageSize));
    if (totalPages <= 1) {
      pager.hidden = true;
      pager.innerHTML = '';
      return;
    }
    pager.hidden = false;
    pager.innerHTML = '';
    const prev = document.createElement('button');
    prev.type = 'button';
    prev.className = 'skillhub-pager-btn';
    prev.textContent = '‹';
    prev.disabled = _skillhubPage <= 1;
    prev.onclick = () => {
      if (_skillhubPage > 1) {
        _skillhubPage -= 1;
        _skillhubData = null;
        loadSkillHub(true);
      }
    };
    const label = document.createElement('span');
    label.className = 'skillhub-pager-label';
    label.textContent = `${_skillhubPage} / ${totalPages}`;
    const next = document.createElement('button');
    next.type = 'button';
    next.className = 'skillhub-pager-btn';
    next.textContent = '›';
    next.disabled = _skillhubPage >= totalPages;
    next.onclick = () => {
      if (_skillhubPage < totalPages) {
        _skillhubPage += 1;
        _skillhubData = null;
        loadSkillHub(true);
      }
    };
    pager.append(prev, label, next);
  }

  function renderSkillHubList(skills) {
    const box = $('skillhubList');
    if (!box) return;
    box.innerHTML = '';
    if (!skills.length) {
      box.innerHTML = `<div style="padding:12px;color:var(--muted);font-size:12px">${esc(typeof t === 'function' ? t('skills_no_match') : 'No skills')}</div>`;
      return;
    }
    for (const skill of skills) {
      const el = document.createElement('div');
      const installed = skill.installed === true;
      const isCustom = _skillhubScope === 'custom' || skill.custom === true;
      const showCatalogOnly = _skillhubScope === 'hub' && !installed;
      const isDisabled = skill.disabled || false;
      el.className = 'skill-item' + (isDisabled ? ' disabled' : '') + (showCatalogOnly ? ' catalog-only' : '');
      // Batch selection checkbox
      const checkWrap = document.createElement('div');
      checkWrap.className = 'skill-item-check';
      checkWrap.style.display = _batchMode ? '' : 'none';
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = _selectedSkills.has(skill.name);
      cb.addEventListener('click', (ev) => {
        ev.stopPropagation();
        if (cb.checked) _selectedSkills.add(skill.name);
        else _selectedSkills.delete(skill.name);
        _updateBatchBar();
      });
      checkWrap.appendChild(cb);
      el.appendChild(checkWrap);
      // Toggle button for installed skills (hub-installed or custom)
      if (installed || isCustom) {
        const enableWrap = document.createElement('div');
        enableWrap.className = 'skill-item-enable';
        const toggle = document.createElement('span');
        toggle.className = 'skill-toggle' + (isDisabled ? '' : ' enabled');
        toggle.title = isDisabled
          ? (typeof t === 'function' ? t('skill_disabled') : 'Disabled')
          : (typeof t === 'function' ? t('skill_enabled') : 'Enabled');
        toggle.setAttribute('role', 'switch');
        toggle.setAttribute('aria-checked', isDisabled ? 'false' : 'true');
        toggle.setAttribute('aria-label', toggle.title);
        toggle.addEventListener('click', (ev) => {
          ev.stopPropagation();
          _toggleSkillHubSkill(skill.name, !isDisabled);
        });
        enableWrap.appendChild(toggle);
        el.appendChild(enableWrap);
      }
      const nameEl = document.createElement('span');
      nameEl.className = 'skill-name';
      nameEl.textContent = skill.display_name || skill.install_name || skill.name;
      const descEl = document.createElement('span');
      descEl.className = 'skill-desc';
      descEl.textContent = skill.display_description || skill.description || '';
      const body = document.createElement('div');
      body.className = 'skill-item-body';
      body.append(nameEl, descEl);
      el.appendChild(body);
      if (installed || isCustom) {
        const labelFn = typeof t === 'function' ? t : (k) => k;
        const lockEl = typeof buildSkillLockEl === 'function'
          ? buildSkillLockEl(skill, _toggleSkillLock, labelFn)
          : null;
        if (lockEl) {
          const lockSlot = document.createElement('div');
          lockSlot.className = 'skill-item-lock-slot';
          lockSlot.appendChild(lockEl);
          el.appendChild(lockSlot);
        }
      }
      el.onclick = () => openSkillHubItem(skill, el);
      box.appendChild(el);
    }
  }

  async function _toggleSkillHubSkill(name, currentlyEnabled) {
    const newEnabled = !currentlyEnabled;
    try {
      const result = await api('/api/skills/toggle', {
        method: 'POST',
        body: JSON.stringify({ name, enabled: newEnabled })
      });
      if (result && result.ok) {
        // Update local cache
        if (_skillhubData) {
          const skill = _skillhubData.find(s => s.name === name);
          if (skill) skill.disabled = !newEnabled;
        }
        renderSkillHubList(_skillhubData || []);
        // Notify Skills panel to sync
        window.dispatchEvent(new CustomEvent('hermes:skill-toggle', {
          detail: { name, enabled: newEnabled }
        }));
      } else {
        setStatus((result && result.error) || (typeof t === 'function' ? t('skill_toggle_failed') : 'Toggle failed'));
      }
    } catch(e) {
      setStatus((typeof t === 'function' ? t('skill_toggle_failed') : 'Toggle failed') + e.message);
    }
  }

  async function _toggleSkillLock(skill) {
    if (!skill || !skill.can_lock) return;
    const newLocked = !skill.no_self_improve;
    try {
      const body = { name: skill.name, locked: newLocked };
      if (skill.dir_name) body.dir_name = skill.dir_name;
      const result = await api('/api/skillhub/skills/no_self_improve/toggle', {
        method: 'POST',
        body: JSON.stringify(body)
      });
      if (result && result.ok) {
        if (_skillhubData) {
          const row = _skillhubData.find(s => s.name === skill.name);
          if (row) row.no_self_improve = newLocked;
        }
        renderSkillHubList(_skillhubData || []);
        window.dispatchEvent(new CustomEvent('hermes:skill-lock-toggle', {
          detail: { name: skill.name, locked: newLocked }
        }));
      } else {
        setStatus((result && result.error) || (typeof t === 'function' ? t('skill_lock_failed') : 'Lock toggle failed'));
      }
    } catch (e) {
      setStatus((typeof t === 'function' ? t('skill_lock_failed') : 'Lock toggle failed') + e.message);
    }
  }

  function _skillhubReadActions(skill) {
    const isCustom = _skillhubScope === 'custom' || (skill && skill.custom === true);
    const installed = skill && skill.installed === true;
    const dirName = skill && String(skill.dir_name || '').trim();
    return {
      canDownload: integrationReady && !!dirName,
      canEdit: isCustom && _skillhubScope === 'custom' && integrationReady,
      canInstall: !isCustom && (_skillhubScope === 'hub' || _skillhubScope === 'not_installed') && !installed,
      canDelete:
        (isCustom && _skillhubScope === 'custom') ||
        (!isCustom && (_skillhubScope === 'hub' || _skillhubScope === 'installed') && installed),
      canManageProfiles: installed && integrationReady,
    };
  }

  function _setSkillhubHeaderButtons(mode, options) {
    const opts = options || {};
    const downloadBtn = $('btnSkillhubDownload');
    const editBtn = $('btnSkillhubEdit');
    const installBtn = $('btnSkillhubInstall');
    const uninstallBtn = $('btnSkillhubUninstall');
    const manageBtn = $('btnSkillhubManageProfiles');
    const cancelBtn = $('btnSkillhubCancelEdit');
    const saveBtn = $('btnSkillhubSaveEdit');
    const show = b => b && (b.style.display = '');
    const hide = b => b && (b.style.display = 'none');
    if (mode === 'read') {
      if (opts.canDownload) show(downloadBtn);
      else hide(downloadBtn);
      if (opts.canEdit) show(editBtn);
      else hide(editBtn);
      if (opts.canInstall) show(installBtn);
      else hide(installBtn);
      if (opts.canDelete) show(uninstallBtn);
      else hide(uninstallBtn);
      if (opts.canManageProfiles) show(manageBtn);
      else hide(manageBtn);
      hide(cancelBtn);
      hide(saveBtn);
    } else if (mode === 'edit') {
      hide(downloadBtn);
      hide(editBtn);
      hide(installBtn);
      hide(uninstallBtn);
      hide(manageBtn);
      show(cancelBtn);
      show(saveBtn);
    } else {
      hide(downloadBtn);
      hide(editBtn);
      hide(installBtn);
      hide(uninstallBtn);
      hide(manageBtn);
      hide(cancelBtn);
      hide(saveBtn);
    }
  }

  function _renderSkillhubDetailJson(name, detail, doc, structure, isCustom, detailMeta) {
    const title = $('skillhubDetailTitle');
    const body = $('skillhubDetailBody');
    const empty = $('skillhubDetailEmpty');
    const meta = detailMeta || {};
    const displayName = meta.display_name || (_currentSkillhubItem && _currentSkillhubItem.display_name) || '';
    if (title) title.textContent = displayName || name;
    const tl = (k, fb) => (typeof t === 'function' ? t(k) || fb : fb);
    const displayDesc = meta.display_description || '';

    let html = '';
    const category = String(meta.category || (_currentSkillhubItem && _currentSkillhubItem.category) || '').trim();
    if (category) {
      html += `<div style="margin-bottom:12px"><span class="detail-json-chip" style="font-size:12px">${esc(category)}</span></div>`;
    }
    if (isCustom) {
      const hint = tl('skillhub_custom_hint', 'Local custom skill.');
      html += `<p class="skillhub-custom-hint" style="color:var(--muted);font-size:12px;margin:0 0 12px">${esc(hint)}</p>`;
    }
    if (displayDesc) {
      html += `<p style="color:var(--muted);font-size:13px;margin:0 0 12px">${esc(displayDesc)}</p>`;
    }

    const section = (label, content) => `<div class="detail-json-section"><div class="detail-json-label">${esc(label)}</div>${content}</div>`;

    if (detail.taskGoal) {
      html += section(tl('skill_detail_task_goal', 'Task Goal'), `<p>${esc(detail.taskGoal)}</p>`);
    }

    if (Array.isArray(detail.taskDetails) && detail.taskDetails.length) {
      const items = detail.taskDetails.map(d => `<li>${esc(d)}</li>`).join('');
      html += section(tl('skill_detail_task_details', 'Task Details'), `<ol class="detail-json-list">${items}</ol>`);
    }

    if (detail.useMode) {
      html += section(tl('skill_detail_use_mode', 'How to Use'), `<p>${esc(detail.useMode)}</p>`);
    }

    if (Array.isArray(detail.triggerKeywords) && detail.triggerKeywords.length) {
      const chips = detail.triggerKeywords.map(k => `<span class="detail-json-chip">${esc(k)}</span>`).join('');
      html += section(tl('skill_detail_trigger_keywords', 'Trigger Keywords'), `<div class="detail-json-chips">${chips}</div>`);
    }

    if (Array.isArray(detail.requiredInfo) && detail.requiredInfo.length) {
      const items = detail.requiredInfo.map(item => {
        const badge = item.required
          ? '<span class="detail-json-badge required">Required</span>'
          : '<span class="detail-json-badge optional">Optional</span>';
        return `<li>${badge} ${esc(item.label || '')}</li>`;
      }).join('');
      html += section(tl('skill_detail_required_info', 'Required Info'), `<ul class="detail-json-list">${items}</ul>`);
    }

    if (detail.dialogExample && (detail.dialogExample.user || detail.dialogExample.assistant)) {
      const ex = detail.dialogExample;
      let chat = '<div class="detail-json-dialog">';
      if (ex.user) chat += `<div class="detail-json-msg user"><span class="detail-json-role">${tl('skill_detail_user', 'User')}</span>${esc(ex.user)}</div>`;
      if (ex.assistant) chat += `<div class="detail-json-msg assistant"><span class="detail-json-role">${tl('skill_detail_assistant', 'Assistant')}</span>${esc(ex.assistant)}</div>`;
      chat += '</div>';
      html += section(tl('skill_detail_dialog_example', 'Dialog Example'), chat);
    }

    // Show rendered SKILL.md below the structured detail
    if (typeof renderMd === 'function') {
      html += '<div class="detail-json-section"><div class="detail-json-label">SKILL.md</div>';
      html += renderMd(doc.content || '(no content)');
      html += '</div>';
    }

    if (structure && (structure.scripts?.length || structure.references?.length)) {
      html += '<div class="skillhub-structure"><div class="skillhub-structure-title">Files</div>';
      const addLinks = (items, label) => {
        if (!items || !items.length) return;
        html += `<div class="skillhub-structure-section"><strong>${esc(label)}</strong>`;
        for (const f of items) {
          const p = f.path || f.name;
          html += `<a href="#" class="skillhub-file-link" data-name="${esc(name)}" data-path="${esc(p)}">${esc(p)}</a>`;
        }
        html += '</div>';
      };
      addLinks(structure.scripts, 'Scripts');
      addLinks(structure.references, 'References');
      html += '</div>';
    }

    if (body) {
      body.innerHTML = `<div class="main-view-content skill-detail-content">${html}</div>`;
      body.style.display = '';
      body.querySelectorAll('.skillhub-file-link').forEach(a => {
        a.addEventListener('click', ev => {
          ev.preventDefault();
          openSkillHubFile(a.dataset.name, a.dataset.path);
        });
      });
    }
    if (empty) empty.style.display = 'none';
    _skillhubMode = 'read';
  }

  function _renderSkillhubDetailRead(name, doc, structure, isCustom, detailMeta) {
    const title = $('skillhubDetailTitle');
    const body = $('skillhubDetailBody');
    const empty = $('skillhubDetailEmpty');
    const meta = detailMeta || {};
    const displayName = meta.display_name || (_currentSkillhubItem && _currentSkillhubItem.display_name) || '';
    if (title) title.textContent = displayName || name;
    const displayDesc = meta.display_description || '';
    let html = '';
    const category = String(meta.category || (_currentSkillhubItem && _currentSkillhubItem.category) || '').trim();
    if (category) {
      html += `<div style="margin-bottom:12px"><span class="detail-json-chip" style="font-size:12px">${esc(category)}</span></div>`;
    }
    if (isCustom) {
      const hint = typeof t === 'function' ? t('skillhub_custom_hint') : 'Local custom skill.';
      html += `<p class="skillhub-custom-hint" style="color:var(--muted);font-size:12px;margin:0 0 12px">${esc(hint)}</p>`;
    }
    if (displayDesc) {
      html += `<p style="color:var(--muted);font-size:13px;margin:0 0 12px">${esc(displayDesc)}</p>`;
    }
    if (typeof renderMd === 'function') {
      html += renderMd(doc.content || '(no content)');
    } else {
      html += `<pre>${esc(doc.content || '')}</pre>`;
    }
    if (structure && (structure.scripts?.length || structure.references?.length)) {
      html += '<div class="skillhub-structure"><div class="skillhub-structure-title">Files</div>';
      const addLinks = (items, label) => {
        if (!items || !items.length) return;
        html += `<div class="skillhub-structure-section"><strong>${esc(label)}</strong>`;
        for (const f of items) {
          const p = f.path || f.name;
          html += `<a href="#" class="skillhub-file-link" data-name="${esc(name)}" data-path="${esc(p)}">${esc(p)}</a>`;
        }
        html += '</div>';
      };
      addLinks(structure.scripts, 'Scripts');
      addLinks(structure.references, 'References');
      html += '</div>';
    }
    if (body) {
      body.innerHTML = `<div class="main-view-content skill-detail-content">${html}</div>`;
      body.style.display = '';
      body.querySelectorAll('.skillhub-file-link').forEach(a => {
        a.addEventListener('click', ev => {
          ev.preventDefault();
          openSkillHubFile(a.dataset.name, a.dataset.path);
        });
      });
    }
    if (empty) empty.style.display = 'none';
    _skillhubMode = 'read';
  }

  function _renderSkillhubEditForm(name, content) {
    const title = $('skillhubDetailTitle');
    const body = $('skillhubDetailBody');
    const empty = $('skillhubDetailEmpty');
    if (!body || !title) return;
    const editLabel = typeof t === 'function' ? t('skills_edit') : 'Edit';
    title.textContent = `${editLabel} · ${name}`;
    const nameLabel = typeof t === 'function' ? t('skill_name') : 'Name';
    const contentLabel = typeof t === 'function' ? t('skill_content') : 'SKILL.md content';
    const contentPlaceholder =
      typeof t === 'function' ? t('skill_content_placeholder') : 'YAML frontmatter + markdown body';
    const renameHint =
      typeof t === 'function'
        ? t('skill_rename_not_supported')
        : 'Renaming a skill is not supported. Create a new skill and delete the old one to rename.';
    body.innerHTML = `
      <div class="main-view-content">
        <form class="detail-form" onsubmit="event.preventDefault();window.HermesSkillHub&&HermesSkillHub.saveEditForm();">
          <div class="detail-form-row">
            <label for="skillhubFormName">${esc(nameLabel)}</label>
            <input type="text" id="skillhubFormName" value="${esc(name)}" disabled>
            <div class="detail-form-hint">${esc(renameHint)}</div>
          </div>
          <div class="detail-form-row">
            <label for="skillhubFormContent">${esc(contentLabel)}</label>
            <textarea id="skillhubFormContent" rows="18" placeholder="${esc(contentPlaceholder)}">${esc(content || '')}</textarea>
          </div>
          <div id="skillhubFormError" class="detail-form-error" style="display:none"></div>
        </form>
      </div>`;
    body.style.display = '';
    if (empty) empty.style.display = 'none';
    _skillhubMode = 'edit';
    _setSkillhubHeaderButtons('edit');
    const focusEl = $('skillhubFormContent');
    if (focusEl) focusEl.focus();
  }

  function _skillhubPreviewScopeParam(skill) {
    const isCustom = _skillhubScope === 'custom' || (skill && skill.custom === true);
    if (isCustom) return '&scope=custom';
    if (skill && skill.installed) return '';
    return '&scope=hub';
  }

  async function openSkillHubItem(skill, el) {
    document.querySelectorAll('#skillhubList .skill-item').forEach(e => e.classList.remove('active'));
    if (el) el.classList.add('active');
    _currentSkillhubItem = skill;
    _skillhubPreFormDetail = null;
    const name = skill.name;
    const title = $('skillhubDetailTitle');
    const body = $('skillhubDetailBody');
    const empty = $('skillhubDetailEmpty');
    if (title) title.textContent = skill.display_name || skill.name;
    const isCustom = _skillhubScope === 'custom' || skill.custom === true;
    const actions = _skillhubReadActions(skill);
    const scopeParam = _skillhubPreviewScopeParam(skill);
    try {
      // Custom skills: always use local detail. Others: try upstream first, fall back to local.
      const detailPromise = isCustom
        ? api(`/api/skillhub/file?name=${encodeURIComponent(name)}&path=.detail.json${scopeParam}`)
            .then(resp => ({ source: 'local', data: resp }))
            .catch(() => null)
        : api(`/api/skillhub/detail?name=${encodeURIComponent(name)}`)
            .then(resp => ({ source: 'upstream', data: resp }))
            .catch(() => (skill && skill.installed)
              ? api(`/api/skillhub/file?name=${encodeURIComponent(name)}&path=.detail.json${scopeParam}`)
                  .then(resp => ({ source: 'local', data: resp }))
                  .catch(() => null)
              : null
            );
      const [doc, structure, detailResult] = await Promise.all([
        api(`/api/skillhub/content?name=${encodeURIComponent(name)}${scopeParam}`),
        api(`/api/skillhub/structure?name=${encodeURIComponent(name)}${scopeParam}`).catch(() => null),
        detailPromise,
      ]);
      let detailJson = null;
      let detailMeta = null;
      if (detailResult && detailResult.data && !detailResult.data.error) {
        if (detailResult.source === 'local' && detailResult.data.content) {
          // Local file response: parse content string
          try {
            const parsed = JSON.parse(detailResult.data.content);
            detailMeta = parsed;
            detailJson = parsed && parsed.detail_json ? parsed.detail_json : parsed;
          } catch (_) {}
        } else if (detailResult.source === 'upstream') {
          // Upstream detail response: already parsed
          detailMeta = detailResult.data;
          detailJson = detailResult.data.detail_json || detailResult.data;
        }
      }
      _skillhubPreFormDetail = {
        name,
        content: doc.content || '',
        structure: structure || null,
        isCustom,
        detailJson,
        detailMeta,
      };
      if (detailJson) {
        _renderSkillhubDetailJson(name, detailJson, doc, structure, isCustom, detailMeta);
      } else {
        _renderSkillhubDetailRead(name, doc, structure, isCustom, detailMeta);
      }
      _setSkillhubHeaderButtons('read', actions);
      if ($('btnSkillhubUninstall') && actions.canDelete) {
        const tip = typeof t === 'function' ? t('delete_title') : 'Delete';
        $('btnSkillhubUninstall').setAttribute('data-tooltip', tip);
        $('btnSkillhubUninstall').setAttribute('data-i18n-title', 'delete_title');
      }
    } catch (e) {
      _skillhubMode = 'empty';
      _setSkillhubHeaderButtons('empty');
      if (body) {
        body.innerHTML = `<div class="main-view-content"><div class="detail-form-error" style="display:block">${esc(e.message)}</div></div>`;
        body.style.display = '';
      }
      if (empty) empty.style.display = 'none';
    }
  }

  function editCurrent() {
    if (!_currentSkillhubItem || !_skillhubPreFormDetail) return;
    const snap = _skillhubPreFormDetail;
    _renderSkillhubEditForm(snap.name, snap.content || '');
  }

  function cancelEditForm() {
    if (_skillhubPreFormDetail) {
      const snap = _skillhubPreFormDetail;
      if (snap.detailJson) {
        _renderSkillhubDetailJson(snap.name, snap.detailJson, { content: snap.content }, snap.structure, snap.isCustom, snap.detailMeta);
      } else {
        _renderSkillhubDetailRead(snap.name, { content: snap.content }, snap.structure, snap.isCustom, snap.detailMeta);
      }
      _setSkillhubHeaderButtons('read', _skillhubReadActions(_currentSkillhubItem));
      return;
    }
    clearDetail();
  }

  async function downloadCurrent() {
    if (!_currentSkillhubItem) return;
    const name = _currentSkillhubItem.name;
    const dirName = String(_currentSkillhubItem.dir_name || '').trim();
    const params = new URLSearchParams({ name });
    if (dirName) params.set('dir_name', dirName);
    const url = new URL(`/api/skillhub/download?${params}`, document.baseURI || location.href).href;
    try {
      const res = await fetch(url, { method: 'GET', credentials: 'include' });
      if (res.status === 401) {
        window.location.href = 'login?next=' + encodeURIComponent(window.location.pathname + window.location.search);
        return;
      }
      if (!res.ok) {
        const text = await res.text();
        let data = {};
        try {
          data = JSON.parse(text);
        } catch (_) {}
        throw new Error(data.error || data.message || text || res.statusText);
      }
      const blob = await res.blob();
      let filename = `${name}.zip`;
      const disp = res.headers.get('Content-Disposition') || '';
      const star = /filename\*=UTF-8''([^;\s]+)/i.exec(disp);
      const plain = /filename="([^"]+)"/i.exec(disp);
      if (star && star[1]) filename = decodeURIComponent(star[1]);
      else if (plain && plain[1]) filename = plain[1];
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = filename;
      link.click();
      URL.revokeObjectURL(link.href);
    } catch (e) {
      const base = typeof t === 'function' ? t('skillhub_download_failed') : 'Download failed';
      if (typeof showToast === 'function') showToast(base + (e.message ? ': ' + e.message : ''));
    }
  }

  async function saveEditForm() {
    if (!_currentSkillhubItem) return;
    const contentInput = $('skillhubFormContent');
    const errEl = $('skillhubFormError');
    if (!contentInput || !errEl) return;
    const content = contentInput.value;
    errEl.style.display = 'none';
    if (!content.trim()) {
      errEl.textContent =
        typeof t === 'function' ? t('content_required') || 'Content is required' : 'Content is required';
      errEl.style.display = '';
      return;
    }
    const name = _currentSkillhubItem.name;
    try {
      await api('/api/skillhub/edit', {
        method: 'POST',
        body: JSON.stringify({
          name,
          dir_name: _currentSkillhubItem.dir_name || '',
          content,
        }),
      });
      _skillhubData = null;
      if (typeof _invalidateSkillsDataCache === 'function') _invalidateSkillsDataCache();
      await loadSkillHub(true);
      if (typeof loadSkills === 'function') await loadSkills();
      const item =
        (_skillhubData || []).find(s => s.name === name) ||
        (_skillhubData || []).find(s => s.dir_name === (_currentSkillhubItem.dir_name || ''));
      if (item) {
        await openSkillHubItem(item, null);
      }
      if (typeof showToast === 'function') {
        showToast(typeof t === 'function' ? t('skill_updated') || 'Skill updated' : 'Skill updated');
      }
    } catch (e) {
      errEl.textContent =
        (typeof t === 'function' ? t('error_prefix') || 'Error: ' : 'Error: ') + (e.message || String(e));
      errEl.style.display = '';
    }
  }

  async function openSkillHubFile(name, path) {
    const body = $('skillhubDetailBody');
    const scopeParam = _skillhubPreviewScopeParam(_currentSkillhubItem);
    try {
      const data = await api(
        `/api/skillhub/file?name=${encodeURIComponent(name)}&path=${encodeURIComponent(path)}${scopeParam}`
      );
      const back = typeof t === 'function' ? t('skills_back_to').replace('{0}', name) : name;
      let html = `<p><a href="#" class="skillhub-back-doc" data-name="${esc(name)}">${esc(back)}</a></p>`;
      html += typeof renderMd === 'function' ? renderMd(data.content || '') : `<pre>${esc(data.content || '')}</pre>`;
      if (body) {
        body.innerHTML = `<div class="main-view-content">${html}</div>`;
        body.querySelector('.skillhub-back-doc')?.addEventListener('click', ev => {
          ev.preventDefault();
          if (_currentSkillhubItem) openSkillHubItem(_currentSkillhubItem, null);
        });
      }
    } catch (e) {
      if (body) {
        body.innerHTML = `<div class="detail-form-error" style="display:block">${esc(e.message)}</div>`;
      }
    }
  }

  async function installCurrent() {
    if (!_currentSkillhubItem) return;
    const name = _currentSkillhubItem.name;
    const displayName = _currentSkillhubItem.display_name || _currentSkillhubItem.install_name || name;
    const category = _currentSkillhubItem.category || '';

    // Show profile selection dialog
    let profiles;
    try {
      profiles = await _showInstallProfileDialog(name, displayName);
    } catch (_) {
      return; // user cancelled
    }
    if (!profiles || profiles.length === 0) return;

    try {
      const resp = await api('/api/skillhub/install-to-profiles', {
        method: 'POST',
        body: JSON.stringify({ name, display_name: displayName, category, profiles }),
      });
      const results = resp.results || [];
      const successCount = results.filter(r => r.ok).length;
      const failCount = results.filter(r => !r.ok).length;

      _skillhubData = null;
      if (typeof _invalidateSkillsDataCache === 'function') _invalidateSkillsDataCache();
      if (typeof _invalidateSkillCommandCache === 'function') _invalidateSkillCommandCache();
      await loadSkillHub(true);
      if (typeof loadSkills === 'function') await loadSkills();
      if (_currentSkillhubItem) {
        _currentSkillhubItem.installed = true;
        openSkillHubItem(_currentSkillhubItem, null);
      }
      if (typeof showToast === 'function') {
        if (failCount === 0) {
          const msg = typeof t === 'function' ? t('install_toast_success') : 'Installed to {0} assistants';
          showToast(msg.replace('{0}', successCount));
        } else {
          const msg = typeof t === 'function' ? t('install_toast_partial') : 'Some installations failed';
          showToast(msg, 5000, 'error');
        }
      }
    } catch (e) {
      if (typeof showToast === 'function') showToast(e.message, 5000, 'error');
    }
  }

  function _showInstallProfileDialog(skillName, displayName, batchCount) {
    return new Promise(async (resolve, reject) => {
      // Fetch profiles list
      let profiles = [];
      try {
        const resp = await api('/api/profiles');
        profiles = resp.profiles || [];
      } catch (_) {
        profiles = [];
      }

      // Filter out profiles that already have this skill installed (skip for batch)
      const installedProfiles = new Set();
      if (!batchCount || batchCount <= 1) {
        for (const p of profiles) {
          const skills = p.skills || [];
          if (skills.some(s => s.name === skillName || s.dir_name === skillName)) {
            installedProfiles.add(p.name);
          }
        }
      }

      const overlay = document.createElement('div');
      overlay.className = 'app-dialog-overlay';
      overlay.style.display = 'flex';

      const titleLabel = batchCount > 1
        ? (typeof t === 'function' ? t('batch_install_select_profiles') || `Install ${batchCount} skills to...` : `Install ${batchCount} skills to...`)
        : (typeof t === 'function' ? t('install_select_profiles') : 'Select Assistants');
      const selectAllLabel = typeof t === 'function' ? t('install_select_all') : 'Select All';
      const deselectAllLabel = typeof t === 'function' ? t('install_deselect_all') : 'Deselect All';
      const installLabel = typeof t === 'function' ? t('install_btn') : 'Install';
      const cancelLabel = typeof t === 'function' ? t('cancel') : 'Cancel';
      const alreadyInstalledLabel = typeof t === 'function' ? t('install_already_installed') : 'Already installed';
      const noSelectionLabel = typeof t === 'function' ? t('install_no_profile_selected') : 'Please select at least one assistant';

      const profileCards = profiles.map(p => {
        const isInstalled = installedProfiles.has(p.name);
        const info = p.info || {};
        const logo = info.logo || '';
        const display_name = info.display_name || p.name;
        const desc = info.description || '';
        const logoHtml = logo
          ? `<img src="${logo}" style="width:32px;height:32px;border-radius:6px;object-fit:cover">`
          : `<div style="width:32px;height:32px;border-radius:6px;background:var(--accent);display:flex;align-items:center;justify-content:center;color:#fff;font-size:14px;font-weight:600">${esc(display_name.charAt(0).toUpperCase())}</div>`;
        return `
          <label class="install-profile-card" data-profile="${esc(p.name)}" style="display:flex;align-items:center;gap:10px;padding:10px 12px;border:1px solid var(--border2);border-radius:8px;cursor:${isInstalled ? 'default' : 'pointer'};opacity:${isInstalled ? '0.5' : '1'};background:var(--bg);transition:border-color .15s">
            <input type="checkbox" value="${esc(p.name)}" ${isInstalled ? 'disabled checked' : ''} style="accent-color:var(--accent);width:16px;height:16px">
            ${logoHtml}
            <div style="flex:1;min-width:0">
              <div style="font-size:13px;font-weight:500;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(display_name)}</div>
              ${desc ? `<div style="font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(desc)}</div>` : ''}
            </div>
            ${isInstalled ? `<span style="font-size:11px;color:var(--muted);white-space:nowrap">${esc(alreadyInstalledLabel)}</span>` : ''}
          </label>
        `;
      }).join('');

      overlay.innerHTML = `
        <div class="app-dialog" role="dialog" style="max-width:420px;width:90vw">
          <div class="app-dialog-header">
            <div class="app-dialog-title">${esc(titleLabel)}</div>
            <button class="app-dialog-close" id="installProfileClose">&times;</button>
          </div>
          <div style="font-size:12px;color:var(--muted);margin-bottom:4px">${esc(displayName || skillName)}</div>
          <div style="display:flex;gap:8px;margin:8px 0">
            <button id="installProfileSelectAll" class="app-dialog-btn" style="font-size:12px;padding:4px 10px">${esc(selectAllLabel)}</button>
            <button id="installProfileDeselectAll" class="app-dialog-btn" style="font-size:12px;padding:4px 10px">${esc(deselectAllLabel)}</button>
          </div>
          <div id="installProfileList" style="max-height:320px;overflow-y:auto;display:flex;flex-direction:column;gap:6px;margin:8px 0">
            ${profileCards}
          </div>
          <div id="installProfileError" style="display:none;font-size:12px;color:var(--error,#e74c3c);margin-bottom:8px"></div>
          <div class="app-dialog-actions">
            <button class="app-dialog-btn" id="installProfileCancel">${esc(cancelLabel)}</button>
            <button class="app-dialog-btn confirm" id="installProfileConfirm">${esc(installLabel)}</button>
          </div>
        </div>
      `;

      document.body.appendChild(overlay);

      const close = (result) => {
        overlay.remove();
        if (result) resolve(result);
        else reject(new Error('cancelled'));
      };

      // Event handlers
      overlay.querySelector('#installProfileClose').onclick = () => close(null);
      overlay.querySelector('#installProfileCancel').onclick = () => close(null);
      overlay.addEventListener('click', (e) => {
        if (e.target === overlay) close(null);
      });
      document.addEventListener('keydown', function escHandler(ev) {
        if (ev.key === 'Escape') {
          document.removeEventListener('keydown', escHandler);
          close(null);
        }
      });

      // Select all / deselect all
      overlay.querySelector('#installProfileSelectAll').onclick = () => {
        overlay.querySelectorAll('#installProfileList input[type="checkbox"]:not(:disabled)').forEach(cb => {
          cb.checked = true;
        });
      };
      overlay.querySelector('#installProfileDeselectAll').onclick = () => {
        overlay.querySelectorAll('#installProfileList input[type="checkbox"]:not(:disabled)').forEach(cb => {
          cb.checked = false;
        });
      };

      // Hover effect for cards
      overlay.querySelectorAll('.install-profile-card').forEach(card => {
        if (card.style.opacity === '0.5') return;
        card.addEventListener('mouseenter', () => { card.style.borderColor = 'var(--accent)'; });
        card.addEventListener('mouseleave', () => { card.style.borderColor = 'var(--border2)'; });
      });

      // Confirm
      overlay.querySelector('#installProfileConfirm').onclick = () => {
        const selected = [];
        overlay.querySelectorAll('#installProfileList input[type="checkbox"]:checked:not(:disabled)').forEach(cb => {
          selected.push(cb.value);
        });
        if (selected.length === 0) {
          const errEl = overlay.querySelector('#installProfileError');
          errEl.textContent = noSelectionLabel;
          errEl.style.display = 'block';
          return;
        }
        close(selected);
      };
    });
  }

  async function deleteCurrent() {
    if (!_currentSkillhubItem) return;
    const name = _currentSkillhubItem.name;
    const label = _currentSkillhubItem.display_name || name;
    const message = typeof t === 'function' && t('skill_delete_confirm')
      ? t('skill_delete_confirm').replace('{0}', label)
      : `Delete skill "${label}" from all assistants?`;
    if (typeof showConfirmDialog === 'function') {
      const ok = await showConfirmDialog({
        title: typeof t === 'function' ? t('delete_title') : 'Delete',
        message,
        confirmLabel: typeof t === 'function' ? t('delete_title') : 'Delete',
        danger: true,
        focusCancel: true,
      });
      if (!ok) return;
    }
    try {
      await api('/api/skillhub/delete-from-all-profiles', {
        method: 'POST',
        body: JSON.stringify({
          name,
          dir_name: _currentSkillhubItem.dir_name || '',
        }),
      });
      _skillhubData = null;
      _currentSkillhubItem = null;
      if (typeof _invalidateSkillsDataCache === 'function') _invalidateSkillsDataCache();
      if (typeof _invalidateSkillCommandCache === 'function') _invalidateSkillCommandCache();
      window.dispatchEvent(new CustomEvent('hermes:skill-delete', { detail: { name } }));
      clearDetail();
      await loadSkillHub(true);
      if (typeof loadSkills === 'function') await loadSkills();
      if (typeof showToast === 'function') {
        showToast(typeof t === 'function' ? t('skill_deleted') || 'Removed' : 'Removed');
      }
    } catch (e) {
      if (typeof showToast === 'function') showToast(e.message);
    }
  }

  async function manageProfilesCurrent() {
    if (!_currentSkillhubItem) return;
    const name = _currentSkillhubItem.name;
    const displayName = _currentSkillhubItem.display_name || _currentSkillhubItem.install_name || name;
    const category = _currentSkillhubItem.category || '';
    const isCustom = _skillhubScope === 'custom' || _currentSkillhubItem.custom === true;

    let result;
    try {
      result = await _showManageProfilesDialog(name, displayName);
    } catch (_) {
      return; // user cancelled
    }
    if (!result) return;

    const { install: toInstall, uninstall: toUninstall } = result;
    if (toInstall.length === 0 && toUninstall.length === 0) return;

    try {
      const resp = await api('/api/skillhub/sync-profiles', {
        method: 'POST',
        body: JSON.stringify({ name, display_name: displayName, category, is_custom: isCustom, install: toInstall, uninstall: toUninstall }),
      });
      const installed = resp.installed || [];
      const uninstalled = resp.uninstalled || [];
      const failCount = installed.filter(r => !r.ok).length + uninstalled.filter(r => !r.ok).length;

      _skillhubData = null;
      if (typeof _invalidateSkillsDataCache === 'function') _invalidateSkillsDataCache();
      if (typeof _invalidateSkillCommandCache === 'function') _invalidateSkillCommandCache();
      await loadSkillHub(true);
      if (typeof loadSkills === 'function') await loadSkills();
      if (_currentSkillhubItem) {
        openSkillHubItem(_currentSkillhubItem, null);
      }
      if (typeof showToast === 'function') {
        if (failCount === 0) {
          const msg = typeof t === 'function' ? t('manage_profiles_success') : 'Associations updated';
          showToast(msg);
        } else {
          const msg = typeof t === 'function' ? t('manage_profiles_partial') : 'Some changes failed';
          showToast(msg, 5000, 'error');
        }
      }
    } catch (e) {
      if (typeof showToast === 'function') showToast(e.message, 5000, 'error');
    }
  }

  function _showManageProfilesDialog(skillName, displayName) {
    return new Promise(async (resolve, reject) => {
      // Fetch profiles list
      let profiles = [];
      try {
        const resp = await api('/api/profiles');
        profiles = resp.profiles || [];
      } catch (_) {
        profiles = [];
      }

      // Determine which profiles currently have this skill
      const installedProfiles = new Set();
      for (const p of profiles) {
        const skills = p.skills || [];
        if (skills.some(s => s.name === skillName || s.dir_name === skillName)) {
          installedProfiles.add(p.name);
        }
      }

      const overlay = document.createElement('div');
      overlay.className = 'app-dialog-overlay';
      overlay.style.display = 'flex';

      const titleLabel = typeof t === 'function' ? t('manage_profiles_title') : 'Manage Skill Assistants';
      const selectAllLabel = typeof t === 'function' ? t('install_select_all') : 'Select All';
      const deselectAllLabel = typeof t === 'function' ? t('install_deselect_all') : 'Deselect All';
      const confirmLabel = typeof t === 'function' ? t('save') : 'Save';
      const cancelLabel = typeof t === 'function' ? t('cancel') : 'Cancel';

      const profileCards = profiles.map(p => {
        const isChecked = installedProfiles.has(p.name);
        const info = p.info || {};
        const logo = info.logo || '';
        const display_name = info.display_name || p.name;
        const desc = info.description || '';
        const logoHtml = logo
          ? `<img src="${logo}" style="width:32px;height:32px;border-radius:6px;object-fit:cover">`
          : `<div style="width:32px;height:32px;border-radius:6px;background:var(--accent);display:flex;align-items:center;justify-content:center;color:#fff;font-size:14px;font-weight:600">${esc(display_name.charAt(0).toUpperCase())}</div>`;
        return `
          <label class="install-profile-card" data-profile="${esc(p.name)}" style="display:flex;align-items:center;gap:10px;padding:10px 12px;border:1px solid var(--border2);border-radius:8px;cursor:pointer;background:var(--bg);transition:border-color .15s">
            <input type="checkbox" value="${esc(p.name)}" ${isChecked ? 'checked' : ''} style="accent-color:var(--accent);width:16px;height:16px">
            ${logoHtml}
            <div style="flex:1;min-width:0">
              <div style="font-size:13px;font-weight:500;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(display_name)}</div>
              ${desc ? `<div style="font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(desc)}</div>` : ''}
            </div>
          </label>
        `;
      }).join('');

      overlay.innerHTML = `
        <div class="app-dialog" role="dialog" style="max-width:420px;width:90vw">
          <div class="app-dialog-header">
            <div class="app-dialog-title">${esc(titleLabel)}</div>
            <button class="app-dialog-close" id="manageProfileClose">&times;</button>
          </div>
          <div style="font-size:12px;color:var(--muted);margin-bottom:4px">${esc(displayName || skillName)}</div>
          <div style="display:flex;gap:8px;margin:8px 0">
            <button id="manageProfileSelectAll" class="app-dialog-btn" style="font-size:12px;padding:4px 10px">${esc(selectAllLabel)}</button>
            <button id="manageProfileDeselectAll" class="app-dialog-btn" style="font-size:12px;padding:4px 10px">${esc(deselectAllLabel)}</button>
          </div>
          <div id="manageProfileList" style="max-height:320px;overflow-y:auto;display:flex;flex-direction:column;gap:6px;margin:8px 0">
            ${profileCards}
          </div>
          <div class="app-dialog-actions">
            <button class="app-dialog-btn" id="manageProfileCancel">${esc(cancelLabel)}</button>
            <button class="app-dialog-btn confirm" id="manageProfileConfirm">${esc(confirmLabel)}</button>
          </div>
        </div>
      `;

      document.body.appendChild(overlay);

      const close = (result) => {
        overlay.remove();
        if (result) resolve(result);
        else reject(new Error('cancelled'));
      };

      // Event handlers
      overlay.querySelector('#manageProfileClose').onclick = () => close(null);
      overlay.querySelector('#manageProfileCancel').onclick = () => close(null);
      overlay.addEventListener('click', (e) => {
        if (e.target === overlay) close(null);
      });
      document.addEventListener('keydown', function escHandler(ev) {
        if (ev.key === 'Escape') {
          document.removeEventListener('keydown', escHandler);
          close(null);
        }
      });

      // Select all / deselect all
      overlay.querySelector('#manageProfileSelectAll').onclick = () => {
        overlay.querySelectorAll('#manageProfileList input[type="checkbox"]').forEach(cb => {
          cb.checked = true;
        });
      };
      overlay.querySelector('#manageProfileDeselectAll').onclick = () => {
        overlay.querySelectorAll('#manageProfileList input[type="checkbox"]').forEach(cb => {
          cb.checked = false;
        });
      };

      // Hover effect for cards
      overlay.querySelectorAll('.install-profile-card').forEach(card => {
        card.addEventListener('mouseenter', () => { card.style.borderColor = 'var(--accent)'; });
        card.addEventListener('mouseleave', () => { card.style.borderColor = 'var(--border2)'; });
      });

      // Confirm - compute diff
      overlay.querySelector('#manageProfileConfirm').onclick = () => {
        const toInstall = [];
        const toUninstall = [];
        overlay.querySelectorAll('#manageProfileList input[type="checkbox"]').forEach(cb => {
          const profileName = cb.value;
          const wasInstalled = installedProfiles.has(profileName);
          if (cb.checked && !wasInstalled) {
            toInstall.push(profileName);
          } else if (!cb.checked && wasInstalled) {
            toUninstall.push(profileName);
          }
        });
        close({ install: toInstall, uninstall: toUninstall });
      };
    });
  }

  function pickUpload() {
    const input = $('skillhubUploadInput');
    if (input) input.click();
  }

  // ── AI-meta upload flow ──────────────────────────────────────────────

  function _showAiModal(file) {
    const overlay = document.createElement('div');
    overlay.id = 'aiMetaModalOverlay';
    overlay.className = 'app-dialog-overlay';
    overlay.style.display = 'flex';
    const nextLabel = typeof t === 'function' ? t('skillhub_next_step') : 'Next';
    const titleLabel = typeof t === 'function' ? t('skillhub_upload') : 'Upload Skill';
    const hintLabel = typeof t === 'function' ? t('skillhub_upload_drop_hint') : 'Select or drag a .md file';
    overlay.innerHTML = `
      <div class="app-dialog" role="dialog">
        <div class="app-dialog-title">${esc(titleLabel)}</div>
        <div class="app-dialog-desc" style="color:var(--muted);font-size:13px">${esc(hintLabel)}</div>
        <div id="aiMetaFileArea" style="margin:16px 0;padding:24px;border:2px dashed var(--border);border-radius:8px;text-align:center;cursor:pointer;color:var(--muted);font-size:13px">
          ${file ? esc(file.name) : (typeof t === 'function' ? t('skillhub_upload_browse') : 'Click to browse')}
        </div>
        <input type="file" id="aiMetaFileInput" accept=".md" style="display:none">
        <div id="aiMetaProgress" style="display:none;text-align:center;padding:12px 0">
          <div style="display:inline-block;width:24px;height:24px;border:3px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .8s linear infinite"></div>
          <div style="color:var(--muted);font-size:12px;margin-top:8px">${esc(typeof t === 'function' ? t('skillhub_ai_processing') : 'AI is analyzing...')}</div>
        </div>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
          <button type="button" class="btn" id="aiMetaCancelBtn">${esc(typeof t === 'function' ? t('cancel') : 'Cancel')}</button>
          <button type="button" class="btn primary" id="aiMetaNextBtn">${esc(nextLabel)}</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);

    let selectedFile = file;
    const fileArea = overlay.querySelector('#aiMetaFileArea');
    const fileInput = overlay.querySelector('#aiMetaFileInput');
    const cancelBtn = overlay.querySelector('#aiMetaCancelBtn');
    const nextBtn = overlay.querySelector('#aiMetaNextBtn');

    fileArea.addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', () => {
      const f = fileInput.files && fileInput.files[0];
      if (f) {
        selectedFile = f;
        fileArea.textContent = f.name;
      }
    });
    let cancelled = false;
    let abortCtrl = null;
    cancelBtn.addEventListener('click', () => {
      cancelled = true;
      if (abortCtrl) abortCtrl.abort();
      _hideAiModal();
    });
    nextBtn.addEventListener('click', async () => {
      if (!selectedFile) {
        const msg = typeof t === 'function' ? t('skillhub_upload_invalid_type') : 'Please select a .md file';
        if (typeof showToast === 'function') showToast(msg);
        return;
      }
      nextBtn.disabled = true;
      fileArea.style.display = 'none';
      overlay.querySelector('#aiMetaProgress').style.display = '';
      abortCtrl = new AbortController();
      await _processAiMetaUpload(selectedFile, () => cancelled, abortCtrl);
      if (!cancelled) _hideAiModal();
    });
  }

  function _hideAiModal() {
    const overlay = document.getElementById('aiMetaModalOverlay');
    if (overlay) overlay.remove();
  }

  async function _processAiMetaUpload(file, isCancelled, ac) {
    const isMd = file.name.toLowerCase().endsWith('.md');
    let content = '';

    if (isCancelled()) return;
    if (isMd) {
      try {
        content = await file.text();
      } catch (e) {
        if (isCancelled()) return;
        _hideAiModal();
        if (typeof showToast === 'function') showToast(e.message);
        return;
      }
      if (!content.trim()) {
        _hideAiModal();
        const msg = typeof t === 'function' ? t('skillhub_upload_empty') : 'File is empty';
        if (typeof showToast === 'function') showToast(msg);
        return;
      }
    } else {
      try {
        const fd = new FormData();
        fd.append('file', file, file.name);
        const url = new URL('api/skillhub/extract', document.baseURI || location.href).href;
        const res = await fetch(url, { method: 'POST', credentials: 'include', body: fd, signal: ac.signal });
        content = (await res.json()).content || '';
      } catch (e) {
        if (isCancelled()) return;
        _hideAiModal();
        if (typeof showToast === 'function') showToast(e.message);
        return;
      }
    }

    if (isCancelled()) return;
    _pendingUploadFile = file;

    let meta = {};
    if (content.trim()) {
      // Extract name/description from frontmatter to skip LLM extraction
      const fmMatch = content.match(/^---\s*\n([\s\S]*?)\n---/);
      let extractedName = '', extractedDesc = '';
      if (fmMatch) {
        const fm = fmMatch[1];
        const nameMatch = fm.match(/^name:\s*(.+)$/m);
        if (nameMatch) extractedName = nameMatch[1].trim();
        const descMatch = fm.match(/^description:\s*\|?\s*\n([\s\S]*?)(?=\n\w|\n---|\s*$)/m)
          || fm.match(/^description:\s*(.+)$/m);
        if (descMatch) extractedDesc = (descMatch[1] || '').trim().split('\n')[0].trim();
      }
      try {
        meta = await api('/api/skillhub/skill/ai-meta', {
          method: 'POST',
          body: JSON.stringify({ skillMdContent: content, name: extractedName, description: extractedDesc }),
          timeoutMs: 120000,
          signal: ac.signal,
        });
      } catch (e) {
        if (isCancelled()) return;
        if (typeof showToast === 'function') {
          const hint = typeof t === 'function' ? t('skillhub_ai_meta_failed') : 'AI extraction failed, filling manually';
          showToast(hint);
        }
      }
    }

    if (isCancelled()) return;
    _hideAiModal();
    try {
      await ensureCategories();
    } catch (_) {}
    if (!_skillhubCategories.length && Array.isArray(_skillhubData)) {
      const cats = new Set();
      for (const s of _skillhubData) {
        const c = String(s.category || '').trim();
        if (c) cats.add(c);
      }
      _skillhubCategories = [...cats].sort();
    }
    _showSkillCreateFormWithAiMeta(content, meta || {});
  }

  async function _renderUploadProfileSelect() {
    const container = $('aiMetaProfilesContainer');
    if (!container) return;
    try {
      const resp = await api('/api/profiles');
      const profiles = resp.profiles || [];
      _uploadProfilesCache = profiles;
      if (!profiles.length) { container.innerHTML = ''; return; }
      const label = typeof t === 'function' ? t('install_select_profiles') || 'Install to Assistants' : 'Install to Assistants';
      const hint = typeof t === 'function' ? t('install_default_hint') || 'Default is pre-selected. Uncheck to skip.' : 'Default is pre-selected. Uncheck to skip.';
      let html = `<label style="font-size:12px;font-weight:500;color:var(--text);margin-bottom:4px;display:block">${esc(label)}</label>`;
      html += `<div style="font-size:11px;color:var(--muted);margin-bottom:6px">${esc(hint)}</div>`;
      html += '<div style="display:flex;flex-wrap:wrap;gap:6px">';
      for (const p of profiles) {
        const info = p.info || {};
        const dn = info.display_name || p.name;
        const checked = p.is_default ? 'checked' : '';
        html += `<label style="display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border:1px solid var(--border2);border-radius:6px;cursor:pointer;font-size:12px;color:var(--text);background:var(--bg)">
          <input type="checkbox" value="${esc(p.name)}" ${checked} class="ai-meta-profile-cb" style="accent-color:var(--accent)">
          ${esc(dn)}
        </label>`;
      }
      html += '</div>';
      container.innerHTML = html;
    } catch (_) { container.innerHTML = ''; }
  }

  function _showSkillCreateFormWithAiMeta(content, meta) {
    _pendingDetailJson = meta.detailJson || null;

    const body = $('skillhubDetailBody');
    const empty = $('skillhubDetailEmpty');
    const title = $('skillhubDetailTitle');
    if (!body || !title) return;

    const nameLabel = typeof t === 'function' ? t('skill_name') : 'Name';
    const descLabel = typeof t === 'function' ? t('skill_description') : 'Description';
    const contentLabel = typeof t === 'function' ? t('skill_content') : 'SKILL.md content';
    const saveLabel = typeof t === 'function' ? t('skills_save') || 'Save' : 'Save';
    const cancelLabel = typeof t === 'function' ? t('cancel') : 'Cancel';
    const displayNameLabel = typeof t === 'function' ? t('skill_display_name') : 'Display name';
    const displayDescLabel = typeof t === 'function' ? t('skill_display_desc') : 'Display description';
    const categoryLabel = typeof t === 'function' ? t('skill_category') : 'Category';

    const catPlaceholder = typeof t === 'function' ? t('skill_category_placeholder') : 'Optional, e.g. devops';
    let categoryHtml;
    if (_skillhubCategories.length) {
      const defaultCat = (_skillhubCategory && _skillhubCategory !== CATEGORY_ALL && _skillhubCategories.includes(_skillhubCategory))
        ? _skillhubCategory : _skillhubCategories[0];
      const catOptions = _skillhubCategories.map(c =>
        `<option value="${esc(c)}" ${c === defaultCat ? 'selected' : ''}>${esc(c)}</option>`
      ).join('');
      categoryHtml = `<select id="aiMetaCategory" class="ai-meta-category-select">${catOptions}</select>`;
    } else {
      categoryHtml = `<input type="text" id="aiMetaCategory" value="" placeholder="${esc(catPlaceholder)}">`;
    }

    title.textContent = saveLabel;

    body.innerHTML = `
      <div class="main-view-content">
        <form class="detail-form" id="aiMetaSkillForm" onsubmit="event.preventDefault()">
          <div class="detail-form-row">
            <label>${esc(nameLabel)} (EN)</label>
            <input type="text" id="aiMetaName" value="${esc(meta.name || '')}" placeholder="english-slug" pattern="^[a-zA-Z0-9_-]+$" title="Only letters, numbers, underscores, and hyphens are allowed">
          </div>
          <div class="detail-form-row">
            <label>${esc(descLabel)} (EN)</label>
            <input type="text" id="aiMetaDesc" value="${esc(meta.description || '')}" placeholder="English description">
          </div>
          <div class="detail-form-row">
            <label>${esc(displayNameLabel)} (CN)</label>
            <input type="text" id="aiMetaSkillName" value="${esc(meta.skillName || '')}" placeholder="中文显示名">
          </div>
          <div class="detail-form-row">
            <label>${esc(displayDescLabel)} (CN)</label>
            <input type="text" id="aiMetaDisplayDesc" value="${esc(meta.displayDescription || '')}" placeholder="中文描述">
          </div>
          <div class="detail-form-row">
            <label>${esc(categoryLabel)}</label>
            ${categoryHtml}
          </div>
          <div class="detail-form-row" id="aiMetaProfilesContainer" style="margin-top:4px"></div>
          <div class="detail-form-row">
            <label>${esc(contentLabel)}</label>
            <textarea id="aiMetaContent" rows="18" readonly>${esc(content || '')}</textarea>
          </div>
          <div id="aiMetaFormError" class="detail-form-error" style="display:none"></div>
          <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
            <button type="button" class="btn" id="aiMetaFormCancelBtn">${esc(cancelLabel)}</button>
            <button type="button" class="btn primary" id="aiMetaFormSubmitBtn">${esc(saveLabel)}</button>
          </div>
        </form>
      </div>`;

    body.style.display = '';
    if (empty) empty.style.display = 'none';
    _skillhubMode = 'edit';
    _setSkillhubHeaderButtons('empty');

    body.querySelector('#aiMetaFormCancelBtn').addEventListener('click', () => cancelAiMetaForm());
    body.querySelector('#aiMetaFormSubmitBtn').addEventListener('click', () => submitAiMetaForm());
    _renderUploadProfileSelect();
  }

  async function submitAiMetaForm() {
    const errEl = $('aiMetaFormError');
    const contentEl = $('aiMetaContent');
    if (!contentEl) return;
    if (errEl) errEl.style.display = 'none';

    const content = contentEl.value;
    if (!content.trim()) {
      if (errEl) {
        errEl.textContent = typeof t === 'function' ? t('content_required') || 'Content is required' : 'Content is required';
        errEl.style.display = '';
      }
      return;
    }

    // Validate Name (EN): only letters, numbers, underscores, hyphens
    const rawName = ($('aiMetaName') && $('aiMetaName').value || '').trim();
    if (rawName && !/^[a-zA-Z0-9_-]+$/.test(rawName)) {
      if (errEl) {
        errEl.textContent = typeof t === 'function' ? t('skill_name_invalid') || 'Name (EN) can only contain letters, numbers, underscores, and hyphens' : 'Name (EN) can only contain letters, numbers, underscores, and hyphens';
        errEl.style.display = '';
      }
      return;
    }

    // Check selected profiles for existing skills with same name/display_name
    const formName = rawName.toLowerCase();
    const formDisplayName = ($('aiMetaSkillName') && $('aiMetaSkillName').value || '').trim().toLowerCase();
    if ((formName || formDisplayName) && _uploadProfilesCache) {
      // Determine target profiles: use checked ones, fallback to "default"
      let targetProfileNames = [];
      document.querySelectorAll('.ai-meta-profile-cb:checked').forEach(cb => {
        targetProfileNames.push(cb.value);
      });
      if (targetProfileNames.length === 0) {
        targetProfileNames = ['default'];
      }
      for (const pname of targetProfileNames) {
        const profile = _uploadProfilesCache.find(p => p.name === pname);
        if (!profile) continue;
        const profileSkills = profile.skills || [];
        for (const ps of profileSkills) {
          const psName = String(ps.name || '').trim().toLowerCase();
          const psDisplay = String(ps.display_name || '').trim().toLowerCase();
          const profileLabel = (profile.info && profile.info.display_name) || pname;
          if (formName && psName && formName === psName) {
            if (errEl) {
              errEl.textContent = profileLabel + ' 已内置技能 ' + ps.name + '，请重新选择助理';
              errEl.style.display = '';
            }
            return;
          }
          if (formDisplayName && psDisplay && formDisplayName === psDisplay) {
            if (errEl) {
              errEl.textContent = profileLabel + ' 已内置技能 ' + (ps.display_name || ps.name) + '，请重新选择助理';
              errEl.style.display = '';
            }
            return;
          }
        }
      }
    }

    const submitBtn = $('aiMetaFormSubmitBtn');
    if (submitBtn) submitBtn.disabled = true;

    try {
      // Step 1: Upload file (deferred from "下一步")
      let uploadedName = '';
      let uploadedDirName = '';
      if (_pendingUploadFile) {
        const fd = new FormData();
        fd.append('file', _pendingUploadFile, _pendingUploadFile.name);
        const formCategory = ($('aiMetaCategory') && $('aiMetaCategory').value) || '';
        if (formCategory) {
          fd.append('category', formCategory);
        }
        if (_skillhubScope === 'custom') {
          fd.append('overwrite', '1');
        }
        const checkName = ($('aiMetaName') && $('aiMetaName').value || '').trim();
        const checkDisplay = ($('aiMetaSkillName') && $('aiMetaSkillName').value || '').trim();
        // Always send name for directory naming and duplicate checking
        if (checkName) {
          fd.append('name', checkName);
          fd.append('check_name', checkName);
        }
        if (checkDisplay) fd.append('check_display_name', checkDisplay);
        const url = new URL('api/skillhub/upload', document.baseURI || location.href).href;
        const res = await fetch(url, { method: 'POST', credentials: 'include', body: fd });
        if (res.status === 401) {
          window.location.href = 'login?next=' + encodeURIComponent(window.location.pathname + window.location.search);
          return;
        }
        const text = await res.text();
        let data = {};
        try { data = JSON.parse(text); } catch (_) {}
        if (!res.ok) {
          const message = data.error || data.message || text || res.statusText;
          throw new Error(message);
        }
        if (data.error) throw new Error(data.error);
        const uploaded = Array.isArray(data.skills) ? data.skills[0] : null;
        if (uploaded) {
          uploadedName = uploaded.name || '';
          uploadedDirName = uploaded.dir_name || '';
        }
      }

      // Step 2: Save detail.json with full metadata (use actual uploaded name/dir_name)
      const detailName = uploadedName || ($('aiMetaName') && $('aiMetaName').value) || '';
      if (detailName) {
        const detailPayload = {
          name: ($('aiMetaName') && $('aiMetaName').value || '').trim(),
          description: ($('aiMetaDesc') && $('aiMetaDesc').value || '').trim(),
          display_name: ($('aiMetaSkillName') && $('aiMetaSkillName').value || '').trim(),
          display_description: ($('aiMetaDisplayDesc') && $('aiMetaDisplayDesc').value || '').trim(),
          category: ($('aiMetaCategory') && $('aiMetaCategory').value || '').trim(),
          detail_json: _pendingDetailJson || null,
        };
        try {
          await api('/api/skillhub/skill/detail', {
            method: 'POST',
            body: JSON.stringify({ name: detailName, dir_name: uploadedDirName, detail: detailPayload }),
          });
        } catch (detailErr) {
          console.warn('[SkillHub] detail.json save failed:', detailErr);
        }
      }

      // Step 3: Install/uninstall to match selected profiles
      // Upload always writes to shared_skills_dir (default profile).
      // If default is unchecked AND skill is new (not re-upload), remove from default.
      try {
        const selectedProfiles = [];
        document.querySelectorAll('.ai-meta-profile-cb:checked').forEach(cb => {
          selectedProfiles.push(cb.value);
        });
        // If user unchecked everything, default to "default"
        if (selectedProfiles.length === 0) {
          selectedProfiles.push('default');
        }
        const defaultSelected = selectedProfiles.includes('default');
        const installProfiles = selectedProfiles.filter(p => p !== 'default');
        // Detect re-upload: skill already exists in default → don't uninstall from default
        const skillNameForCheck = uploadedName || detailName;
        const existingInDefault = Array.isArray(_skillhubData) && _skillhubData.some(s => s.name === skillNameForCheck);
        const uninstallProfiles = (defaultSelected || existingInDefault) ? [] : ['default'];
        if (installProfiles.length > 0 || uninstallProfiles.length > 0) {
          const skillName = uploadedName || detailName;
          const displayN = ($('aiMetaSkillName') && $('aiMetaSkillName').value || '').trim();
          const catVal = ($('aiMetaCategory') && $('aiMetaCategory').value || '').trim();
          await api('/api/skillhub/sync-profiles', {
            method: 'POST',
            body: JSON.stringify({
              name: skillName,
              display_name: displayN,
              category: catVal,
              is_custom: true,
              install: installProfiles,
              uninstall: uninstallProfiles,
            }),
          });
        }
      } catch (profileErr) {
        console.warn('[SkillHub] install to profiles failed:', profileErr);
      }

      _pendingUploadFile = null;
      _pendingDetailJson = null;
      _uploadProfilesCache = null;
      _skillhubData = null;
      _currentSkillhubItem = null;
      if (typeof _invalidateSkillsDataCache === 'function') _invalidateSkillsDataCache();
      await loadSkillHub(true);
      if (typeof loadSkills === 'function') await loadSkills();

      const savedName = uploadedName || detailName;
      const savedDirName = uploadedDirName;
      const savedItem =
        (_skillhubData || []).find(s => s.name === savedName) ||
        (_skillhubData || []).find(s => s.dir_name === savedDirName);
      if (savedItem) {
        await openSkillHubItem(savedItem, null);
      }

      if (typeof showToast === 'function') {
        showToast(typeof t === 'function' ? t('skillhub_upload_ok') || 'Skill created' : 'Skill created');
      }
    } catch (e) {
      if (errEl) {
        errEl.textContent = (typeof t === 'function' ? t('error_prefix') || 'Error: ' : 'Error: ') + (e.message || String(e));
        errEl.style.display = '';
      }
    } finally {
      if (submitBtn) submitBtn.disabled = false;
    }
  }

  function cancelAiMetaForm() {
    _pendingUploadFile = null;
    _pendingDetailJson = null;
    _uploadProfilesCache = null;
    clearDetail();
  }

  async function handleUploadFile(file) {
    if (!file) return;
    if (!isAllowedSkillUploadFile(file)) {
      const msg = typeof t === 'function' ? t('skillhub_upload_invalid_type') : 'Only .md and .zip files are supported';
      if (typeof showToast === 'function') showToast(msg);
      return;
    }
    _showAiModal(file);
  }



  async function uploadCustomSkill(file) {
    if (!file) return;
    const fd = new FormData();
    fd.append('file', file, file.name);
    if (_skillhubCategory && _skillhubCategory !== CATEGORY_ALL) {
      fd.append('category', _skillhubCategory);
    }
    if (_skillhubScope === 'custom') {
      fd.append('overwrite', '1');
    }
    const url = new URL('api/skillhub/upload', document.baseURI || location.href).href;
    const res = await fetch(url, { method: 'POST', credentials: 'include', body: fd });
    if (res.status === 401) {
      window.location.href = 'login?next=' + encodeURIComponent(window.location.pathname + window.location.search);
      return;
    }
    const text = await res.text();
    let data = {};
    try {
      data = JSON.parse(text);
    } catch (_) {}
    if (!res.ok) {
      const message = data.error || data.message || text || res.statusText;
      throw new Error(message);
    }
    if (data.error) throw new Error(data.error);
    return data;
  }

  function setScope(nextScope) {
    if (!VALID_SCOPES.has(nextScope)) return;
    if (_skillhubScope === nextScope) return;
    _skillhubScope = nextScope;
    _skillhubPage = 1;
    _skillhubData = null;
    _currentSkillhubItem = null;
    _batchMode = false;
    _selectedSkills.clear();
    _updateBatchBar();
    persistScope();
    updateScopeTabs();
    clearDetail();
    loadSkillHub(true);
  }

  function setCategory(nextCategory) {
    const value = nextCategory == null ? CATEGORY_ALL : String(nextCategory);
    if (_skillhubCategory === value) return;
    _skillhubCategory = value;
    _skillhubPage = 1;
    _skillhubData = null;
    _currentSkillhubItem = null;
    _batchMode = false;
    _selectedSkills.clear();
    _updateBatchBar();
    persistCategory();
    renderCategoryChips();
    clearDetail();
    loadSkillHub(true);
  }

  function clearDetail() {
    const body = $('skillhubDetailBody');
    const empty = $('skillhubDetailEmpty');
    const title = $('skillhubDetailTitle');
    _skillhubMode = 'empty';
    _skillhubPreFormDetail = null;
    if (title) title.textContent = '';
    if (body) {
      body.innerHTML = '';
      body.style.display = 'none';
    }
    if (empty) empty.style.display = '';
    _setSkillhubHeaderButtons('empty');
  }

  function filterSkillHub() {
    clearTimeout(_searchTimer);
    _searchTimer = setTimeout(() => {
      _skillhubPage = 1;
      _skillhubData = null;
      loadSkillHub(true);
    }, 250);
  }

  function bindUploadDropzone() {
    const zone = $('skillhubUploadDropzone');
    const inner = $('skillhubUploadDropzoneInner');
    if (!zone || !inner) return;

    let dragDepth = 0;

    inner.addEventListener('click', () => pickUpload());

    zone.addEventListener('dragenter', ev => {
      ev.preventDefault();
      dragDepth += 1;
      zone.classList.add('is-dragover');
    });
    zone.addEventListener('dragover', ev => {
      ev.preventDefault();
      if (ev.dataTransfer) ev.dataTransfer.dropEffect = 'copy';
    });
    zone.addEventListener('dragleave', ev => {
      ev.preventDefault();
      dragDepth = Math.max(0, dragDepth - 1);
      if (dragDepth === 0) zone.classList.remove('is-dragover');
    });
    zone.addEventListener('drop', ev => {
      ev.preventDefault();
      dragDepth = 0;
      zone.classList.remove('is-dragover');
      const file = ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files[0];
      void handleUploadFile(file);
    });
  }

  function bindSkillHubControls() {
    document.querySelectorAll('#skillhubScopeTabs .skillhub-scope-btn').forEach(btn => {
      btn.addEventListener('click', () => setScope(btn.dataset.scope));
    });
    const storedSort = readStoredSort();
    _skillhubSort = storedSort.sort;
    _skillhubOrder = storedSort.order;
    renderSortSelect();
    const uploadInput = $('skillhubUploadInput');
    if (uploadInput) {
      uploadInput.addEventListener('change', () => {
        const file = uploadInput.files && uploadInput.files[0];
        uploadInput.value = '';
        void handleUploadFile(file);
      });
    }
    bindUploadDropzone();
    const sortSelect = $('skillhubSort');
    if (sortSelect) {
      sortSelect.addEventListener('change', () => setSortFromValue(sortSelect.value));
    }
    updateCustomUploadVisibility();
    // Batch mode buttons
    const batchInstallBtn = $('btnBatchInstall');
    const batchUninstallBtn = $('btnBatchUninstall');
    const batchCancelBtn = $('btnBatchCancel');
    if (batchInstallBtn) batchInstallBtn.addEventListener('click', batchInstallSelected);
    if (batchUninstallBtn) batchUninstallBtn.addEventListener('click', batchUninstallSelected);
    if (batchCancelBtn) batchCancelBtn.addEventListener('click', () => {
      _batchMode = false;
      _selectedSkills.clear();
      _updateBatchBar();
      renderSkillHubList(_skillhubData || []);
    });
  }

  bindSkillHubControls();

  // ── Batch mode ──────────────────────────────────────────────

  function toggleBatchMode() {
    _batchMode = !_batchMode;
    if (!_batchMode) _selectedSkills.clear();
    _updateBatchBar();
    renderSkillHubList(_skillhubData || []);
  }

  function _updateBatchBar() {
    const bar = $('skillhubBatchBar');
    if (bar) bar.style.display = _batchMode ? '' : 'none';
    const countEl = $('skillhubBatchCount');
    if (countEl) {
      const n = _selectedSkills.size;
      countEl.textContent = (typeof t === 'function' ? t('skillhub_batch_selected') || '{0} selected' : '{0} selected').replace('{0}', n);
    }
    const installBtn = $('btnBatchInstall');
    const uninstallBtn = $('btnBatchUninstall');
    if (installBtn) installBtn.disabled = _selectedSkills.size === 0;
    if (uninstallBtn) uninstallBtn.disabled = _selectedSkills.size === 0;
    const toggleBtn = $('btnSkillhubBatch');
    if (toggleBtn) toggleBtn.classList.toggle('active', _batchMode);
  }

  async function batchInstallSelected() {
    if (_selectedSkills.size === 0) return;
    const skills = [];
    const data = _skillhubData || [];
    for (const name of _selectedSkills) {
      const skill = data.find(s => s.name === name);
      if (!skill) continue;
      const isCustom = _skillhubScope === 'custom' || skill.custom === true;
      skills.push({
        name: skill.name,
        display_name: skill.display_name || skill.install_name || skill.name,
        category: skill.category || '',
        is_custom: isCustom,
      });
    }
    if (skills.length === 0) return;

    let profiles;
    try {
      const label = skills.map(s => s.display_name || s.name).join(', ');
      profiles = await _showInstallProfileDialog(skills[0].name, label, skills.length);
    } catch (_) { return; }
    if (!profiles || profiles.length === 0) return;

    try {
      const resp = await api('/api/skillhub/batch-install', {
        method: 'POST',
        body: JSON.stringify({ skills, profiles }),
      });
      const results = resp.results || [];
      const ok = results.filter(r => r.ok && !r.skipped).length;
      const skipped = results.filter(r => r.skipped).length;
      const fail = results.filter(r => !r.ok).length;

      _skillhubData = null;
      _selectedSkills.clear();
      _batchMode = false;
      _updateBatchBar();
      if (typeof _invalidateSkillsDataCache === 'function') _invalidateSkillsDataCache();
      await loadSkillHub(true);
      if (typeof loadSkills === 'function') await loadSkills();
      if (typeof showToast === 'function') {
        if (fail === 0) {
          const msg = typeof t === 'function' ? t('batch_install_success') || 'Installed {0} skills ({1} skipped)' : 'Installed {0} skills ({1} skipped)';
          showToast(msg.replace('{0}', ok).replace('{1}', skipped));
        } else {
          const msg = typeof t === 'function' ? t('batch_install_partial') || '{0} installed, {1} failed' : '{0} installed, {1} failed';
          showToast(msg.replace('{0}', ok).replace('{1}', fail), 5000, 'error');
        }
      }
    } catch (e) {
      if (typeof showToast === 'function') showToast(e.message, 5000, 'error');
    }
  }

  async function batchUninstallSelected() {
    if (_selectedSkills.size === 0) return;
    const skills = [];
    const data = _skillhubData || [];
    for (const name of _selectedSkills) {
      const skill = data.find(s => s.name === name);
      if (!skill) continue;
      skills.push({ name: skill.name, dir_name: skill.dir_name || '' });
    }
    if (skills.length === 0) return;

    const count = skills.length;
    const message = typeof t === 'function' && t('batch_delete_confirm')
      ? t('batch_delete_confirm').replace('{0}', count)
      : `Delete ${count} skill(s) from all assistants?`;
    if (typeof showConfirmDialog === 'function') {
      const ok = await showConfirmDialog({
        title: typeof t === 'function' ? t('delete_title') : 'Delete',
        message,
        confirmLabel: typeof t === 'function' ? t('delete_title') : 'Delete',
        danger: true,
        focusCancel: true,
      });
      if (!ok) return;
    }

    try {
      const resp = await api('/api/skillhub/batch-uninstall', {
        method: 'POST',
        body: JSON.stringify({ skills }),
      });
      let totalOk = 0, totalFail = 0;
      for (const sr of (resp.results || [])) {
        for (const pr of (sr.results || [])) {
          if (pr.ok) totalOk++; else totalFail++;
        }
      }

      _skillhubData = null;
      _currentSkillhubItem = null;
      _selectedSkills.clear();
      _batchMode = false;
      _updateBatchBar();
      if (typeof _invalidateSkillsDataCache === 'function') _invalidateSkillsDataCache();
      clearDetail();
      await loadSkillHub(true);
      if (typeof loadSkills === 'function') await loadSkills();
      if (typeof showToast === 'function') {
        if (totalFail === 0) {
          showToast(typeof t === 'function' ? t('skill_deleted') || 'Removed' : `${count} skill(s) removed`);
        } else {
          showToast(`${totalOk} removed, ${totalFail} failed`, 5000, 'error');
        }
      }
    } catch (e) {
      if (typeof showToast === 'function') showToast(e.message, 5000, 'error');
    }
  }

  window.filterSkillHub = filterSkillHub;

  window.HermesSkillHub = {
    loadSkillHub,
    renderSkillHubList,
    openSkillHubItem,
    installCurrent,
    deleteCurrent,
    manageProfilesCurrent,
    editCurrent,
    cancelEditForm,
    saveEditForm,
    downloadCurrent,
    filterSkillHub,
    setScope,
    setCategory,
    pickUpload,
    uploadCustomSkill,
    handleUploadFile,
    submitAiMetaForm,
    cancelAiMetaForm,
    toggleBatchMode,
    batchInstallSelected,
    batchUninstallSelected,
  };

  // Sync skill lock state from Skills panel
  window.addEventListener('hermes:skill-lock-toggle', (ev) => {
    const { name, locked } = ev.detail || {};
    if (!name || !_skillhubData) return;
    const skill = _skillhubData.find(s => s.name === name);
    if (skill) {
      skill.no_self_improve = !!locked;
      renderSkillHubList(_skillhubData);
    }
  });

  // Sync skill toggle state from Skills panel
  window.addEventListener('hermes:skill-toggle', (ev) => {
    const { name, enabled } = ev.detail || {};
    if (!name || !_skillhubData) return;
    const skill = _skillhubData.find(s => s.name === name);
    if (skill) {
      skill.disabled = !enabled;
      renderSkillHubList(_skillhubData);
    }
  });
})();
