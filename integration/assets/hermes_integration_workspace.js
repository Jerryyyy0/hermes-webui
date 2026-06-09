(function () {
  'use strict';

  const cfg = window.__HERMES_CONFIG__ || {};
  if (!cfg.integrationWorkspaceFiles) return;

  const PAGE_SIZE = 500;
  const API_PREFIX = '/api/integration/workspace';
  const DEBOUNCE_MS = 300;

  let _index = [];
  let _workspace = '';
  let _page = 0;
  let _hasMore = true;
  let _loading = false;
  let _selectedPath = '';
  let _filter = '';
  let _typeFilter = '';
  let _sort = 'path';
  let _order = 'desc';
  let _previewPath = '';
  let _previewMode = '';
  let _reloadTimer = null;

  function $(id) {
    return document.getElementById(id);
  }

  function esc(s) {
    return typeof escHtml === 'function' ? escHtml(s) : String(s ?? '');
  }

  function fileExt(p) {
    const i = p.lastIndexOf('.');
    return i >= 0 ? p.slice(i).toLowerCase() : '';
  }

  function basename(p) {
    const parts = String(p || '').split('/');
    return parts[parts.length - 1] || p;
  }

  function formatSize(bytes) {
    const n = Number(bytes);
    if (!Number.isFinite(n) || n < 0) return '';
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  }

  function typeLabel(row) {
    const ext = String(row.ext || fileExt(row.path || '')).trim();
    return ext || '—';
  }

  function listQueryParams(page, pageSize) {
    const params = new URLSearchParams();
    params.set('page', String(page));
    params.set('page_size', String(pageSize));
    if (_filter.trim()) params.set('q', _filter.trim());
    if (_typeFilter) params.set('type', _typeFilter);
    params.set('sort', _sort || 'path');
    params.set('order', _order || 'desc');
    return params.toString();
  }

  const HTML_PREVIEW_SANDBOX = 'sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox';

  const API = {
    list(page, pageSize) {
      return api(`${API_PREFIX}/files?${listQueryParams(page, pageSize)}`);
    },
    fileUrl(path) {
      return `${API_PREFIX}/file?path=${encodeURIComponent(path)}`;
    },
    async fetchBlob(path) {
      const res = await fetch(API.fileUrl(path), { credentials: 'same-origin' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.blob();
    },
    async fetchText(path) {
      const blob = await API.fetchBlob(path);
      return blob.text();
    },
  };

  function showNav() {
    ['integrationWorkspaceRailBtn', 'integrationWorkspaceSidebarBtn'].forEach(id => {
      const el = $(id);
      if (!el) return;
      el.hidden = false;
      el.classList.remove('nav-tab-hidden');
    });
  }

  function scheduleReload() {
    if (_reloadTimer) clearTimeout(_reloadTimer);
    _reloadTimer = setTimeout(() => {
      _reloadTimer = null;
      loadIndex(true);
    }, DEBOUNCE_MS);
  }

  function renderList() {
    const list = $('integrationWorkspaceFileList');
    const loadMore = $('integrationWorkspaceLoadMore');
    if (!list) return;

    const rows = _index;
    if (_loading && !_index.length) {
      list.innerHTML = '<div style="padding:12px;color:var(--muted);font-size:12px">Loading...</div>';
      if (loadMore) loadMore.hidden = true;
      return;
    }

    if (!rows.length) {
      list.innerHTML = '<div style="padding:12px;color:var(--muted);font-size:12px">No files</div>';
    } else {
      list.innerHTML = rows.map(row => {
        const path = row.path || '';
        const name = basename(path);
        const selected = path === _selectedPath ? ' selected' : '';
        const size = formatSize(row.size);
        const typeText = typeLabel(row);
        return `<div class="integration-ws-file-item${selected}" role="button" tabindex="0" data-path="${esc(path)}">`
          + `<div class="integration-ws-file-row">`
          + `<span class="integration-ws-file-name">${esc(name)}</span>`
          + `<span class="integration-ws-file-type">${esc(typeText)}</span>`
          + `</div>`
          + `<span class="integration-ws-file-path">${esc(path)}</span>`
          + (size ? `<span class="integration-ws-file-meta">${esc(size)}</span>` : '')
          + '</div>';
      }).join('');
      list.querySelectorAll('.integration-ws-file-item').forEach(row => {
        const activate = () => {
          const path = row.getAttribute('data-path') || '';
          if (path) selectFile(path);
        };
        row.addEventListener('click', activate);
        row.addEventListener('keydown', (e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            activate();
          }
        });
      });
    }

    if (loadMore) {
      loadMore.hidden = !_hasMore;
      loadMore.disabled = _loading;
      loadMore.textContent = _loading ? 'Loading...' : 'Load more';
    }
  }

  function updateRootHint() {
    const hint = $('integrationWorkspaceRootHint');
    if (hint) hint.textContent = _workspace || '';
  }

  async function loadIndex(reset) {
    if (_loading) return;
    _loading = true;
    if (reset) {
      _index = [];
      _page = 0;
      _hasMore = true;
      _workspace = '';
    }
    renderList();

    try {
      const nextPage = _page + 1;
      const data = await API.list(nextPage, PAGE_SIZE);
      if (data.workspace) _workspace = data.workspace;
      const files = Array.isArray(data.files) ? data.files : [];
      _index = reset ? files : _index.concat(files);
      _page = nextPage;
      if (typeof data.has_more === 'boolean') {
        _hasMore = data.has_more;
      } else {
        const pageSize = Number(data.page_size) || PAGE_SIZE;
        _hasMore = files.length >= pageSize;
      }
      updateRootHint();
    } catch (e) {
      if (typeof setStatus === 'function') setStatus('Workspace index failed: ' + (e.message || e));
    } finally {
      _loading = false;
      renderList();
    }
  }

  async function loadMorePage() {
    if (_loading || !_hasMore) return;
    _loading = true;
    renderList();
    try {
      const nextPage = _page + 1;
      const data = await API.list(nextPage, PAGE_SIZE);
      if (!_workspace && data.workspace) _workspace = data.workspace;
      const files = Array.isArray(data.files) ? data.files : [];
      _index = _index.concat(files);
      _page = nextPage;
      if (typeof data.has_more === 'boolean') {
        _hasMore = data.has_more;
      } else {
        const pageSize = Number(data.page_size) || PAGE_SIZE;
        _hasMore = files.length >= pageSize;
      }
      updateRootHint();
    } catch (e) {
      if (typeof setStatus === 'function') setStatus('Load more failed: ' + (e.message || e));
    } finally {
      _loading = false;
      renderList();
    }
  }

  function showIwsPreview(mode) {
    const code = $('iwsPreviewCode');
    const imgWrap = $('iwsPreviewImgWrap');
    const mediaWrap = $('iwsPreviewMediaWrap');
    const pdfWrap = $('iwsPreviewPdfWrap');
    const md = $('iwsPreviewMd');
    const htmlWrap = $('iwsPreviewHtmlWrap');
    if (code) code.style.display = mode === 'code' ? '' : 'none';
    if (imgWrap) imgWrap.style.display = mode === 'image' ? '' : 'none';
    if (mediaWrap) mediaWrap.style.display = (mode === 'audio' || mode === 'video') ? '' : 'none';
    if (pdfWrap) pdfWrap.style.display = mode === 'pdf' ? '' : 'none';
    if (md) md.style.display = mode === 'md' ? '' : 'none';
    if (htmlWrap) htmlWrap.style.display = mode === 'html' ? '' : 'none';
    const badge = $('iwsPreviewBadge');
    if (badge) {
      badge.className = 'preview-badge ' + mode;
      const pathText = ($('iwsPreviewPathText') || {}).textContent || '';
      badge.textContent = mode === 'image' ? 'image'
        : mode === 'audio' ? 'audio'
        : mode === 'video' ? 'video'
        : mode === 'pdf' ? 'pdf'
        : mode === 'md' ? 'md'
        : mode === 'html' ? 'html'
        : fileExt(pathText) || 'text';
    }
    _previewMode = mode;
    const openBtn = $('iwsBtnOpenInBrowser');
    if (openBtn) openBtn.style.display = (mode === 'html' || mode === 'pdf') ? 'inline-flex' : 'none';
  }

  async function downloadIntegrationFile(path) {
    const blob = await API.fetchBlob(path);
    const filename = basename(path);
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => {
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    }, 100);
    if (typeof showToast === 'function') showToast('Downloading ' + filename, 2000);
  }

  function openInBrowserIntegration() {
    if (!_previewPath) return;
    window.open(API.fileUrl(_previewPath), '_blank');
  }

  function showHtmlPreview(iframe, html) {
    if (!iframe) return;
    iframe.removeAttribute('src');
    iframe.srcdoc = html || '';
    iframe.setAttribute('sandbox', HTML_PREVIEW_SANDBOX);
  }

  async function openIntegrationFile(path) {
    const ext = fileExt(path);
    const downloadExts = typeof DOWNLOAD_EXTS !== 'undefined' ? DOWNLOAD_EXTS : new Set();

    if (downloadExts.has(ext)) {
      try {
        await downloadIntegrationFile(path);
      } catch (_) {
        if (typeof setStatus === 'function') setStatus('File open failed');
      }
      return;
    }

    const empty = $('integrationWorkspacePreviewEmpty');
    const area = $('iwsPreviewArea');
    if ($('iwsPreviewPathText')) $('iwsPreviewPathText').textContent = path;
    if (empty) empty.hidden = true;
    if (area) area.classList.add('visible');
    _previewPath = path;

    const imageExts = typeof IMAGE_EXTS !== 'undefined' ? IMAGE_EXTS : new Set(['.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp']);
    const mdExts = typeof MD_EXTS !== 'undefined' ? MD_EXTS : new Set(['.md', '.markdown', '.mdown']);
    const htmlExts = typeof HTML_EXTS !== 'undefined' ? HTML_EXTS : new Set(['.html', '.htm']);
    const pdfExts = typeof PDF_EXTS !== 'undefined' ? PDF_EXTS : new Set(['.pdf']);
    const audioExts = typeof AUDIO_EXTS !== 'undefined' ? AUDIO_EXTS : new Set(['.mp3', '.wav', '.ogg']);
    const videoExts = typeof VIDEO_EXTS !== 'undefined' ? VIDEO_EXTS : new Set(['.mp4', '.webm']);

    const fileUrl = API.fileUrl(path);
    if (imageExts.has(ext)) {
      showIwsPreview('image');
      const img = $('iwsPreviewImg');
      if (img) {
        img.alt = path;
        img.src = fileUrl;
        img.onerror = () => { if (typeof setStatus === 'function') setStatus('Image load failed'); };
      }
    } else if (audioExts.has(ext) || videoExts.has(ext)) {
      const mode = videoExts.has(ext) ? 'video' : 'audio';
      showIwsPreview(mode);
      const wrap = $('iwsPreviewMediaWrap');
      if (wrap) {
        wrap.innerHTML = (typeof _mediaPlayerHtml === 'function')
          ? _mediaPlayerHtml(mode, fileUrl, basename(path))
          : `<${mode} src="${fileUrl.replace(/"/g, '%22')}" controls preload="metadata"></${mode}>`;
        if (typeof _applyMediaPlaybackPreferences === 'function') _applyMediaPlaybackPreferences(wrap);
      }
    } else if (pdfExts.has(ext)) {
      showIwsPreview('pdf');
      const frame = $('iwsPreviewPdfFrame');
      if (frame) {
        frame.src = '';
        frame.src = fileUrl;
        frame.title = 'PDF preview: ' + basename(path);
      }
    } else if (mdExts.has(ext)) {
      try {
        const content = await API.fetchText(path);
        if (typeof shouldRenderMarkdownPreviewAsPlainText === 'function'
          && shouldRenderMarkdownPreviewAsPlainText(content)) {
          showIwsPreview('code');
          const code = $('iwsPreviewCode');
          if (code) code.textContent = content;
          if (typeof largeMarkdownPlainTextStatus === 'function' && typeof setStatus === 'function') {
            setStatus(largeMarkdownPlainTextStatus(content));
          }
          return;
        }
        showIwsPreview('md');
        const mdEl = $('iwsPreviewMd');
        if (mdEl && typeof renderMd === 'function') {
          mdEl.innerHTML = renderMd(content);
          requestAnimationFrame(() => {
            if (typeof renderKatexBlocks === 'function') renderKatexBlocks();
          });
        }
      } catch (e) {
        if (typeof setStatus === 'function') setStatus('File open failed');
      }
    } else if (htmlExts.has(ext)) {
      try {
        const content = await API.fetchText(path);
        showIwsPreview('html');
        showHtmlPreview($('iwsPreviewHtmlIframe'), content);
      } catch (e) {
        if (typeof setStatus === 'function') setStatus('File open failed');
      }
    } else {
      try {
        const content = await API.fetchText(path);
        showIwsPreview('code');
        const code = $('iwsPreviewCode');
        if (code) code.textContent = content;
      } catch (_) {
        try {
          await downloadIntegrationFile(path);
        } catch (e) {
          if (typeof setStatus === 'function') setStatus('File open failed');
        }
      }
    }
  }

  function clearIwsPreview() {
    _previewPath = '';
    _previewMode = '';
    const empty = $('integrationWorkspacePreviewEmpty');
    const area = $('iwsPreviewArea');
    if (empty) empty.hidden = false;
    if (area) area.classList.remove('visible');
  }

  function selectFile(path, options) {
    const preview = !options || options.preview !== false;
    _selectedPath = path;
    try {
      localStorage.setItem('hermes-iws-selected-path', path);
    } catch (_) {}
    renderList();
    if (preview) openIntegrationFile(path);
  }

  function bindUi() {
    const refresh = $('integrationWorkspaceRefreshBtn');
    if (refresh) refresh.addEventListener('click', () => loadIndex(true));

    const search = $('integrationWorkspaceSearch');
    if (search) {
      search.addEventListener('input', () => {
        _filter = search.value || '';
        scheduleReload();
      });
    }

    const typeFilter = $('integrationWorkspaceTypeFilter');
    if (typeFilter) {
      typeFilter.addEventListener('change', () => {
        _typeFilter = typeFilter.value || '';
        loadIndex(true);
      });
    }

    const sortSelect = $('integrationWorkspaceSort');
    if (sortSelect) {
      sortSelect.addEventListener('change', () => {
        _sort = sortSelect.value || 'path';
        loadIndex(true);
      });
    }

    const orderSelect = $('integrationWorkspaceOrder');
    if (orderSelect) {
      orderSelect.addEventListener('change', () => {
        _order = orderSelect.value || 'desc';
        loadIndex(true);
      });
    }

    const loadMoreBtn = $('integrationWorkspaceLoadMore');
    if (loadMoreBtn) loadMoreBtn.addEventListener('click', () => loadMorePage());

    const dl = $('iwsBtnDownloadFile');
    if (dl) dl.addEventListener('click', () => {
      if (_previewPath) {
        downloadIntegrationFile(_previewPath).catch(() => {
          if (typeof setStatus === 'function') setStatus('File open failed');
        });
      }
    });

    const openBrowser = $('iwsBtnOpenInBrowser');
    if (openBrowser) openBrowser.addEventListener('click', openInBrowserIntegration);
  }

  async function load() {
    if (!_index.length) await loadIndex(true);
    else renderList();
    clearIwsPreview();
    try {
      const saved = localStorage.getItem('hermes-iws-selected-path');
      if (saved && _index.some(r => r.path === saved)) selectFile(saved, { preview: false });
    } catch (_) {}
  }

  window.HermesIntegrationWorkspace = { load, loadIndex, openIntegrationFile, showNav };
  bindUi();
  showNav();
})();
