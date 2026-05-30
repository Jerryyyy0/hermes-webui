(function () {
  'use strict';

  const cfg = window.__HERMES_CONFIG__ || {};
  if (!cfg.integrationSkills) return;

  let _presetsCache = null;
  let _logoPickerState = { mode: 'none', presetId: '', logoBase64: '' };

  function profileInfo(p) {
    return (p && p.info && typeof p.info === 'object') ? p.info : {};
  }

  function profileTitle(p) {
    const info = profileInfo(p);
    const title = (info.display_name || '').trim() || p.name;
    const sub = info.display_name && info.display_name !== p.name ? p.name : '';
    return { title, sub };
  }

  function profileLogo(p) {
    return profileInfo(p).logo || '';
  }

  function profileDescription(p) {
    return profileInfo(p).description || '';
  }

  function logoImg(p, cls) {
    const src = profileLogo(p);
    if (!src) return '';
    return `<img class="${cls}" src="${src}" alt="" loading="lazy">`;
  }

  function resetLogoPickerState(initial) {
    _logoPickerState = { mode: 'none', presetId: '', logoBase64: '' };
    if (initial && initial.logo) {
      _logoPickerState.mode = 'custom';
      _logoPickerState.logoBase64 = initial.logo;
    }
  }

  async function loadPresets() {
    if (_presetsCache) return _presetsCache;
    _presetsCache = await api('/api/profile/logo-presets');
    return _presetsCache;
  }

  function logoPickerPayload() {
    const s = _logoPickerState;
    if (s.mode === 'remove') return { remove_logo: true };
    if (s.mode === 'preset' && s.presetId) return { logo_preset: s.presetId };
    if (s.mode === 'custom' && s.logoBase64) return { logo_base64: s.logoBase64 };
    return {};
  }

  async function renderLogoPicker(container) {
    if (!container) return;
    const data = await loadPresets();
    const categories = data.categories || [];
    const presets = data.presets || [];
    const activeCat = container.dataset.activeCategory || (categories[0] && categories[0].id) || '';
    container.dataset.activeCategory = activeCat;
    const filtered = activeCat ? presets.filter(p => p.category === activeCat) : presets;

    const tabs = categories.map(c =>
      `<button type="button" class="profile-logo-tab${c.id === activeCat ? ' active' : ''}" data-cat="${esc(c.id)}">${esc(c.label)}</button>`
    ).join('');

    const grid = filtered.map(p => {
      const sel = _logoPickerState.mode === 'preset' && _logoPickerState.presetId === p.id ? ' selected' : '';
      return `<button type="button" class="profile-logo-preset${sel}" data-preset-id="${esc(p.id)}" title="${esc(p.label)}"><img src="${esc(p.url)}" alt="${esc(p.label)}"></button>`;
    }).join('');

    const preview = _logoPickerState.mode === 'custom' && _logoPickerState.logoBase64
      ? `<img class="profile-logo-upload-preview" src="${_logoPickerState.logoBase64}" alt="">`
      : '';

    container.innerHTML = `
      <div class="profile-logo-picker">
        <div class="profile-logo-tabs">${tabs}</div>
        <div class="profile-logo-grid">${grid || `<span class="profile-logo-empty">${esc(typeof t === 'function' ? t('profiles_no_profiles') : 'No presets')}</span>`}</div>
        <div class="profile-logo-upload-row">
          <label class="profile-logo-upload">${esc('Upload custom')}
            <input type="file" accept="image/png,image/jpeg,image/gif,image/webp" class="profile-logo-file-input" hidden>
          </label>
          <button type="button" class="profile-logo-remove">${esc('Remove logo')}</button>
          ${preview}
        </div>
      </div>`;

    container.querySelectorAll('.profile-logo-tab').forEach(btn => {
      btn.onclick = () => {
        container.dataset.activeCategory = btn.dataset.cat || '';
        renderLogoPicker(container);
      };
    });
    container.querySelectorAll('.profile-logo-preset').forEach(btn => {
      btn.onclick = () => {
        _logoPickerState = { mode: 'preset', presetId: btn.dataset.presetId || '', logoBase64: '' };
        renderLogoPicker(container);
      };
    });
    const fileInput = container.querySelector('.profile-logo-file-input');
    if (fileInput) {
      fileInput.onchange = () => {
        const file = fileInput.files && fileInput.files[0];
        if (!file) return;
        if (file.size > 100 * 1024) {
          if (typeof showToast === 'function') showToast('Logo must be 100KB or smaller');
          return;
        }
        const reader = new FileReader();
        reader.onload = () => {
          _logoPickerState = { mode: 'custom', presetId: '', logoBase64: String(reader.result || '') };
          renderLogoPicker(container);
        };
        reader.readAsDataURL(file);
      };
    }
    const removeBtn = container.querySelector('.profile-logo-remove');
    if (removeBtn) {
      removeBtn.onclick = () => {
        _logoPickerState = { mode: 'remove', presetId: '', logoBase64: '' };
        renderLogoPicker(container);
      };
    }
  }

  function skillsDetailHtml(p) {
    const skills = Array.isArray(p.skills) ? p.skills : [];
    if (!skills.length) return '';
    const rows = skills.map(s => {
      const disabled = s.disabled ? ` <span class="detail-badge">${esc('disabled')}</span>` : '';
      const desc = s.description ? `<div class="profile-skill-desc">${esc(s.description)}</div>` : '';
      return `<div class="profile-skill-row"><div class="profile-skill-name">${esc(s.name || '')}${disabled}</div>${desc}</div>`;
    }).join('');
    return `
      <div class="detail-card" style="margin-top:12px">
        <div class="detail-card-title">Skills</div>
        ${rows}
      </div>`;
  }

  async function loadProfilesPanel() {
    const panel = $('profilesPanel');
    if (!panel) return;
    try {
      const data = await api('/api/profiles');
      _profilesCache = data;
      panel.innerHTML = '';
      const explainer = document.createElement('div');
      explainer.className = 'profile-card profile-help-card';
      explainer.innerHTML = `
      <div class="profile-card-header">
        <div style="min-width:0;flex:1">
          <div class="profile-card-name">Profiles vs workspaces</div>
          <div class="profile-card-meta">Use profiles for how the agent works; use workspaces for what files it works on.</div>
        </div>
      </div>`;
      explainer.onclick = () => typeof _renderProfileConceptHelp === 'function' && _renderProfileConceptHelp(data.active || 'default');
      panel.appendChild(explainer);
      if (!data.profiles || !data.profiles.length) {
        const emptyMsg = document.createElement('div');
        emptyMsg.style.cssText = 'padding:16px;color:var(--muted);font-size:12px';
        emptyMsg.textContent = typeof t === 'function' ? t('profiles_no_profiles') : 'No profiles';
        panel.appendChild(emptyMsg);
        if (typeof _profileMode !== 'undefined' && _profileMode !== 'create' && typeof _clearProfileDetail === 'function') _clearProfileDetail();
        return;
      }
      const activeName = (S.activeProfile && data.profiles.some(p => p.name === S.activeProfile))
        ? S.activeProfile
        : (data.active || 'default');
      for (const p of data.profiles) {
        const card = document.createElement('div');
        card.className = 'profile-card';
        card.dataset.name = p.name;
        const { title, sub } = profileTitle(p);
        const desc = profileDescription(p);
        const meta = [];
        if (p.model) meta.push(p.model.split('/').pop());
        if (p.provider) meta.push(p.provider);
        if (p.skill_count) meta.push(typeof t === 'function' ? t('profile_skill_count', p.skill_count) : `${p.skill_count} skills`);
        const gwDot = p.gateway_running
          ? `<span class="profile-opt-badge running"></span>`
          : `<span class="profile-opt-badge stopped"></span>`;
        const isActive = p.name === activeName;
        const activeBadge = isActive ? `<span style="color:var(--link);font-size:10px;font-weight:600;margin-left:6px">${esc(typeof t === 'function' ? t('profile_active') : 'Active')}</span>` : '';
        const defaultBadge = p.is_default ? ` <span style="opacity:.5">${esc(typeof t === 'function' ? t('profile_default_label') : 'default')}</span>` : '';
        const subLine = sub ? `<div class="profile-card-meta" style="opacity:.65">${esc(sub)}</div>` : '';
        card.innerHTML = `
        <div class="profile-card-header">
          ${logoImg(p, 'profile-card-logo')}
          <div style="min-width:0;flex:1">
            <div class="profile-card-name${isActive ? ' is-active' : ''}">${gwDot}${esc(title)}${defaultBadge}${activeBadge}</div>
            ${subLine}
            ${meta.length ? `<div class="profile-card-meta">${esc(meta.join(' · '))}</div>` : `<div class="profile-card-meta">${esc(typeof t === 'function' ? t('profile_no_configuration') : '')}</div>`}
            ${desc ? `<div class="profile-card-meta" style="margin-top:4px">${esc(desc)}</div>` : ''}
          </div>
        </div>`;
        card.onclick = () => typeof openProfileDetail === 'function' && openProfileDetail(p.name, card);
        if (typeof _currentProfileDetail !== 'undefined' && _currentProfileDetail && _currentProfileDetail.name === p.name) card.classList.add('active');
        panel.appendChild(card);
      }
      if (typeof _currentProfileDetail !== 'undefined' && _currentProfileDetail && typeof _profileMode !== 'undefined' && _profileMode !== 'create') {
        const refreshed = data.profiles.find(p => p.name === _currentProfileDetail.name);
        if (refreshed && typeof _renderProfileDetail === 'function') _renderProfileDetail(refreshed, data.active);
        else if (typeof _clearProfileDetail === 'function') _clearProfileDetail();
      }
    } catch (e) {
      panel.innerHTML = `<div style="color:var(--accent);font-size:12px;padding:12px">${esc(typeof t === 'function' ? t('error_prefix') : 'Error: ')}${esc(e.message)}</div>`;
    }
  }

  function renderProfileDropdown(data) {
    const dd = $('profileDropdown');
    if (!dd) return;
    dd.innerHTML = '';
    const profiles = data.profiles || [];
    const active = (S.activeProfile && profiles.some(p => p.name === S.activeProfile))
      ? S.activeProfile
      : (data.active || 'default');
    for (const p of profiles) {
      const opt = document.createElement('div');
      opt.className = 'profile-opt' + (p.name === active ? ' active' : '');
      const { title, sub } = profileTitle(p);
      const meta = [];
      if (p.model) meta.push(p.model.split('/').pop());
      if (p.skill_count) meta.push(typeof t === 'function' ? t('profile_skill_count', p.skill_count) : String(p.skill_count));
      const gwDot = `<span class="profile-opt-badge ${p.gateway_running ? 'running' : 'stopped'}"></span>`;
      const checkmark = p.name === active ? ' <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--link)" stroke-width="3" style="vertical-align:-1px"><polyline points="20 6 9 17 4 12"/></svg>' : '';
      const defaultBadge = p.is_default ? ` <span style="opacity:.5;font-weight:400">${esc(typeof t === 'function' ? t('profile_default_label') : 'default')}</span>` : '';
      const subHtml = sub ? `<div class="profile-opt-meta" style="opacity:.65">${esc(sub)}</div>` : '';
      opt.innerHTML = `<div class="profile-opt-name">${logoImg(p, 'profile-opt-logo')}${gwDot}${esc(title)}${defaultBadge}${checkmark}</div>` +
        subHtml +
        (meta.length ? `<div class="profile-opt-meta">${esc(meta.join(' · '))}</div>` : '');
      opt.onclick = async () => {
        if (typeof closeProfileDropdown === 'function') closeProfileDropdown();
        if (p.name === active) return;
        await switchToProfile(p.name);
      };
      dd.appendChild(opt);
    }
    const div = document.createElement('div');
    div.className = 'ws-divider';
    dd.appendChild(div);
    const mgmt = document.createElement('div');
    mgmt.className = 'profile-opt ws-manage';
    mgmt.innerHTML = `${typeof li === 'function' ? li('settings', 12) : ''} ${esc(typeof t === 'function' ? t('manage_profiles') : 'Manage profiles')}`;
    mgmt.onclick = () => {
      if (typeof closeProfileDropdown === 'function') closeProfileDropdown();
      if (typeof mobileSwitchPanel === 'function') mobileSwitchPanel('profiles');
    };
    dd.appendChild(mgmt);
  }

  function toggleProfileDropdown() {
    const dd = $('profileDropdown');
    if (!dd) return;
    if (dd.classList.contains('open')) {
      if (typeof closeProfileDropdown === 'function') closeProfileDropdown();
      return;
    }
    if (typeof closeWsDropdown === 'function') closeWsDropdown();
    if (typeof closeModelDropdown === 'function') closeModelDropdown();
    api('/api/profiles')
      .then(data => {
        renderProfileDropdown(data);
        dd.classList.add('open');
        if (typeof _positionProfileDropdown === 'function') _positionProfileDropdown();
        const chip = $('profileChip');
        if (chip) chip.classList.add('active');
      })
      .catch(e => {
        if (typeof showToast === 'function') showToast(typeof t === 'function' ? t('profiles_load_failed') : e.message);
      });
  }

  function renderProfileDetail(p, activeName) {
    _currentProfileDetail = p;
    const titleEl = $('profileDetailTitle');
    const body = $('profileDetailBody');
    const empty = $('profileDetailEmpty');
    if (!titleEl || !body) return;
    const { title, sub } = profileTitle(p);
    titleEl.textContent = title;
    const isActive = p.name === activeName;
    const isDefault = !!p.is_default;
    const statusBadge = isActive
      ? `<span class="detail-badge active">${esc(typeof t === 'function' ? t('profile_active') : 'Active')}</span>`
      : `<span class="detail-badge">Inactive</span>`;
    const defaultBadge = isDefault ? ` <span class="detail-badge">${esc(typeof t === 'function' ? t('profile_default_label') : 'default')}</span>` : '';
    const gwBadge = p.gateway_running
      ? `<span class="detail-badge ok">${esc(typeof t === 'function' ? t('profile_gateway_running') : 'Gateway running')}</span>`
      : `<span class="detail-badge">${esc(typeof t === 'function' ? t('profile_gateway_stopped') : 'Gateway stopped')}</span>`;
    const rows = [];
    rows.push(`<div class="detail-row"><div class="detail-row-label">Status</div><div class="detail-row-value">${statusBadge}${defaultBadge}</div></div>`);
    rows.push(`<div class="detail-row"><div class="detail-row-label">Gateway</div><div class="detail-row-value">${gwBadge}</div></div>`);
    if (p.model) rows.push(`<div class="detail-row"><div class="detail-row-label">Model</div><div class="detail-row-value"><code>${esc(p.model)}</code></div></div>`);
    if (p.provider) rows.push(`<div class="detail-row"><div class="detail-row-label">Provider</div><div class="detail-row-value">${esc(p.provider)}</div></div>`);
    if (p.base_url) rows.push(`<div class="detail-row"><div class="detail-row-label">Base URL</div><div class="detail-row-value"><code>${esc(p.base_url)}</code></div></div>`);
    rows.push(`<div class="detail-row"><div class="detail-row-label">API key</div><div class="detail-row-value">${p.has_env ? esc(typeof t === 'function' ? t('profile_api_keys_configured') : 'Configured') : '<span style="color:var(--muted)">Not configured</span>'}</div></div>`);
    if (typeof p.skill_count === 'number') rows.push(`<div class="detail-row"><div class="detail-row-label">Skills</div><div class="detail-row-value">${esc(typeof t === 'function' ? t('profile_skill_count', p.skill_count) : String(p.skill_count))}</div></div>`);
    if (p.default_workspace) rows.push(`<div class="detail-row"><div class="detail-row-label">Default space</div><div class="detail-row-value"><code>${esc(p.default_workspace)}</code></div></div>`);
    const headerLogo = logoImg(p, 'profile-detail-logo');
    const headerBlock = (headerLogo || sub)
      ? `<div class="profile-detail-header">${headerLogo}<div class="profile-detail-header-text">${sub ? `<div class="profile-detail-subtitle">${esc(sub)}</div>` : ''}</div></div>`
      : '';
    const desc = profileDescription(p);
    const descBlock = desc ? `<div class="profile-detail-description">${esc(desc)}</div>` : '';
    body.innerHTML = `
    <div class="main-view-content">
      <div class="detail-card">
        <div class="detail-card-title" style="display:flex;align-items:center;justify-content:space-between;gap:8px">
          <span>Profile</span>
          <button type="button" class="btn btn-sm profile-edit-btn" id="btnEditProfileInfo">Edit</button>
        </div>
        ${headerBlock}
        ${descBlock}
        ${rows.join('')}
      </div>
      ${skillsDetailHtml(p)}
    </div>`;
    body.style.display = '';
    if (empty) empty.style.display = 'none';
    _profileMode = 'read';
    if (typeof _setProfileHeaderButtons === 'function') _setProfileHeaderButtons('read', p, activeName);
    const editBtn = $('btnEditProfileInfo');
    if (editBtn) editBtn.onclick = () => openProfileEdit(p);
  }

  function openProfileEdit(p) {
    _currentProfileDetail = p;
    _profileMode = 'edit';
    const titleEl = $('profileDetailTitle');
    const body = $('profileDetailBody');
    const empty = $('profileDetailEmpty');
    if (!titleEl || !body) return;
    titleEl.textContent = (typeof t === 'function' ? t('edit_title') : 'Edit') + ': ' + p.name;
    const info = profileInfo(p);
    resetLogoPickerState(info);
    body.innerHTML = `
    <div class="main-view-content">
      <form class="detail-form" id="profileInfoEditForm" onsubmit="event.preventDefault();">
        <div class="detail-form-row">
          <label for="profileInfoDisplayName">Display name</label>
          <input type="text" id="profileInfoDisplayName" value="${esc(info.display_name || '')}" autocomplete="off">
        </div>
        <div class="detail-form-row">
          <label for="profileInfoDescription">Description</label>
          <textarea id="profileInfoDescription" rows="3">${esc(info.description || '')}</textarea>
        </div>
        <div class="detail-form-row">
          <label>Logo</label>
          <div id="profileLogoPicker"></div>
        </div>
        <div id="profileInfoError" class="detail-form-error" style="display:none"></div>
        <div style="display:flex;gap:8px;margin-top:12px">
          <button type="button" class="btn btn-primary" id="btnSaveProfileInfo">Save</button>
          <button type="button" class="btn" id="btnCancelProfileInfo">Cancel</button>
        </div>
      </form>
    </div>`;
    body.style.display = '';
    if (empty) empty.style.display = 'none';
    if (typeof _setProfileHeaderButtons === 'function') _setProfileHeaderButtons('empty');
    renderLogoPicker($('profileLogoPicker'));
    $('btnSaveProfileInfo').onclick = () => saveProfileEdit(p.name);
    $('btnCancelProfileInfo').onclick = () => {
      const activeName = _profilesCache ? _profilesCache.active : null;
      renderProfileDetail(p, activeName);
    };
  }

  async function saveProfileEdit(name) {
    const errEl = $('profileInfoError');
    const displayEl = $('profileInfoDisplayName');
    const descEl = $('profileInfoDescription');
    if (!errEl) return;
    errEl.style.display = 'none';
    try {
      const payload = {
        name,
        display_name: displayEl ? (displayEl.value || '') : '',
        description: descEl ? (descEl.value || '') : '',
        ...logoPickerPayload(),
      };
      await api('/api/profile/info', { method: 'POST', body: JSON.stringify(payload) });
      await loadProfilesPanel();
      if (typeof openProfileDetail === 'function') openProfileDetail(name);
      if (typeof showToast === 'function') showToast('Profile info saved');
    } catch (e) {
      errEl.textContent = e.message || 'Save failed';
      errEl.style.display = '';
    }
  }

  async function renderProfileForm() {
    const title = $('profileDetailTitle');
    const body = $('profileDetailBody');
    const empty = $('profileDetailEmpty');
    if (!title || !body) return;
    title.textContent = typeof t === 'function' ? t('new_profile') : 'New profile';
    resetLogoPickerState(null);
    body.innerHTML = `
    <div class="main-view-content">
      <form class="detail-form" onsubmit="event.preventDefault(); saveProfileForm();">
        <div class="detail-form-row">
          <label for="profileFormName">${esc(typeof t === 'function' ? t('profile_name_label') : 'Name')}</label>
          <input type="text" id="profileFormName" placeholder="${esc(typeof t === 'function' ? t('profile_name_placeholder') : 'lowercase, a-z 0-9 hyphens')}" autocomplete="off" autocapitalize="none" autocorrect="off" spellcheck="false" required>
          <div class="detail-form-hint">${esc(typeof t === 'function' ? t('profile_name_rule') : 'Lowercase letters, numbers, hyphens, underscores only.')}</div>
        </div>
        <div class="detail-form-row">
          <label class="detail-form-check" for="profileFormClone">
            <input type="checkbox" id="profileFormClone"> <span>${esc(typeof t === 'function' ? t('profile_clone_label') : 'Clone config from active profile')}</span>
          </label>
        </div>
        <div class="detail-form-row">
          <label for="profileFormModel">${esc(typeof t === 'function' ? t('profile_model_label') : 'Model / provider')}</label>
          <select id="profileFormModel"></select>
        </div>
        <div class="detail-form-row">
          <label for="profileFormBaseUrl">${esc(typeof t === 'function' ? t('profile_base_url_label') : 'Base URL')}</label>
          <input type="text" id="profileFormBaseUrl" placeholder="${esc(typeof t === 'function' ? t('profile_base_url_placeholder') : 'Optional')}" autocomplete="off">
        </div>
        <div class="detail-form-row">
          <label for="profileFormApiKey">${esc(typeof t === 'function' ? t('profile_api_key_label') : 'API key')}</label>
          <input type="password" id="profileFormApiKey" placeholder="${esc(typeof t === 'function' ? t('profile_api_key_placeholder') : 'Optional')}" autocomplete="off">
        </div>
        <div class="detail-form-row">
          <label for="profileFormDisplayName">Display name</label>
          <input type="text" id="profileFormDisplayName" autocomplete="off">
        </div>
        <div class="detail-form-row">
          <label for="profileFormDescription">Description</label>
          <textarea id="profileFormDescription" rows="2"></textarea>
        </div>
        <div class="detail-form-row">
          <label>Logo</label>
          <div id="profileLogoPicker"></div>
        </div>
        <div id="profileFormError" class="detail-form-error" style="display:none"></div>
      </form>
    </div>`;
    body.style.display = '';
    if (empty) empty.style.display = 'none';
    if (typeof _setProfileHeaderButtons === 'function') _setProfileHeaderButtons('create');
    const n = $('profileFormName');
    if (n) n.focus();
    if (typeof _populateProfileFormModelSelect === 'function') _populateProfileFormModelSelect();
    renderLogoPicker($('profileLogoPicker'));
  }

  async function saveProfileForm() {
    const nameEl = $('profileFormName');
    const cloneEl = $('profileFormClone');
    const modelEl = $('profileFormModel');
    const baseEl = $('profileFormBaseUrl');
    const apiKeyEl = $('profileFormApiKey');
    const displayEl = $('profileFormDisplayName');
    const descEl = $('profileFormDescription');
    const errEl = $('profileFormError');
    if (!nameEl || !errEl) return;
    const name = (nameEl.value || '').trim().toLowerCase();
    const cloneConfig = !!(cloneEl && cloneEl.checked);
    errEl.style.display = 'none';
    if (!name) { errEl.textContent = typeof t === 'function' ? t('name_required') : 'Name required'; errEl.style.display = ''; return; }
    if (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(name)) { errEl.textContent = typeof t === 'function' ? t('profile_name_rule') : 'Invalid name'; errEl.style.display = ''; return; }
    const baseUrl = (baseEl ? (baseEl.value || '') : '').trim();
    const apiKey = (apiKeyEl ? (apiKeyEl.value || '') : '').trim();
    if (baseUrl && !/^https?:\/\//.test(baseUrl)) { errEl.textContent = typeof t === 'function' ? t('profile_base_url_rule') : 'Invalid URL'; errEl.style.display = ''; return; }
    try {
      const payload = { name, clone_config: cloneConfig };
      const selectedModel = modelEl ? (modelEl.value || '').trim() : '';
      if (selectedModel) {
        const modelState = (typeof _modelStateForSelect === 'function')
          ? _modelStateForSelect(modelEl, selectedModel)
          : { model: selectedModel, model_provider: null };
        if (modelState.model) payload.default_model = modelState.model;
        if (modelState.model_provider) payload.model_provider = modelState.model_provider;
      }
      if (baseUrl) payload.base_url = baseUrl;
      if (apiKey) payload.api_key = apiKey;
      await api('/api/profile/create', { method: 'POST', body: JSON.stringify(payload) });

      const infoPayload = { name };
      const dn = displayEl ? (displayEl.value || '').trim() : '';
      const ds = descEl ? (descEl.value || '').trim() : '';
      if (dn) infoPayload.display_name = dn;
      if (ds) infoPayload.description = ds;
      Object.assign(infoPayload, logoPickerPayload());
      if (Object.keys(infoPayload).length > 1) {
        await api('/api/profile/info', { method: 'POST', body: JSON.stringify(infoPayload) });
      }

      if (typeof _invalidateKanbanProfileCache === 'function') _invalidateKanbanProfileCache();
      _profilePreFormDetail = null;
      await loadProfilesPanel();
      if (typeof showToast === 'function') showToast(typeof t === 'function' ? t('profile_created', name) : `Created ${name}`);
      if (typeof openProfileDetail === 'function') openProfileDetail(name);
    } catch (e) {
      errEl.textContent = e.message || (typeof t === 'function' ? t('create_failed') : 'Create failed');
      errEl.style.display = '';
    }
  }

  window.HermesProfiles = {
    loadProfilesPanel,
    renderProfileDropdown,
    toggleProfileDropdown,
    renderProfileDetail,
    renderProfileForm,
    saveProfileForm,
  };
})();
