/* Zattoo-DL Frontend ----------------------------------------------------- */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const PAGE_SIZE = 15;
const POLL_INTERVAL_MS = 750;

const TARGETS = {
  downie: 'Downie',
  metube: 'Metube',
  vlc: 'VLC',
  local: 'Lokal',
};

const STATE = {
  recordings: [],
  filter: '',
  page: 1,
  view: 'recordings',  // 'recordings' | 'downloads'
  settings: loadSettings(),
};

// recording_id → { jobId } für Polling und Re-Render-Persistenz
const ACTIVE_JOBS = new Map();
let pollTimer = null;

const els = {
  topbarActions: $('#topbarActions'),
  searchBox: $('#searchBox'),
  filterInput: $('#filterInput'),
  refreshBtn: $('#refreshBtn'),
  settingsBtn: $('#settingsBtn'),
  tabs: $('#tabs'),
  dlBadge: $('#dlBadge'),
  recordingsView: $('#recordingsView'),
  downloadsView: $('#downloadsView'),
  downloadsList: $('#downloadsList'),
  downloadsSummary: $('#downloadsSummary'),
  grid: $('#grid'),
  loading: $('#loadingState'),
  empty: $('#emptyState'),
  emptyTitle: $('#emptyTitle'),
  emptyText: $('#emptyText'),
  countLabel: $('#countLabel'),
  filteredLabel: $('#filteredLabel'),
  defaultTargetChip: $('#defaultTargetChip'),
  statsBar: $('#statsBar'),
  pagination: $('#pagination'),

  loginModal: $('#loginModal'),
  loginForm: $('#loginForm'),
  loginUser: $('#loginUser'),
  loginPass: $('#loginPass'),
  loginError: $('#loginError'),
  loginSubmit: $('#loginSubmit'),

  settingsModal: $('#settingsModal'),
  settingsForm: $('#settingsForm'),
  settingsClose: $('#settingsClose'),
  metubeHost: $('#metubeHost'),
  defaultTarget: $('#defaultTarget'),
  bilingualToggle: $('#bilingualToggle'),
  logoutBtn: $('#logoutBtn'),

  toasts: $('#toasts'),
};

/* --- Settings (persisted in localStorage) ------------------------------- */

function loadSettings() {
  let s = {};
  try { s = JSON.parse(localStorage.getItem('zattoo-dl-settings') || '{}'); } catch {}
  return {
    target: TARGETS[s.target] ? s.target : 'downie',
    metubeHost: typeof s.metubeHost === 'string' ? s.metubeHost : '',
    bilingual: !!s.bilingual,
  };
}

function saveSettings() {
  localStorage.setItem('zattoo-dl-settings', JSON.stringify(STATE.settings));
  updateDefaultTargetChip();
}

function updateDefaultTargetChip() {
  els.defaultTargetChip.textContent = TARGETS[STATE.settings.target] || 'Downie';
}

/* --- HTTP helpers -------------------------------------------------------- */

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  let body = null;
  try { body = await res.json(); } catch {}
  return { ok: res.ok, status: res.status, body: body || {} };
}

/* --- Toasts ------------------------------------------------------------- */

function toast(message, kind = 'info', timeout = 3500) {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = message;
  els.toasts.appendChild(el);
  setTimeout(() => {
    el.classList.add('fadeOut');
    setTimeout(() => el.remove(), 250);
  }, timeout);
}

/* --- View switching (Aufnahmen / Downloads) ----------------------------- */

function setView(view) {
  if (view !== 'recordings' && view !== 'downloads') view = 'recordings';
  STATE.view = view;

  els.recordingsView.hidden = view !== 'recordings';
  els.downloadsView.hidden = view !== 'downloads';

  // Topbar-Actions: Suchfeld + Refresh nur für Recordings sinnvoll
  els.searchBox.hidden = view !== 'recordings';
  els.refreshBtn.hidden = view !== 'recordings';

  // Aktive Tab-Markierung
  $$('.tab').forEach(t => {
    t.classList.toggle('active', t.dataset.view === view);
  });

  // Wenn wir auf Downloads wechseln, Liste neu rendern
  if (view === 'downloads') renderDownloadsView();

  // URL-Hash synchron halten
  const wantHash = view === 'downloads' ? '#downloads' : '#recordings';
  if (location.hash !== wantHash) {
    history.replaceState(null, '', wantHash);
  }
}

function updateDownloadsBadge() {
  const n = ACTIVE_JOBS.size;
  els.dlBadge.textContent = String(n);
  els.dlBadge.hidden = n === 0;
}

function renderDownloadsView() {
  const list = els.downloadsList;
  list.innerHTML = '';

  const n = ACTIVE_JOBS.size;
  if (n === 0) {
    els.downloadsSummary.textContent = 'Keine Downloads aktiv.';
    list.innerHTML = `
      <div class="downloads-empty">
        <div class="downloads-empty-icon">
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path fill="currentColor" d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/>
          </svg>
        </div>
        <p>Keine aktiven Downloads.</p>
        <p class="muted">Klick auf einer Karte auf <strong>Lokal</strong>, um eine Aufnahme hier in die Queue zu legen.</p>
      </div>`;
    return;
  }

  els.downloadsSummary.textContent =
    `${n} ${n === 1 ? 'Download' : 'Downloads'} in Bearbeitung`;

  for (const [recId] of ACTIVE_JOBS) {
    const rec = STATE.recordings.find(r => r.id === recId) || null;
    const item = buildDownloadItem(recId, rec);
    list.appendChild(item);
    // Initiale Progress-Anzeige bis das Polling den ersten realen Wert liefert
    const progressEl = item.querySelector('.dl-item-progress');
    renderProgress(progressEl, {
      state: 'queued', percent: 0, speed: '', eta: '',
      message: 'Verbinde…',
    });
  }
}

function buildDownloadItem(recId, rec) {
  const item = document.createElement('div');
  item.className = 'dl-item';
  item.dataset.id = recId;

  // Thumbnail (oder Platzhalter, wenn keine Recording-Daten verfügbar)
  if (rec && rec.thumbnail) {
    const thumb = document.createElement('img');
    thumb.className = 'dl-item-thumb';
    thumb.alt = '';
    thumb.loading = 'lazy';
    thumb.src = `/api/thumbnail?id=${encodeURIComponent(recId)}&url=${encodeURIComponent(rec.thumbnail)}`;
    thumb.addEventListener('error', () => {
      thumb.replaceWith(makeThumbPlaceholder());
    });
    item.appendChild(thumb);
  } else {
    item.appendChild(makeThumbPlaceholder());
  }

  const info = document.createElement('div');
  info.className = 'dl-item-info';

  const title = document.createElement('div');
  title.className = 'dl-item-title';
  title.textContent = rec ? (rec.title || '–') : `Aufnahme ${recId}`;
  info.appendChild(title);

  const ep = document.createElement('div');
  ep.className = 'dl-item-episode';
  ep.textContent = rec && rec.episode ? rec.episode : (rec && rec.cid ? rec.cid : ' ');
  info.appendChild(ep);

  const progress = document.createElement('div');
  progress.className = 'dl-item-progress';
  info.appendChild(progress);

  item.appendChild(info);
  return item;
}

function makeThumbPlaceholder() {
  const ph = document.createElement('div');
  ph.className = 'dl-item-thumb';
  return ph;
}

/* --- Date helpers ------------------------------------------------------- */

function formatDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const dd = String(d.getDate()).padStart(2, '0');
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const yyyy = d.getFullYear();
  const hh = String(d.getHours()).padStart(2, '0');
  const mi = String(d.getMinutes()).padStart(2, '0');
  return `${dd}.${mm}.${yyyy} · ${hh}:${mi} Uhr`;
}

function safeFilename(rec) {
  const date = new Date(rec.start);
  let prefix = '';
  if (!Number.isNaN(date.getTime())) {
    const yyyy = date.getFullYear();
    const mm = String(date.getMonth() + 1).padStart(2, '0');
    const dd = String(date.getDate()).padStart(2, '0');
    const hh = String(date.getHours()).padStart(2, '0');
    const mi = String(date.getMinutes()).padStart(2, '0');
    prefix = `${yyyy}-${mm}-${dd} ${hh}h${mi} `;
  }
  let title = (rec.title || '').replace(/[\\/:*?"<>|]/g, ' ').trim();
  let episode = (rec.episode || '').replace(/[\\/:*?"<>|]/g, ' ').trim();
  let name = `${prefix}${title}`.trim();
  if (episode) name += ` - ${episode}`;
  return name;
}

/* --- Card rendering ----------------------------------------------------- */

function getFilteredList() {
  const f = STATE.filter.trim().toLowerCase();
  if (!f) return STATE.recordings;
  return STATE.recordings.filter(r =>
    (r.title || '').toLowerCase().includes(f) ||
    (r.episode || '').toLowerCase().includes(f));
}

function render() {
  const list = getFilteredList();
  const totalPages = Math.max(1, Math.ceil(list.length / PAGE_SIZE));
  if (STATE.page > totalPages) STATE.page = totalPages;
  if (STATE.page < 1) STATE.page = 1;

  const start = (STATE.page - 1) * PAGE_SIZE;
  const pageItems = list.slice(start, start + PAGE_SIZE);

  els.countLabel.textContent =
    `${STATE.recordings.length} Aufnahme${STATE.recordings.length === 1 ? '' : 'n'}`;

  if (STATE.recordings.length === 0) {
    els.filteredLabel.textContent = '';
  } else if (list.length === STATE.recordings.length) {
    els.filteredLabel.textContent = `Seite ${STATE.page} von ${totalPages}`;
  } else {
    els.filteredLabel.textContent =
      `${list.length} gefiltert · Seite ${STATE.page} von ${totalPages}`;
  }

  els.grid.innerHTML = '';

  if (STATE.recordings.length === 0) {
    els.empty.hidden = false;
    els.emptyTitle.textContent = 'Keine Aufnahmen gefunden';
    els.emptyText.textContent = 'Sobald du bei Zattoo Aufnahmen hast, erscheinen sie hier.';
    renderPagination(0, 0);
    return;
  }
  if (list.length === 0) {
    els.empty.hidden = false;
    els.emptyTitle.textContent = 'Kein Treffer';
    els.emptyText.textContent = `Filter „${STATE.filter}" liefert keine Aufnahme.`;
    renderPagination(0, 0);
    return;
  }

  els.empty.hidden = true;
  const frag = document.createDocumentFragment();
  for (const rec of pageItems) frag.appendChild(buildCard(rec));
  els.grid.appendChild(frag);

  renderPagination(totalPages, list.length);
}

function pageNumbersForDisplay(current, total) {
  const set = new Set([
    1, total,
    current - 1, current, current + 1,
  ]);
  return [...set]
    .filter(p => p >= 1 && p <= total)
    .sort((a, b) => a - b);
}

function renderPagination(totalPages, totalItems) {
  els.pagination.innerHTML = '';
  if (totalPages <= 1) {
    els.pagination.hidden = true;
    return;
  }
  els.pagination.hidden = false;

  const make = (label, page, opts = {}) => {
    const btn = document.createElement('button');
    btn.className = 'page-btn';
    if (opts.current) btn.classList.add('current');
    btn.type = 'button';
    btn.textContent = label;
    btn.disabled = !!opts.disabled;
    if (opts.aria) btn.setAttribute('aria-label', opts.aria);
    if (page != null && !opts.disabled && !opts.current) {
      btn.addEventListener('click', () => goToPage(page));
    }
    return btn;
  };

  els.pagination.appendChild(
    make('←', STATE.page - 1, {
      disabled: STATE.page <= 1,
      aria: 'Vorherige Seite',
    })
  );

  const pages = pageNumbersForDisplay(STATE.page, totalPages);
  let prev = 0;
  for (const p of pages) {
    if (p - prev > 1) {
      const ell = document.createElement('span');
      ell.className = 'page-ellipsis';
      ell.textContent = '…';
      els.pagination.appendChild(ell);
    }
    els.pagination.appendChild(
      make(String(p), p, { current: p === STATE.page, aria: `Seite ${p}` })
    );
    prev = p;
  }

  els.pagination.appendChild(
    make('→', STATE.page + 1, {
      disabled: STATE.page >= totalPages,
      aria: 'Nächste Seite',
    })
  );
}

function goToPage(page) {
  STATE.page = page;
  render();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function buildCardMeta(rec) {
  const frag = document.createDocumentFragment();

  const buildCidFallback = () => {
    const span = document.createElement('span');
    span.className = 'card-cid-fallback';
    span.textContent = (rec.cid || '?').toUpperCase();
    return span;
  };

  // Logo-Zeile ganz oben
  const logoRow = document.createElement('div');
  logoRow.className = 'card-logo-row';
  if (rec.logo_url) {
    const logo = document.createElement('img');
    logo.className = 'card-logo';
    logo.src = rec.logo_url;
    logo.alt = rec.cid || '';
    logo.loading = 'lazy';
    logo.decoding = 'async';
    logo.addEventListener('error', () => {
      logo.replaceWith(buildCidFallback());
    });
    logoRow.appendChild(logo);
  } else if (rec.cid) {
    logoRow.appendChild(buildCidFallback());
  }
  frag.appendChild(logoRow);

  // Datum-Zeile darunter
  const date = document.createElement('div');
  date.className = 'card-date';
  date.textContent = formatDate(rec.start);
  frag.appendChild(date);

  return frag;
}

function buildCard(rec) {
  const card = document.createElement('article');
  card.className = 'card';
  card.dataset.id = rec.id;

  // --- Thumbnail
  const thumbWrap = document.createElement('div');
  thumbWrap.className = 'thumb';

  if (rec.thumbnail) {
    const img = document.createElement('img');
    img.alt = '';
    img.loading = 'lazy';
    img.decoding = 'async';
    img.src = `/api/thumbnail?id=${encodeURIComponent(rec.id)}&url=${encodeURIComponent(rec.thumbnail)}`;
    img.addEventListener('load', () => img.classList.add('loaded'));
    img.addEventListener('error', () => {
      img.remove();
      addFallback(thumbWrap, rec);
    });
    thumbWrap.appendChild(img);
  } else {
    addFallback(thumbWrap, rec);
  }

  card.appendChild(thumbWrap);

  // --- Body
  const body = document.createElement('div');
  body.className = 'card-body';

  body.appendChild(buildCardMeta(rec));

  const title = document.createElement('div');
  title.className = 'card-title';
  title.textContent = rec.title || '–';
  body.appendChild(title);

  const ep = document.createElement('div');
  ep.className = 'card-episode';
  ep.textContent = rec.episode || ' ';
  body.appendChild(ep);

  const actions = document.createElement('div');
  actions.className = 'card-actions';

  // Wenn für diese Aufnahme ein lokaler Download läuft → Progress statt Button
  if (ACTIVE_JOBS.has(rec.id)) {
    renderProgress(actions, {
      state: 'running', percent: 0, speed: '', eta: '',
      message: 'Lädt…',
    });
  } else {
    actions.appendChild(buildSplitButton(card, rec));
  }

  body.appendChild(actions);

  card.appendChild(body);
  return card;
}

function buildSplitButton(card, rec) {
  const split = document.createElement('div');
  split.className = 'split';

  const main = document.createElement('button');
  main.className = 'btn-primary split-main';
  main.type = 'button';
  main.dataset.action = 'download';
  setMainButtonLabel(main);
  main.addEventListener('click', () => triggerDownload(card, rec, STATE.settings.target));

  const toggle = document.createElement('button');
  toggle.className = 'btn-primary split-toggle';
  toggle.type = 'button';
  toggle.title = 'Anderes Ziel wählen';
  toggle.innerHTML = '<svg viewBox="0 0 24 24"><path fill="currentColor" d="M7 10l5 5 5-5z"/></svg>';
  toggle.addEventListener('click', (e) => {
    e.stopPropagation();
    openMenu(toggle, card, rec);
  });

  split.appendChild(main);
  split.appendChild(toggle);
  return split;
}

function setMainButtonLabel(btn) {
  const label = TARGETS[STATE.settings.target] || 'Downie';
  let icon;
  if (STATE.settings.target === 'vlc') {
    icon = '<svg viewBox="0 0 24 24"><path fill="currentColor" d="M8 5v14l11-7z"/></svg>';
  } else if (STATE.settings.target === 'local') {
    icon = '<svg viewBox="0 0 24 24"><path fill="currentColor" d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg>';
  } else {
    icon = '<svg viewBox="0 0 24 24"><path fill="currentColor" d="M5 20h14v-2H5v2zm7-18l-5.5 5.5L8 9l3-3v8h2V6l3 3 1.5-1.5L12 2z" transform="rotate(180 12 12)"/></svg>';
  }
  btn.innerHTML = `${icon}<span>${label}</span>`;
}

function addFallback(wrap, rec) {
  const fb = document.createElement('div');
  fb.className = 'thumb-fallback';
  fb.textContent = (rec.title || '?').slice(0, 1).toUpperCase();
  wrap.appendChild(fb);
}

/* --- Download menu ------------------------------------------------------ */

let openMenuEl = null;

function closeMenu() {
  if (openMenuEl) {
    openMenuEl.remove();
    openMenuEl = null;
    document.removeEventListener('click', onDocClickClose, true);
  }
}

function onDocClickClose(e) {
  if (openMenuEl && !openMenuEl.contains(e.target)) closeMenu();
}

function openMenu(anchor, card, rec) {
  closeMenu();
  const menu = document.createElement('div');
  menu.className = 'menu';
  const titleEl = document.createElement('div');
  titleEl.className = 'menu-title';
  titleEl.textContent = 'Download via …';
  menu.appendChild(titleEl);

  const targets = [
    { id: 'downie', label: 'Downie' },
    { id: 'metube', label: 'Metube' },
    { id: 'vlc', label: 'VLC' },
    { id: 'local', label: 'Lokal (ffmpeg / yt-dlp)' },
  ];
  for (const t of targets) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = t.label;
    btn.addEventListener('click', () => {
      closeMenu();
      triggerDownload(card, rec, t.id);
    });
    menu.appendChild(btn);
  }

  document.body.appendChild(menu);
  const r = anchor.getBoundingClientRect();
  const mw = menu.offsetWidth;
  let left = r.right - mw;
  if (left < 8) left = 8;
  menu.style.left = `${left}px`;
  menu.style.top = `${r.bottom + window.scrollY + 6}px`;
  openMenuEl = menu;
  setTimeout(() => document.addEventListener('click', onDocClickClose, true), 0);
}

/* --- Trigger one download (lazy URL generation) ------------------------- */

async function triggerDownload(card, rec, target) {
  const main = card.querySelector('.split-main');
  const toggle = card.querySelector('.split-toggle');
  if (!main) return;

  if (target === 'metube' && !STATE.settings.metubeHost) {
    toast('Bitte zuerst in den Einstellungen den Metube-Host eintragen.', 'error', 4500);
    openSettings();
    return;
  }

  // Lokaler Download: Progress-Komponente einsetzen, Polling starten
  if (target === 'local') {
    if (ACTIVE_JOBS.has(rec.id)) {
      toast('Download für diese Aufnahme läuft bereits.', 'info', 2500);
      return;
    }
    await startLocalDownload(card, rec);
    return;
  }

  const original = main.innerHTML;
  main.disabled = true;
  toggle.disabled = true;
  const verb = target === 'vlc' ? 'Stream wird geöffnet' : 'Stream-URL wird geholt';
  main.innerHTML = `<span>${verb}…</span>`;

  const payload = {
    recording_id: rec.id,
    target,
    filename: safeFilename(rec),
    metube_host: STATE.settings.metubeHost,
  };

  try {
    const { ok, status, body } = await api('/api/download', {
      method: 'POST',
      body: JSON.stringify(payload),
    });

    if (!ok) {
      const msg = body.error || `Fehler ${status}`;
      toast(`Aktion fehlgeschlagen: ${msg}`, 'error', 4500);
      return;
    }

    if (target === 'downie') {
      toast(`„${rec.title}" an Downie übergeben.`, 'success');
    } else if (target === 'vlc') {
      const note = body.note ? ` (${body.note})` : '';
      toast(`„${rec.title}" wird in VLC geöffnet.${note}`, 'success', 4500);
    } else {
      toast(body.ok
        ? `„${rec.title}" an Metube übergeben.`
        : `Metube meldet: ${JSON.stringify(body.metube || {})}`,
        body.ok ? 'success' : 'error');
    }
  } catch (e) {
    toast(`Netzwerk-Fehler: ${e.message}`, 'error', 4500);
  } finally {
    main.innerHTML = original;
    main.disabled = false;
    toggle.disabled = false;
  }
}

/* --- Local-Download mit Progress-Polling -------------------------------- */

async function startLocalDownload(card, rec) {
  const actions = card.querySelector('.card-actions');
  if (!actions) return;

  // Kurz „URL wird geholt" anzeigen, bis das Backend einen job_id liefert
  renderProgress(actions, {
    state: 'queued', percent: 0, speed: '', eta: '',
    queue_position: 0, message: 'Stream-URL wird geholt…',
  });

  let jobId;
  try {
    const { ok, status, body } = await api('/api/download', {
      method: 'POST',
      body: JSON.stringify({
        recording_id: rec.id,
        target: 'local',
        filename: safeFilename(rec),
        bilingual: STATE.settings.bilingual,
      }),
    });
    if (!ok) {
      restoreCardActions(card, rec);
      toast(`Download fehlgeschlagen: ${body.error || status}`, 'error', 5000);
      return;
    }
    jobId = body.job_id;
  } catch (e) {
    restoreCardActions(card, rec);
    toast(`Netzwerk-Fehler: ${e.message}`, 'error', 4500);
    return;
  }

  ACTIVE_JOBS.set(rec.id, { jobId });
  updateDownloadsBadge();
  renderProgress(actions, {
    state: 'queued', percent: 0, speed: '', eta: '',
    queue_position: 1, message: 'In Queue…',
  });
  // Falls Downloads-View gerade offen ist: Liste neu aufbauen, damit der
  // neue Eintrag sofort dort erscheint
  if (STATE.view === 'downloads') renderDownloadsView();
  startPolling();
}

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(pollAllJobs, POLL_INTERVAL_MS);
}

function stopPollingIfIdle() {
  if (ACTIVE_JOBS.size === 0 && pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function pollAllJobs() {
  // Snapshot, damit wir während Iteration mutieren dürfen
  const snapshot = Array.from(ACTIVE_JOBS.entries());
  await Promise.all(snapshot.map(([recId, ctx]) => pollOne(recId, ctx.jobId)));
}

async function pollOne(recId, jobId) {
  let body;
  try {
    const r = await api(`/api/download/${jobId}/progress`);
    if (!r.ok) {
      // 404: Job vom Backend vergessen — wir geben auf
      finishJob(recId, jobId, 'error', r.body.error || `HTTP ${r.status}`);
      return;
    }
    body = r.body;
  } catch (e) {
    return; // Netzwerk-Hickup, einfach beim nächsten Tick erneut versuchen
  }

  // Karte auf der Recordings-Seite
  const card = document.querySelector(`.card[data-id="${CSS.escape(recId)}"]`);
  if (card) {
    const actions = card.querySelector('.card-actions');
    if (actions) renderProgress(actions, body);
  }

  // Zeile auf der Downloads-Seite
  const item = document.querySelector(`.dl-item[data-id="${CSS.escape(recId)}"]`);
  if (item) {
    const progressEl = item.querySelector('.dl-item-progress');
    if (progressEl) renderProgress(progressEl, body);
  }

  if (body.state === 'done') {
    finishJob(recId, jobId, 'done');
  } else if (body.state === 'error') {
    finishJob(recId, jobId, 'error', body.error);
  } else if (body.state === 'cancelled') {
    finishJob(recId, jobId, 'cancelled');
  }
}

function finishJob(recId, jobId, state, error) {
  ACTIVE_JOBS.delete(recId);
  updateDownloadsBadge();
  stopPollingIfIdle();

  const rec = STATE.recordings.find(r => r.id === recId);
  const title = rec ? `„${rec.title}"` : 'Aufnahme';

  if (state === 'done') {
    toast(`${title} fertig heruntergeladen.`, 'success', 4000);
  } else if (state === 'error') {
    toast(`Download fehlgeschlagen: ${error || 'unbekannt'}`, 'error', 6000);
  } else if (state === 'cancelled') {
    toast(`${title}: Download abgebrochen.`, 'info', 3000);
  }

  const settleDelay = state === 'done' ? 1500 : 600;

  // Karte auf der Recordings-Seite zurücksetzen
  setTimeout(() => {
    const card = document.querySelector(`.card[data-id="${CSS.escape(recId)}"]`);
    if (card && rec) restoreCardActions(card, rec);
  }, settleDelay);

  // Zeile auf der Downloads-Seite ausfaden + entfernen
  setTimeout(() => {
    const item = document.querySelector(`.dl-item[data-id="${CSS.escape(recId)}"]`);
    if (!item) return;
    item.classList.add('removing');
    setTimeout(() => {
      item.remove();
      // Wenn Downloads-Liste jetzt leer und View aktiv: Empty-State zeigen
      if (STATE.view === 'downloads' &&
          els.downloadsList.querySelectorAll('.dl-item').length === 0) {
        renderDownloadsView();
      }
    }, 280);
  }, settleDelay);
}

async function cancelJob(recId) {
  const ctx = ACTIVE_JOBS.get(recId);
  if (!ctx) return;
  try {
    await api(`/api/download/${ctx.jobId}/cancel`, { method: 'POST' });
  } catch {}
}

function renderProgress(actionsEl, p) {
  const pct = Math.max(0, Math.min(100, Number(p.percent) || 0));
  const stateText = stateLabel(p);
  const stateClass = p.state === 'error' ? 'error' : (p.state === 'done' ? 'done' : '');
  const detailLine = (p.speed || p.eta)
    ? `${p.speed || ''}${p.speed && p.eta ? ' · ' : ''}${p.eta ? 'ETA ' + p.eta : ''}`
    : '';

  // Bar bei "queued" indeterminate anzeigen, sonst feste Breite
  const indet = (p.state === 'queued' || p.state === 'running' && pct === 0);

  actionsEl.innerHTML = `
    <div class="dl">
      <div class="dl-row">
        <div class="dl-bar"><div class="dl-bar-fill ${indet ? 'indeterminate' : ''}" style="width:${pct}%"></div></div>
        <button class="dl-cancel" type="button" aria-label="Abbrechen" title="Abbrechen">
          <svg viewBox="0 0 24 24"><path fill="currentColor" d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg>
        </button>
      </div>
      <div class="dl-stats">
        <span class="dl-pct">${p.state === 'done' ? '✓ Fertig' : `${pct.toFixed(0)} %`}</span>
        <span class="dl-state ${stateClass}">${stateText}${detailLine ? ' · ' + detailLine : ''}</span>
      </div>
    </div>
  `;

  const cancelBtn = actionsEl.querySelector('.dl-cancel');
  if (cancelBtn) {
    cancelBtn.addEventListener('click', () => {
      const parent = actionsEl.closest('[data-id]');
      if (parent) cancelJob(parent.dataset.id);
    });
  }
}

function stateLabel(p) {
  switch (p.state) {
    case 'queued':
      if (p.message) return p.message;
      if (p.queue_position > 0) return `In Queue (Pos. ${p.queue_position})`;
      return 'Wartet…';
    case 'running': return 'Läuft';
    case 'done': return 'Fertig';
    case 'error': return p.error ? p.error.slice(0, 80) : 'Fehler';
    case 'cancelled': return 'Abgebrochen';
    default: return p.state || '';
  }
}

function restoreCardActions(card, rec) {
  const actions = card.querySelector('.card-actions');
  if (!actions) return;
  actions.innerHTML = '';
  actions.appendChild(buildSplitButton(card, rec));
}

/* --- Loading recordings ------------------------------------------------- */

async function loadRecordings({ force = false } = {}) {
  els.loading.hidden = false;
  els.empty.hidden = true;
  els.grid.innerHTML = '';

  try {
    const path = force ? '/api/recordings/refresh' : '/api/recordings';
    const opts = force ? { method: 'POST' } : { method: 'GET' };
    const { ok, status, body } = await api(path, opts);

    if (status === 401) {
      showLogin();
      return;
    }
    if (!ok) {
      toast(`Fehler beim Laden: ${body.error || status}`, 'error', 5000);
      return;
    }
    STATE.recordings = body.recordings || [];
    STATE.page = 1;
    render();
  } catch (e) {
    toast(`Backend nicht erreichbar: ${e.message}. Läuft zattoo-gui.py noch?`, 'error', 7000);
  } finally {
    els.loading.hidden = true;
  }
}

/* --- Login flow --------------------------------------------------------- */

function showLogin() {
  els.loginModal.hidden = false;
  els.topbarActions.hidden = true;
  els.tabs.hidden = true;
  els.statsBar.hidden = true;
  els.recordingsView.hidden = true;
  els.downloadsView.hidden = true;
  els.loading.hidden = true;
  els.empty.hidden = true;
  els.grid.innerHTML = '';
  setTimeout(() => els.loginUser.focus(), 80);
}

function showApp() {
  els.loginModal.hidden = true;
  els.topbarActions.hidden = false;
  els.tabs.hidden = false;
  els.statsBar.hidden = false;
  // setView entscheidet, welche der beiden Views sichtbar wird
}

const hideLogin = showApp; // alias, behavior unchanged

async function handleLogin(e) {
  e.preventDefault();
  els.loginError.hidden = true;
  els.loginSubmit.disabled = true;
  els.loginSubmit.textContent = 'Anmelden …';

  const { ok, body } = await api('/api/login', {
    method: 'POST',
    body: JSON.stringify({
      username: els.loginUser.value,
      password: els.loginPass.value,
    }),
  });

  els.loginSubmit.disabled = false;
  els.loginSubmit.textContent = 'Anmelden';

  if (!ok) {
    els.loginError.textContent = body.error || 'Anmeldung fehlgeschlagen.';
    els.loginError.hidden = false;
    return;
  }

  els.loginPass.value = '';
  hideLogin();
  await loadRecordings({ force: true });
  toast('Erfolgreich angemeldet.', 'success');
}

/* --- Settings flow ------------------------------------------------------ */

function openSettings() {
  els.metubeHost.value = STATE.settings.metubeHost;
  els.defaultTarget.value = STATE.settings.target;
  els.bilingualToggle.checked = STATE.settings.bilingual;
  els.settingsModal.hidden = false;
}

function closeSettings() {
  els.settingsModal.hidden = true;
}

function handleSettingsSubmit(e) {
  e.preventDefault();
  const target = els.defaultTarget.value;
  STATE.settings.target = TARGETS[target] ? target : 'downie';
  STATE.settings.metubeHost = els.metubeHost.value.trim();
  STATE.settings.bilingual = !!els.bilingualToggle.checked;
  saveSettings();

  // Update all card buttons to reflect new default target
  $$('.card .split-main').forEach(setMainButtonLabel);

  closeSettings();
  toast('Einstellungen gespeichert.', 'success', 2200);
}

async function handleLogout() {
  closeSettings();
  await fetch('/api/recordings/refresh', { method: 'POST' }).catch(() => {});
  toast('cookies.txt löschen, um Logout zu erzwingen — oder neuen Login durchführen.', 'info', 4500);
}

/* --- Wiring ------------------------------------------------------------- */

function wire() {
  // Tab-Buttons (Aufnahmen / Downloads)
  $$('.tab').forEach(t => {
    t.addEventListener('click', () => setView(t.dataset.view));
  });

  // Browser Back/Forward → Hash-Wechsel
  window.addEventListener('hashchange', () => {
    const v = location.hash === '#downloads' ? 'downloads' : 'recordings';
    if (STATE.view !== v) setView(v);
  });

  els.filterInput.addEventListener('input', (e) => {
    STATE.filter = e.target.value;
    STATE.page = 1;
    render();
  });

  els.refreshBtn.addEventListener('click', async () => {
    els.refreshBtn.classList.add('spinning');
    await loadRecordings({ force: true });
    els.refreshBtn.classList.remove('spinning');
  });

  els.settingsBtn.addEventListener('click', openSettings);
  els.settingsClose.addEventListener('click', closeSettings);
  els.settingsForm.addEventListener('submit', handleSettingsSubmit);
  els.logoutBtn.addEventListener('click', handleLogout);

  els.loginForm.addEventListener('submit', handleLogin);

  els.settingsModal.addEventListener('click', (e) => {
    if (e.target === els.settingsModal) closeSettings();
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      closeMenu();
      if (!els.settingsModal.hidden) closeSettings();
    }
  });
}

/* --- Bootstrap ---------------------------------------------------------- */

async function bootstrap() {
  wire();
  updateDefaultTargetChip();

  try {
    const { body } = await api('/api/session');
    if (body.logged_in) {
      showApp();
      // Reihenfolge wichtig: erst aktive Jobs laden, dann Recordings rendern —
      // dann zeigt der erste render()-Lauf direkt die Progress-Bars an.
      await loadActiveJobs();
      await loadRecordings();
      // Initial-View aus dem URL-Hash (z.B. http://…/#downloads)
      const initialView = location.hash === '#downloads' ? 'downloads' : 'recordings';
      setView(initialView);
    } else {
      showLogin();
    }
  } catch (e) {
    els.loading.hidden = true;
    toast(`Backend nicht erreichbar: ${e.message}. Läuft zattoo-gui.py noch?`, 'error', 8000);
  }
}

async function loadActiveJobs() {
  try {
    const { ok, body } = await api('/api/jobs');
    if (!ok) return;
    const jobs = body.jobs || [];
    if (jobs.length === 0) return;
    for (const job of jobs) {
      ACTIVE_JOBS.set(job.recording_id, { jobId: job.id });
    }
    updateDownloadsBadge();
    startPolling();
    const word = jobs.length === 1 ? 'aktiver Download' : 'aktive Downloads';
    toast(`${jobs.length} ${word} wieder verbunden.`, 'info', 3500);
  } catch {
    // non-critical — UI funktioniert auch ohne Restore
  }
}

bootstrap();
