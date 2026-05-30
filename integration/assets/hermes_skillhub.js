(function () {
  'use strict';

  const cfg = window.__HERMES_CONFIG__ || {};
  if (!cfg.integrationSkills && !cfg.skillhubEnabled) return;

  const hubReady = !!cfg.skillhubEnabled;
  const integrationReady = !!cfg.integrationSkills;
  const STORAGE_SCOPE = 'hermes.skillhub.scope';
  const STORAGE_CATEGORY = 'hermes.skillhub.category';
  const CATEGORY_ALL = '';
  const VALID_SCOPES = new Set(['hub', 'installed', 'not_installed', 'custom']);

  let _skillhubScope = 'hub';
  let _skillhubCategory = CATEGORY_ALL;
  let _skillhubCategories = [];
  let _skillhubStats = null;
  let _skillhubData = null;
  let _skillhubPage = 1;
  let _skillhubPageSize = 20;
  let _skillhubTotal = 0;
  let _currentSkillhubItem = null;
  let _searchTimer = null;

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
    renderCategoryChips();
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
    const sorted = skills.slice().sort((a, b) => (a.name || '').localeCompare(b.name || ''));
    for (const skill of sorted) {
      const el = document.createElement('div');
      const installed = skill.installed === true;
      const isCustom = _skillhubScope === 'custom' || skill.custom === true;
      const showCatalogOnly = _skillhubScope === 'hub' && !installed;
      el.className = 'skill-item' + (showCatalogOnly ? ' catalog-only' : '');
      const nameEl = document.createElement('span');
      nameEl.className = 'skill-name';
      nameEl.textContent = skill.display_name || skill.install_name || skill.name;
      const descEl = document.createElement('span');
      descEl.className = 'skill-desc';
      descEl.textContent = skill.description || '';
      el.append(nameEl, descEl);
      el.onclick = () => openSkillHubItem(skill, el);
      box.appendChild(el);
    }
  }

  async function openSkillHubItem(skill, el) {
    document.querySelectorAll('#skillhubList .skill-item').forEach(e => e.classList.remove('active'));
    if (el) el.classList.add('active');
    _currentSkillhubItem = skill;
    const name = skill.name;
    const title = $('skillhubDetailTitle');
    const body = $('skillhubDetailBody');
    const empty = $('skillhubDetailEmpty');
    const installBtn = $('btnSkillhubInstall');
    const uninstallBtn = $('btnSkillhubUninstall');
    if (title) title.textContent = skill.display_name || skill.name;
    const isCustom = _skillhubScope === 'custom' || skill.custom === true;
    const installed = skill.installed === true;
    const canInstall = !isCustom && (_skillhubScope === 'hub' || _skillhubScope === 'not_installed') && !installed;
    const canDelete =
      (isCustom && _skillhubScope === 'custom') ||
      (!isCustom && (_skillhubScope === 'hub' || _skillhubScope === 'installed') && installed);
    if (installBtn) installBtn.style.display = canInstall ? '' : 'none';
    if (uninstallBtn) {
      uninstallBtn.style.display = canDelete ? '' : 'none';
      const tip = typeof t === 'function' ? t('delete_title') : 'Delete';
      uninstallBtn.setAttribute('data-tooltip', tip);
      uninstallBtn.setAttribute('data-i18n-title', 'delete_title');
    }
    const scopeParam = isCustom ? '&scope=custom' : '';
    try {
      const [doc, structure] = await Promise.all([
        api(`/api/skillhub/content?name=${encodeURIComponent(name)}${scopeParam}`),
        api(`/api/skillhub/structure?name=${encodeURIComponent(name)}${scopeParam}`).catch(() => null),
      ]);
      let html = '';
      if (isCustom) {
        const hint = typeof t === 'function' ? t('skillhub_custom_hint') : 'Local custom skill.';
        html += `<p class="skillhub-custom-hint" style="color:var(--muted);font-size:12px;margin:0 0 12px">${esc(hint)}</p>`;
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
    } catch (e) {
      if (body) {
        body.innerHTML = `<div class="main-view-content"><div class="detail-form-error" style="display:block">${esc(e.message)}</div></div>`;
        body.style.display = '';
      }
      if (empty) empty.style.display = 'none';
    }
  }

  async function openSkillHubFile(name, path) {
    const body = $('skillhubDetailBody');
    const scopeParam = _skillhubScope === 'custom' ? '&scope=custom' : '';
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
    try {
      await api('/api/skillhub/install', {
        method: 'POST',
        body: JSON.stringify({
          name,
          display_name: _currentSkillhubItem.display_name || _currentSkillhubItem.install_name || name,
          category: _currentSkillhubItem.category || '',
        }),
      });
      _skillhubData = null;
      if (typeof _skillsData !== 'undefined') _skillsData = null;
      await loadSkillHub(true);
      if (typeof loadSkills === 'function') await loadSkills();
      if (_currentSkillhubItem) {
        _currentSkillhubItem.installed = true;
        openSkillHubItem(_currentSkillhubItem, null);
      }
      if (typeof showToast === 'function') {
        showToast(typeof t === 'function' ? t('skill_installed') || 'Installed' : 'Installed');
      }
    } catch (e) {
      if (typeof showToast === 'function') showToast(e.message);
    }
  }

  async function deleteCurrent() {
    if (!_currentSkillhubItem) return;
    const name = _currentSkillhubItem.name;
    const label = _currentSkillhubItem.display_name || name;
    const message = typeof t === 'function' && t('skill_delete_confirm')
      ? t('skill_delete_confirm').replace('{0}', label)
      : `Delete skill "${label}"?`;
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
      await api('/api/skillhub/delete', {
        method: 'POST',
        body: JSON.stringify({
          name,
          dir_name: _currentSkillhubItem.dir_name || '',
        }),
      });
      _skillhubData = null;
      _currentSkillhubItem = null;
      if (typeof _skillsData !== 'undefined') _skillsData = null;
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

  function pickUpload() {
    const input = $('skillhubUploadInput');
    if (input) input.click();
  }

  async function handleUploadFile(file) {
    if (!file) return;
    if (!isAllowedSkillUploadFile(file)) {
      const msg = typeof t === 'function' ? t('skillhub_upload_invalid_type') : 'Only .md and .zip files are supported';
      if (typeof showToast === 'function') showToast(msg);
      return;
    }
    setUploadDropzoneBusy(true);
    try {
      await uploadCustomSkill(file);
    } catch (e) {
      const msg =
        (typeof t === 'function' ? t('skillhub_upload_failed') : 'Upload failed') +
        (e.message ? ': ' + e.message : '');
      if (typeof showToast === 'function') showToast(msg);
    } finally {
      setUploadDropzoneBusy(false);
    }
  }

  async function uploadCustomSkill(file) {
    if (!file) return;
    const fd = new FormData();
    fd.append('file', file, file.name);
    if (_skillhubCategory && _skillhubCategory !== CATEGORY_ALL) {
      fd.append('category', _skillhubCategory);
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
    _skillhubData = null;
    _currentSkillhubItem = null;
    await loadSkillHub(true);
    const imported = Array.isArray(data.skills) ? data.skills : [];
    const skillCount = data.skill_count ?? imported.length ?? 0;
    if (typeof showToast === 'function') {
      const base = typeof t === 'function' ? t('skillhub_upload_ok') : 'Uploaded';
      showToast(skillCount > 1 ? `${base} (${skillCount})` : base);
    }
    const first = imported[0];
    if (first && first.name) {
      const item = (_skillhubData || []).find(s => s.name === first.name);
      if (item) openSkillHubItem(item);
    }
  }

  function setScope(nextScope) {
    if (!VALID_SCOPES.has(nextScope)) return;
    if (_skillhubScope === nextScope) return;
    _skillhubScope = nextScope;
    _skillhubPage = 1;
    _skillhubData = null;
    _currentSkillhubItem = null;
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
    persistCategory();
    renderCategoryChips();
    clearDetail();
    loadSkillHub(true);
  }

  function clearDetail() {
    const body = $('skillhubDetailBody');
    const empty = $('skillhubDetailEmpty');
    const title = $('skillhubDetailTitle');
    if (title) title.textContent = '';
    if (body) {
      body.innerHTML = '';
      body.style.display = 'none';
    }
    if (empty) empty.style.display = '';
    $('btnSkillhubInstall')?.style && ($('btnSkillhubInstall').style.display = 'none');
    $('btnSkillhubUninstall')?.style && ($('btnSkillhubUninstall').style.display = 'none');
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
    const uploadInput = $('skillhubUploadInput');
    if (uploadInput) {
      uploadInput.addEventListener('change', () => {
        const file = uploadInput.files && uploadInput.files[0];
        uploadInput.value = '';
        void handleUploadFile(file);
      });
    }
    bindUploadDropzone();
    updateCustomUploadVisibility();
  }

  bindSkillHubControls();

  window.filterSkillHub = filterSkillHub;

  window.HermesSkillHub = {
    loadSkillHub,
    renderSkillHubList,
    openSkillHubItem,
    installCurrent,
    deleteCurrent,
    filterSkillHub,
    setScope,
    setCategory,
    pickUpload,
    uploadCustomSkill,
    handleUploadFile,
  };
})();
