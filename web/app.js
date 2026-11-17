/* livewatcher dashboard. Plain script, no build step: Alpine.js drives the page,
 * uPlot draws the chat-rate charts and hls.js plays the live buffer.
 *
 * Fast-changing data (rate samples, chart and player objects) lives outside
 * Alpine's reactive state so the page doesn't re-render on every sample.
 */
'use strict';

const RATE_KEEP_S = 15 * 60;   // matches app.rate_history()
const LOG_KEEP = 1000;
const LEVELS = { debug: 0, info: 1, warning: 2, error: 3 };

const rates = new Map();       // streamer -> { ts: [], rate: [], base: [], thr: [] }
const charts = new Map();      // key -> { u, el, name, windowS, ro }
const players = new Map();     // video element -> { hls, io, name }
const dirty = new Set();       // streamers whose charts need a redraw
let peaksRef = [];             // the component's peaks array (non-reactive copy)

function series(name) {
  let s = rates.get(name);
  if (!s) { s = { ts: [], rate: [], base: [], thr: [] }; rates.set(name, s); }
  return s;
}

function addRate(r) {
  const s = series(r.streamer);
  const n = s.ts.length;
  if (n && r.ts <= s.ts[n - 1]) return;  // already have it (resync overlap)
  s.ts.push(r.ts); s.rate.push(r.rate); s.base.push(r.baseline); s.thr.push(r.threshold);
  const cutoff = r.ts - RATE_KEEP_S;
  let drop = 0;
  while (drop < s.ts.length && s.ts[drop] < cutoff) drop++;
  if (drop) for (const k of ['ts', 'rate', 'base', 'thr']) s[k].splice(0, drop);
  dirty.add(r.streamer);
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch('/api' + path, opts);
  let data = null;
  const text = await res.text();
  if (text) { try { data = JSON.parse(text); } catch { data = text; } }
  if (!res.ok) {
    let msg = data && data.detail !== undefined ? data.detail : res.statusText;
    if (Array.isArray(msg)) msg = msg.map(e => (e.loc ? e.loc.filter(x => x !== 'body').join('.') + ': ' : '') + e.msg).join('; ');
    const err = new Error(String(msg));
    err.status = res.status;
    throw err;
  }
  return data;
}

const pad = n => String(n).padStart(2, '0');
function clock(ts) {
  if (!ts) return '–';
  const d = new Date(ts * 1000);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
function duration(s) {
  s = Math.max(0, Math.round(s));
  return s >= 60 ? `${Math.floor(s / 60)}:${pad(s % 60)}` : `${s} s`;
}

/* ---- charts ------------------------------------------------------------------ */
function chartOpts(width, height, windowS, getName) {
  const ink2 = cssVar('--ink-2'), grid = cssVar('--grid');
  const font = `500 11px ${cssVar('--font-ui')}`;
  const axis = { stroke: ink2, font, grid: { stroke: grid, width: 1 }, ticks: { show: false }, size: 26, gap: 4 };
  return {
    width, height,
    padding: [8, 6, 0, 0],
    legend: { show: false },
    cursor: { y: false, points: { size: 7, width: 2, fill: cssVar('--panel') }, drag: { x: false, y: false } },
    scales: {
      x: { time: true },
      y: { range: (u, min, max) => [0, Math.max(4, Math.ceil(max * 1.15))] },
    },
    axes: [
      { ...axis, space: windowS > 400 ? 90 : 70, values: (u, splits) => splits.map(v => { const d = new Date(v * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; }) },
      { ...axis, size: 34, space: 24, values: (u, splits) => splits.map(v => (Number.isInteger(v) ? v : v.toFixed(1))) },
    ],
    series: [
      {},
      { label: 'Chat rate', stroke: cssVar('--trace'), width: 2, fill: cssVar('--trace-fill'), points: { show: false } },
      { label: 'Normal', stroke: cssVar('--base-line'), width: 1.5, points: { show: false } },
      { label: 'Clip threshold', stroke: cssVar('--peak'), width: 1.25, dash: [5, 4], points: { show: false } },
    ],
    hooks: {
      drawClear: [u => drawPeaks(u, getName())],
      setCursor: [u => showTip(u)],
    },
  };
}

function drawPeaks(u, name) {
  const ctx = u.ctx;
  const { left, top, width, height } = u.bbox;
  const [xmin, xmax] = [u.scales.x.min, u.scales.x.max];
  if (xmin == null) return;
  const band = cssVar('--peak-band'), mark = cssVar('--peak');
  const dpr = devicePixelRatio || 1;
  ctx.save();
  ctx.beginPath(); ctx.rect(left, top, width, height); ctx.clip();
  for (const p of peaksRef) {
    if (p.streamer !== name || p.ts_end < xmin || p.ts_start > xmax) continue;
    const x0 = u.valToPos(Math.max(p.ts_start, xmin), 'x', true);
    const x1 = u.valToPos(Math.min(Math.max(p.ts_end, p.ts_start + 1), xmax), 'x', true);
    ctx.fillStyle = band;
    ctx.fillRect(x0, top, Math.max(x1 - x0, 2 * dpr), height);
    if (p.ts_peak >= xmin && p.ts_peak <= xmax) {
      const xp = Math.round(u.valToPos(p.ts_peak, 'x', true));
      ctx.fillStyle = mark;
      ctx.fillRect(xp - dpr, top, 2 * dpr, height);
      const s = 5 * dpr;  // a small downward triangle at the top
      ctx.beginPath(); ctx.moveTo(xp - s, top); ctx.lineTo(xp + s, top); ctx.lineTo(xp, top + s * 1.4); ctx.closePath(); ctx.fill();
    }
  }
  ctx.restore();
}

function showTip(u) {
  const tip = u.root.closest('.chart-wrap')?.querySelector('.chart-tip');
  if (!tip) return;
  const i = u.cursor.idx;
  if (i == null || u.data[0][i] == null) { tip.hidden = true; return; }
  const [ts, r, b, t] = [u.data[0][i], u.data[1][i], u.data[2][i], u.data[3][i]];
  tip.hidden = false;
  tip.innerHTML = `<time>${clock(ts)}</time><span class="t-rate">${r.toFixed(1)} msg/s</span>` +
    `<span class="t-base">normal ${b.toFixed(1)}</span><span class="t-thr">clips at ${t.toFixed(1)}</span>`;
  const x = u.valToPos(ts, 'x');
  const w = u.root.clientWidth;
  tip.style.left = Math.min(Math.max(x - 60, 0), w - 130) + 'px';
}

function chartData(name, windowS) {
  const s = series(name);
  const now = Date.now() / 1000;
  return { data: [s.ts, s.rate, s.base, s.thr], min: now - windowS, max: now };
}

function redraw(entry) {
  const name = entry.name;
  const { data, min, max } = chartData(name, entry.windowS);
  entry.u.batch(() => {
    entry.u.setData(data, false);
    entry.u.setScale('x', { min, max });
  });
}

function buildChart(entry) {
  const el = entry.el;
  const w = Math.max(el.clientWidth, 100), h = Math.max(el.clientHeight, 60);
  entry.u?.destroy();
  el.innerHTML = '';
  const tip = document.createElement('div');
  tip.className = 'chart-tip'; tip.hidden = true;
  el.parentElement.querySelector('.chart-tip')?.remove();
  el.parentElement.appendChild(tip);
  entry.u = new uPlot(chartOpts(w, h, entry.windowS, () => entry.name), [[], [], [], []], el);
  el.addEventListener('mouseleave', () => { tip.hidden = true; });
  redraw(entry);
}

function pruneDetached() {
  for (const [key, entry] of charts) {
    if (!entry.el.isConnected) { entry.ro.disconnect(); entry.u?.destroy(); charts.delete(key); }
  }
  for (const [video, p] of players) {
    if (!video.isConnected) { p.io?.disconnect(); p.hls?.destroy(); players.delete(video); }
  }
}

/* ---- the Alpine component -------------------------------------------------------- */
document.addEventListener('alpine:init', () => {
  Alpine.data('dash', () => ({
    views: [
      { id: 'streams', label: 'Streams' }, { id: 'clips', label: 'Clips' },
      { id: 'debug', label: 'Debug' }, { id: 'settings', label: 'Settings' },
    ],
    clipTabs: [
      { id: 'review', label: 'To review' }, { id: 'queued', label: 'Queued' },
      { id: 'uploaded', label: 'Uploaded' }, { id: 'failed', label: 'Failed and dropped' },
    ],
    layouts: [{ id: 'blur_fit', label: 'Blur fit' }, { id: 'crop', label: 'Crop' }, { id: 'stacked', label: 'Stacked' }],
    detFields: [
      { key: 'multiplier', label: 'Times normal', min: 1, max: 10, step: 0.1, unit: '×', help: 'A peak needs chat at this many times its normal rate.' },
      { key: 'z_threshold', label: 'Unusualness (z)', min: 0, max: 12, step: 0.1, unit: '', help: 'How far above normal, in standard deviations.' },
      { key: 'min_msgs_per_sec', label: 'Busiest second, at least', min: 0, max: 30, step: 0.5, unit: 'msg/s', help: 'Ignores spikes in very quiet chats.' },
      { key: 'min_unique_chatters', label: 'Different chatters, at least', min: 0, max: 60, step: 1, unit: '', help: 'Stops one person spamming from counting.' },
      { key: 'end_ratio', label: 'Peak ends below', min: 1, max: 5, step: 0.1, unit: '× normal', help: 'When chat falls under this, the peak is over.' },
      { key: 'cooldown_s', label: 'Wait between peaks', min: 0, max: 600, step: 5, unit: 's', help: 'No new peak this soon after the last one.' },
      { key: 'smooth_s', label: 'Smoothing', min: 1, max: 30, step: 1, unit: 's', help: 'Averages the rate over this many seconds.' },
      { key: 'baseline_window_s', label: 'Normal is measured over', min: 60, max: 1800, step: 30, unit: 's', help: 'How far back "normal chat" looks.' },
      { key: 'preroll_s', label: 'Keep before the peak', min: 0, max: 60, step: 1, unit: 's', help: 'Seconds of video kept before chat rose.' },
      { key: 'postroll_s', label: 'Keep after the peak', min: 0, max: 30, step: 1, unit: 's', help: 'Seconds kept after chat fell back.' },
      { key: 'max_clip_s', label: 'Longest clip', min: 10, max: 180, step: 5, unit: 's', help: 'A clip never runs longer than this.' },
    ],

    view: 'streams',
    arrange: 'strip',
    theme: 'system',
    loaded: false,
    now: Date.now() / 1000,  // ticks every second so "2 min ago" stays true
    loadError: '',
    isDemo: false,

    // server state
    config: null,
    components: {},
    streamers: [],      // [{name, enabled, status, has_playlist, has_snapshot}]
    latest: {},         // streamer -> last RateSample
    snapTs: {},         // streamer -> ts of the latest snapshot (cache buster)
    liveFailed: {},     // streamer -> true when hls.js gave up
    peaks: [],
    clips: [],
    logs: [],

    // ui state
    selected: null,
    busy: {},
    drafts: {},
    layoutPick: {},
    noPreview: {},
    clipTab: 'review',
    toasts: [],
    confirmRemove: null,
    newStreamer: '',
    addError: '',
    limits: { dirty: false },
    logLevel: 'info', logStreamer: '', logSearch: '', logPaused: false, logBacklog: 0, logFollow: true,
    detScope: '',
    peakFilter: '',
    detPending: {},     // key -> value not yet confirmed by the server
    lay: { streamer: '', layout: 'blur_fit', crop_x: 0.5, fit_zoom: 1, stack_top: {}, stack_bottom: {}, stack_split: 0.4, dirty: false },
    previewUrl: '', previewFailed: false,

    // connection
    connState: 'connecting',  // connecting | open | closed
    everConnected: false,
    retryIn: 0,
    _ws: null, _retryDelay: 1000, _retryTimer: null, _countdown: null, _logSeq: 0, _pendingLogs: [],

    /* ---- lifecycle -------------------------------------------------------- */
    init() {
      try {
        this.arrange = localStorage.getItem('lw.arrange') || 'strip';
        this.theme = localStorage.getItem('lw.theme') || 'system';
        const v = localStorage.getItem('lw.view');
        if (v && this.views.some(x => x.id === v)) this.view = v;
      } catch { /* storage blocked: defaults are fine */ }
      this.applyTheme();
      const fromHash = location.hash.slice(1);
      if (this.views.some(x => x.id === fromHash)) this.view = fromHash;
      window.addEventListener('hashchange', () => {
        const h = location.hash.slice(1);
        if (this.views.some(x => x.id === h)) this.view = h;
      });
      matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => this.rebuildCharts());
      this.connect();
      setInterval(() => this.tick(), 1000);
      this.$watch('view', v => {
        try { localStorage.setItem('lw.view', v); } catch { /* ignore */ }
        if (location.hash.slice(1) !== v) history.replaceState(null, '', '#' + v);
        this.$nextTick(() => { this.resizeCharts(); if (v === 'debug') this.scrollLogToEnd(); });
      });
      this.$watch('arrange', () => this.$nextTick(() => this.resizeCharts()));
      this.$watch('detScope', () => { this.detPending = {}; });
      this.$watch('logPaused', paused => {
        if (paused) return;
        const backlog = this._pendingLogs.splice(0);
        this.logBacklog = 0;
        this.logs.push(...backlog);
        if (this.logs.length > LOG_KEEP) this.logs.splice(0, this.logs.length - LOG_KEEP);
        this.logFollow = true;
        this.$nextTick(() => this.scrollLogToEnd());
      });
    },

    tick() {
      this.now = Date.now() / 1000;
      pruneDetached();
      for (const entry of charts.values()) {
        if (entry.el.offsetParent !== null) redraw(entry);  // slides the window even when chat is silent
      }
      dirty.clear();
    },

    /* ---- connection and loading ------------------------------------------- */
    connect() {
      clearTimeout(this._retryTimer); clearInterval(this._countdown);
      this.connState = 'connecting';
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      let ws;
      try { ws = new WebSocket(`${proto}//${location.host}/api/ws`); } catch { this.scheduleReconnect(); return; }
      this._ws = ws;
      ws.onopen = async () => {
        this.connState = 'open';
        this._retryDelay = 1000;
        await this.resync();
        this.everConnected = true;
      };
      ws.onmessage = ev => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch { return; }
        this.onEvent(msg.topic, msg.data);
      };
      ws.onclose = () => {
        if (this._ws !== ws) return;
        this.connState = 'closed';
        this.scheduleReconnect();
      };
    },

    scheduleReconnect() {
      const delay = this._retryDelay;
      this._retryDelay = Math.min(this._retryDelay * 2, 10000);
      this.retryIn = Math.ceil(delay / 1000);
      clearInterval(this._countdown);
      this._countdown = setInterval(() => { this.retryIn = Math.max(0, this.retryIn - 1); }, 1000);
      this._retryTimer = setTimeout(() => this.connect(), delay);
      if (!this.everConnected) this.loadError = 'Is the app running? Start it with ./lw run or ./lw demo.';
    },

    reconnectNow() {
      this._retryDelay = 1000;
      if (this._ws) { this._ws.onclose = null; try { this._ws.close(); } catch { /* ignore */ } }
      this.connect();
    },

    connTitle() {
      return { open: 'Live updates connected', connecting: 'Connecting…', closed: 'Disconnected' }[this.connState];
    },

    async resync() {
      try {
        const since = Date.now() / 1000 - 24 * 3600;
        const [state, config, clips, peaks, logs] = await Promise.all([
          api('GET', '/state'), api('GET', '/config'), api('GET', '/clips?limit=500'),
          api('GET', `/peaks?since=${since}&limit=2000`), api('GET', '/logs'),
        ]);
        this.applyState(state);
        this.setConfig(config);
        this.clips = clips;
        this.peaks = peaks; peaksRef = peaks.slice();
        this.logs = logs.map(l => ({ ...l, _k: ++this._logSeq }));
        await Promise.all(this.streamers.map(async s => {
          const list = await api('GET', `/streamers/${encodeURIComponent(s.name)}/rates`);
          const sr = series(s.name);
          for (const k of ['ts', 'rate', 'base', 'thr']) sr[k].length = 0;
          list.forEach(addRate);
          if (list.length) this.latest[s.name] = list[list.length - 1];
        }));
        this.loadError = '';
        this.loaded = true;
        this.$nextTick(() => { this.redrawAll(); this.scrollLogToEnd(); });
      } catch (e) {
        this.loadError = e.message;
      }
    },

    applyState(state) {
      this.components = state.components;
      this.isDemo = Object.values(state.components).every(k => k === 'fake');
      const now = Date.now() / 1000;
      for (const s of state.streamers) if (s.has_snapshot && !this.snapTs[s.name]) this.snapTs[s.name] = now;
      this.streamers = state.streamers;
      if (this.selected && !this.streamers.some(s => s.name === this.selected)) this.selected = null;
      if (!this.lay.streamer && this.streamers.length) { this.lay.streamer = this.streamers[0].name; }
    },

    setConfig(config) {
      this.config = config;
      if (!this.limits.dirty) this.loadLimits();
      if (!this.lay.dirty) this.loadLayoutForm();
    },

    async refreshState() {
      try { this.applyState(await api('GET', '/state')); } catch { /* the socket handler reports outages */ }
    },

    onEvent(topic, data) {
      switch (topic) {
        case 'rate':
          addRate(data);
          this.latest[data.streamer] = data;
          break;
        case 'peak':
          if (!this.peaks.some(p => p.id === data.id)) {
            this.peaks.unshift(data); peaksRef.unshift(data);
          }
          break;
        case 'clip': this.upsertClip(data); break;
        case 'stream_status': {
          const s = this.streamers.find(x => x.name === data.streamer);
          if (s) s.status = data;
          break;
        }
        case 'snapshot': this.onSnapshot(data.streamer, data.ts); break;
        case 'config_changed':
          api('GET', '/config').then(c => this.setConfig(c)).catch(() => {});
          if (data.patch && ('streamers' in data.patch || 'general' in data.patch || 'tiktok' in data.patch)) this.refreshState();
          break;
        case 'log': this.addLog(data); break;
      }
    },

    onSnapshot(name, ts) {
      const s = this.streamers.find(x => x.name === name);
      if (s) s.has_snapshot = true;
      // Preload, then swap, so the picture never flashes empty.
      const img = new Image();
      img.onload = () => { this.snapTs[name] = ts; };
      img.src = `/api/streamers/${encodeURIComponent(name)}/snapshot.jpg?t=${ts}`;
      if (this.view === 'debug' && this.lay.streamer === name && !this.previewUrl) this.updatePreview();
    },

    upsertClip(c) {
      const i = this.clips.findIndex(x => x.id === c.id);
      if (i >= 0) {
        const prev = this.clips[i];
        if (prev.renders[prev.active_layout] !== c.renders[c.active_layout]) delete this.noPreview[c.id];
        this.clips[i] = c;
      } else {
        this.clips.unshift(c);
      }
      if (this.drafts[c.id] === c.caption) delete this.drafts[c.id];
    },

    addLog(line) {
      const item = { ...line, _k: ++this._logSeq };
      if (this.logPaused) {
        this._pendingLogs.push(item);
        if (this._pendingLogs.length > LOG_KEEP) this._pendingLogs.shift();
        this.logBacklog = this._pendingLogs.length;
        return;
      }
      this.logs.push(item);
      if (this.logs.length > LOG_KEEP) this.logs.splice(0, this.logs.length - LOG_KEEP);
      if (this.logFollow) this.$nextTick(() => this.scrollLogToEnd());
    },

    /* ---- navigation, keys, theme ------------------------------------------- */
    go(view) { this.view = view; },
    setArrange(a) { this.arrange = a; try { localStorage.setItem('lw.arrange', a); } catch { /* ignore */ } },

    setTheme(t) {
      this.theme = t;
      try { localStorage.setItem('lw.theme', t); } catch { /* ignore */ }
      this.applyTheme();
    },
    applyTheme() {
      if (this.theme === 'system') delete document.documentElement.dataset.theme;
      else document.documentElement.dataset.theme = this.theme;
      this.$nextTick(() => this.rebuildCharts());
    },

    onKey(e) {
      if (this.view !== 'streams' || e.altKey || e.ctrlKey || e.metaKey) return;
      const t = e.target;
      if (t && (t.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT', 'VIDEO'].includes(t.tagName))) return;
      if (e.key === 'Escape' && this.selected) { this.select(null); return; }
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
      if (!this.streamers.length) return;
      e.preventDefault();
      const i = this.streamers.findIndex(s => s.name === this.selected);
      const step = e.key === 'ArrowRight' ? 1 : -1;
      const next = i < 0 ? (step > 0 ? 0 : this.streamers.length - 1)
        : (i + step + this.streamers.length) % this.streamers.length;
      this.select(this.streamers[next].name);
    },

    select(name) {
      this.selected = name;
      if (name) {
        this.$nextTick(() => {
          // Bring the large view into sight, and slide the strip (sideways only) to the card.
          const focus = document.querySelector('.focus');
          const top = focus?.getBoundingClientRect().top;
          if (focus && (top < 56 || top > innerHeight * 0.6)) focus.scrollIntoView({ behavior: 'smooth', block: 'start' });
          const card = document.querySelector(`.stream-card[data-name="${CSS.escape(name)}"]`);
          const strip = card?.parentElement;
          if (card && strip && strip.scrollWidth > strip.clientWidth) {
            strip.scrollTo({ left: card.offsetLeft - strip.offsetLeft - (strip.clientWidth - card.offsetWidth) / 2, behavior: 'smooth' });
          }
        });
      }
    },

    get focused() { return this.streamers.find(s => s.name === this.selected) || null; },

    /* ---- streams ---------------------------------------------------------- */
    liveSummary() {
      if (!this.loaded) return 'Loading…';
      const watched = this.streamers.filter(s => s.enabled);
      const live = watched.filter(s => s.status.live).length;
      if (!watched.length) return 'Nobody is being watched';
      return `${live} of ${watched.length} live`;
    },
    snapUrl(name) {
      const ts = this.snapTs[name];
      return ts ? `/api/streamers/${encodeURIComponent(name)}/snapshot.jpg?t=${ts}` : '';
    },
    showLive(s) { return s.has_playlist && !this.liveFailed[s.name] && typeof Hls !== 'undefined'; },
    monitorClass(s) {
      return { live: s.status.live, off: !s.status.live, error: s.status.recorder_state === 'error' };
    },
    noPictureText(s) {
      if (!s.enabled) return 'Not watched';
      if (!s.status.live) return 'Offline';
      return 'Waiting for the first picture';
    },
    stateText(s) {
      if (!s.enabled) return 'Off';
      if (s.status.recorder_state === 'error') return 'Recorder error';
      if (!s.status.live) return 'Offline';
      return { recording: 'Live', starting: 'Live, starting recorder', stopped: 'Live, not recording' }[s.status.recorder_state] || 'Live';
    },
    recorderLabel(state) {
      return { stopped: 'Stopped', starting: 'Starting', recording: 'Recording', error: 'Error' }[state] || state;
    },
    bufferText(s) {
      const { buffer_start: a, buffer_end: b } = s.status;
      if (a == null || b == null || b <= a) return '';
      return `${duration(b - a)} buffered`;
    },
    hasRates(name) { return !!this.latest[name]; },
    recentPeaks(name, windowS) {
      const since = this.now - windowS;
      return this.peaks.filter(p => p.streamer === name && p.ts_peak >= since);
    },
    peakSummary(name) {
      const list = this.recentPeaks(name, 300);
      const last = this.peaks.find(p => p.streamer === name);
      if (!list.length) return last ? `Last peak ${this.fmtAgo(last.ts_peak)}` : 'No peaks yet';
      return `${list.length} peak${list.length > 1 ? 's' : ''} in 5 min`;
    },
    clipNowHint(s) {
      if (!s.status.live) return 'Clip now needs the stream to be live.';
      const pre = this.config?.detection?.preroll_s;
      return pre != null ? `Saves the last ${Math.round(pre)} s and what follows.` : '';
    },

    async clipNow(name) {
      this.busy['clip:' + name] = true;
      try {
        const clip = await api('POST', `/streamers/${encodeURIComponent(name)}/clip-now`);
        this.upsertClip(clip);
        this.toast(`Clipping ${name}. It shows up in Clips when it's ready.`);
      } catch (e) {
        this.toast(`Couldn't clip ${name}: ${e.message}`, 'bad');
      } finally {
        this.busy['clip:' + name] = false;
      }
    },

    attachLive(video, name) {
      const src = `/api/streamers/${encodeURIComponent(name)}/live/live.m3u8`;
      const entry = { name, hls: null, io: null };
      players.set(video, entry);
      const start = () => {
        if (entry.hls || !video.isConnected) return;
        if (Hls.isSupported()) {
          const hls = new Hls({ liveSyncDurationCount: 2, maxBufferLength: 10, backBufferLength: 10 });
          hls.on(Hls.Events.ERROR, (ev, data) => {
            if (!data.fatal) return;
            if (data.type === Hls.ErrorTypes.MEDIA_ERROR) { hls.recoverMediaError(); return; }
            hls.destroy(); entry.hls = null;
            this.liveFailed[name] = true;  // fall back to the snapshot
            setTimeout(() => { delete this.liveFailed[name]; }, 30000);
          });
          hls.loadSource(src);
          hls.attachMedia(video);
          entry.hls = hls;
        } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
          video.src = src;
        } else {
          this.liveFailed[name] = true;
        }
        video.play?.().catch(() => {});
      };
      const stop = () => { entry.hls?.destroy(); entry.hls = null; video.pause(); };
      // Only play what can be seen: a long strip shouldn't decode every stream.
      entry.io = new IntersectionObserver(([e]) => (e.isIntersecting ? start() : stop()), { threshold: 0.05 });
      entry.io.observe(video);
    },

    /* ---- charts ----------------------------------------------------------- */
    mountChart(el, key, name, windowS) {
      const old = charts.get(key);
      if (old) { old.ro.disconnect(); old.u?.destroy(); }
      const entry = { el, name, windowS, u: null, ro: null, w: 0, h: 0 };
      entry.ro = new ResizeObserver(() => {
        const w = el.clientWidth, h = el.clientHeight;
        if (!w || !h || (w === entry.w && h === entry.h)) return;
        entry.w = w; entry.h = h;
        if (entry.u) entry.u.setSize({ width: w, height: h }); else buildChart(entry);
      });
      entry.ro.observe(el);
      charts.set(key, entry);
      this.$nextTick(() => { if (!entry.u && el.clientWidth) buildChart(entry); });
    },
    redrawAll() { for (const e of charts.values()) if (e.u) redraw(e); },
    resizeCharts() {
      for (const e of charts.values()) {
        if (!e.el.clientWidth) continue;
        if (e.u) e.u.setSize({ width: e.el.clientWidth, height: e.el.clientHeight }); else buildChart(e);
        if (e.u) redraw(e);
      }
    },
    rebuildCharts() { for (const e of charts.values()) if (e.el.clientWidth) buildChart(e); },

    /* ---- clips ------------------------------------------------------------ */
    tabStatuses: {
      review: ['pending_review'],
      queued: ['queued', 'uploading'],
      uploaded: ['uploaded', 'dry_run_ok'],
      failed: ['failed', 'dropped', 'rejected'],
    },
    get reviewCount() { return this.clips.filter(c => c.status === 'pending_review').length; },
    tabClips() {
      const st = this.tabStatuses[this.clipTab];
      return this.clips.filter(c => st.includes(c.status)).sort((a, b) => b.created_at - a.created_at);
    },
    tabCount(id) {
      const st = this.tabStatuses[id];
      const n = this.clips.filter(c => st.includes(c.status)).length;
      return id === 'review' ? n + this.inProgress().length : n;
    },
    inProgress() { return this.clips.filter(c => ['waiting', 'cutting', 'rendering'].includes(c.status)); },
    emptyClipsText() {
      return {
        review: this.autoUpload ? 'Nothing to review: auto-upload is on, so finished clips go straight to the queue.'
          : 'Nothing to review. New clips land here when chat spikes, or when you press Clip now.',
        queued: 'The upload queue is empty.',
        uploaded: 'Nothing uploaded yet.',
        failed: 'No failed, dropped or rejected clips.',
      }[this.clipTab];
    },
    stepLabel(st) { return { waiting: 'Recording the rest', cutting: 'Cutting', rendering: 'Rendering' }[st]; },
    stepClass(status, st) {
      const order = ['waiting', 'cutting', 'rendering'];
      const a = order.indexOf(status), b = order.indexOf(st);
      return { done: b < a, now: b === a };
    },
    statusLabel(s) {
      return {
        waiting: 'Waiting for video', cutting: 'Cutting', rendering: 'Rendering', pending_review: 'To review',
        queued: 'Queued', uploading: 'Uploading', uploaded: 'Uploaded', dry_run_ok: 'Dry run passed',
        failed: 'Failed', rejected: 'Rejected', dropped: 'Dropped',
      }[s] || s;
    },
    hasRender(c) { return !!c.renders[c.active_layout]; },
    videoUrl(c) {
      const path = c.renders[c.active_layout] || '';
      return `/api/clips/${c.id}/video?layout=${c.active_layout}&v=${encodeURIComponent(path.length + ':' + c.status)}`;
    },
    peakFor(c) { return c.peak_id ? this.peaks.find(p => p.id === c.peak_id) || null : null; },
    peakPos(c) {
      const p = this.peakFor(c);
      if (!p) return 0;
      const span = Math.max(c.window_end - c.window_start, 1);
      return Math.min(100, Math.max(0, ((p.ts_peak - c.window_start) / span) * 100));
    },
    windowTitle(c) {
      const p = this.peakFor(c);
      return `Clip window ${this.fmtClock(c.window_start)} to ${this.fmtClock(c.window_end)}` + (p ? `, peak at ${this.fmtClock(p.ts_peak)}` : '');
    },
    captionEditable(c) { return !['uploading', 'uploaded', 'dropped'].includes(c.status); },
    rerenderable(c) { return !!c.raw_path && ['pending_review', 'dry_run_ok', 'failed', 'rejected'].includes(c.status); },
    pickedLayout(c) { return this.layoutPick[c.id] || c.active_layout; },
    canApprove(c) { return ['pending_review', 'dry_run_ok', 'failed', 'rejected'].includes(c.status) && this.hasRender(c); },
    canReject(c) { return ['pending_review', 'dry_run_ok', 'failed', 'queued', 'waiting', 'cutting', 'rendering'].includes(c.status); },
    approveLabel(c) {
      return { pending_review: 'Approve', failed: 'Retry upload', dry_run_ok: 'Queue again', rejected: 'Approve after all' }[c.status] || 'Approve';
    },

    async act(c, action) {
      if (this.drafts[c.id] !== undefined && action === 'approve') await this.saveCaption(c);
      this.busy['act:' + c.id] = true;
      try {
        const clip = await api('POST', `/clips/${c.id}/${action}`);
        this.upsertClip(clip);
        this.toast(action === 'approve' ? `Queued the ${c.streamer} clip for upload.` : `Rejected the ${c.streamer} clip.`);
      } catch (e) {
        this.toast(`Couldn't ${action} the clip: ${e.message}`, 'bad');
      } finally {
        this.busy['act:' + c.id] = false;
      }
    },

    async saveCaption(c) {
      const draft = this.drafts[c.id];
      if (draft === undefined || draft === c.caption) { delete this.drafts[c.id]; return; }
      try {
        const clip = await api('PATCH', `/clips/${c.id}`, { caption: draft });
        this.upsertClip(clip);
        delete this.drafts[c.id];
        this.toast('Caption saved.');
      } catch (e) {
        this.toast(`Couldn't save the caption: ${e.message}`, 'bad');
      }
    },

    async rerender(c) {
      const layout = this.pickedLayout(c);
      this.busy['render:' + c.id] = true;
      try {
        const clip = await api('POST', `/clips/${c.id}/rerender`, { layout });
        delete this.layoutPick[c.id];
        delete this.noPreview[c.id];
        this.upsertClip(clip);
        if (clip.status === 'failed') this.toast(`Re-render failed: ${clip.error}`, 'bad');
        else this.toast(`Rendered in ${this.layouts.find(l => l.id === layout).label}. That's the version that will upload.`);
      } catch (e) {
        this.toast(`Couldn't re-render: ${e.message}`, 'bad');
      } finally {
        this.busy['render:' + c.id] = false;
      }
    },

    /* ---- config ------------------------------------------------------------ */
    get autoUpload() { return !!this.config?.general.auto_upload; },

    async patchConfig(patch, okText) {
      try {
        const cfg = await api('PATCH', '/config', patch);
        this.setConfig(cfg);
        if (okText) this.toast(okText);
        return cfg;
      } catch (e) {
        this.toast(`Couldn't save: ${e.message}`, 'bad');
        // Put the controls back the way the server has them.
        try { this.setConfig(await api('GET', '/config')); } catch { /* ignore */ }
        return null;
      }
    },

    setAutoUpload(on) {
      this.config.general.auto_upload = on;  // move the switch at once; the reply confirms it
      this.patchConfig({ general: { auto_upload: on } },
        on ? 'Auto-upload is on: finished clips go to the upload queue.' : 'Auto-upload is off: finished clips wait for review.');
    },

    streamerList() { return JSON.parse(JSON.stringify(this.config.streamers)); },

    // detection
    detValue(key) {
      if (key in this.detPending) return this.detPending[key];
      if (!this.config) return 0;
      if (this.detScope) {
        const s = this.config.streamers.find(x => x.name.toLowerCase() === this.detScope);
        if (s && s[key] !== undefined && s[key] !== null) return s[key];
      }
      return this.config.detection[key];
    },
    isOverride(key) {
      const s = this.config?.streamers.find(x => x.name.toLowerCase() === this.detScope);
      return !!s && s[key] !== undefined && s[key] !== null;
    },
    setDet(key, value) {
      this.detPending[key] = value;
      clearTimeout(this._detTimer);
      this._detTimer = setTimeout(() => this.flushDet(), 250);
    },
    async flushDet() {
      const pending = { ...this.detPending };
      if (!Object.keys(pending).length || !this.config) return;
      let patch;
      if (this.detScope) {
        const list = this.streamerList();
        const s = list.find(x => x.name.toLowerCase() === this.detScope);
        if (!s) return;
        Object.assign(s, pending);
        patch = { streamers: list };
      } else {
        patch = { detection: pending };
      }
      const cfg = await this.patchConfig(patch);
      // Drop what was confirmed, unless the slider moved again meanwhile.
      for (const [k, v] of Object.entries(pending)) if (this.detPending[k] === v) delete this.detPending[k];
      if (!cfg) this.detPending = {};
    },
    async clearOverride(key) {
      const list = this.streamerList();
      const s = list.find(x => x.name.toLowerCase() === this.detScope);
      if (!s) return;
      delete s[key];
      delete this.detPending[key];
      await this.patchConfig({ streamers: list }, `${this.detScope} uses the default again.`);
    },

    // layout tool
    loadLayoutForm() {
      if (!this.config || !this.lay.streamer) return;
      const s = this.config.streamers.find(x => x.name.toLowerCase() === this.lay.streamer) || {};
      const r = this.config.render;
      Object.assign(this.lay, {
        layout: s.layout || r.default_layout,
        crop_x: s.crop_x ?? 0.5,
        fit_zoom: s.fit_zoom ?? r.fit_zoom,
        stack_top: { ...(s.stack_top || { x: 0, y: 0.6, w: 0.25, h: 0.4 }) },
        stack_bottom: { ...(s.stack_bottom || { x: 0.15, y: 0, w: 0.7, h: 1 }) },
        stack_split: s.stack_split ?? 0.4,
        dirty: false,
      });
      this.updatePreview();
    },
    layoutChanged() {
      this.lay.dirty = true;
      clearTimeout(this._prevTimer);
      this._prevTimer = setTimeout(() => this.updatePreview(), 150);
    },
    updatePreview() {
      const s = this.streamers.find(x => x.name === this.lay.streamer);
      if (!s || !s.has_snapshot) { this.previewUrl = ''; return; }
      const l = this.lay, reg = r => [r.x, r.y, r.w, r.h].map(v => (+v).toFixed(3)).join(',');
      const q = new URLSearchParams({ layout: l.layout, t: String(this.snapTs[s.name] || Date.now()) });
      if (l.layout === 'crop') q.set('crop_x', l.crop_x);
      if (l.layout === 'blur_fit') q.set('fit_zoom', l.fit_zoom);
      if (l.layout === 'stacked') { q.set('stack_top', reg(l.stack_top)); q.set('stack_bottom', reg(l.stack_bottom)); q.set('stack_split', l.stack_split); }
      this.previewUrl = `/api/streamers/${encodeURIComponent(s.name)}/layout-preview.jpg?${q}`;
    },
    layoutHelp() {
      return {
        blur_fit: 'The whole picture, centred on a blurred copy of itself.',
        crop: 'A 9:16 slice of the picture. The yellow box is what stays.',
        stacked: 'Two regions stacked: usually the facecam on top and the game below.',
      }[this.lay.layout];
    },
    async saveLayout() {
      const list = this.streamerList();
      const s = list.find(x => x.name.toLowerCase() === this.lay.streamer);
      if (!s) return;
      const l = this.lay;
      Object.assign(s, {
        layout: l.layout, crop_x: l.crop_x, fit_zoom: l.fit_zoom, stack_split: l.stack_split,
        stack_top: { ...l.stack_top }, stack_bottom: { ...l.stack_bottom },
      });
      this.busy.layout = true;
      this.lay.dirty = false;
      const ok = await this.patchConfig({ streamers: list }, `Saved the layout for ${l.streamer}. New clips use it.`);
      if (!ok) this.lay.dirty = true;
      this.busy.layout = false;
    },

    // streamers
    streamerSummary(s) {
      const layout = this.layouts.find(l => l.id === (s.layout || this.config.render.default_layout));
      const overrides = Object.keys(s).filter(k => this.detFields.some(f => f.key === k) && s[k] != null).length;
      let text = layout ? layout.label : '';
      if (overrides) text += `, ${overrides} detection setting${overrides > 1 ? 's' : ''} of its own`;
      return text;
    },
    async setStreamerEnabled(name, on) {
      const list = this.streamerList();
      const s = list.find(x => x.name === name);
      s.enabled = on;
      await this.patchConfig({ streamers: list }, on ? `Watching ${name}.` : `Stopped watching ${name}.`);
    },
    async addStreamer() {
      const name = this.newStreamer.replace(/^@/, '').replace(/^https?:\/\/(www\.)?twitch\.tv\//i, '').replace(/\/.*$/, '').toLowerCase();
      this.addError = '';
      if (!/^[a-z0-9_]{3,25}$/.test(name)) { this.addError = 'A Twitch channel name is 3 to 25 letters, digits or underscores.'; return; }
      if (this.config.streamers.some(s => s.name.toLowerCase() === name)) { this.addError = `${name} is already in the list.`; return; }
      const list = this.streamerList();
      list.push({ name, enabled: true });
      if (await this.patchConfig({ streamers: list }, `Added ${name}.`)) this.newStreamer = '';
    },
    async removeStreamer(name) {
      this.confirmRemove = null;
      const list = this.streamerList().filter(s => s.name !== name);
      if (this.selected === name.toLowerCase()) this.selected = null;
      await this.patchConfig({ streamers: list }, `Removed ${name}.`);
    },

    // upload limits
    loadLimits() {
      const t = this.config?.tiktok;
      if (!t) return;
      this.limits = {
        max_uploads_per_day: t.max_uploads_per_day,
        min_minutes_between_uploads: t.min_minutes_between_uploads,
        caption_template: t.caption_template,
        dirty: false,
      };
    },
    async saveLimits() {
      const { dirty, ...tiktok } = this.limits;
      if (await this.patchConfig({ tiktok }, 'Upload limits saved.')) this.limits.dirty = false;
    },

    /* ---- debug ------------------------------------------------------------- */
    filteredLogs() {
      const min = LEVELS[this.logLevel] ?? 0;
      const q = this.logSearch.toLowerCase();
      return this.logs.filter(l => (LEVELS[l.level] ?? 0) >= min
        && (!this.logStreamer || l.streamer === this.logStreamer)
        && (!q || l.message.toLowerCase().includes(q) || l.source.toLowerCase().includes(q)));
    },
    onLogScroll(e) {
      const el = e.target;
      this.logFollow = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    },
    scrollLogToEnd() {
      const el = this.$refs.log;
      if (el && this.logFollow) el.scrollTop = el.scrollHeight;
    },
    healthDot(s) {
      if (!s.enabled) return 'off';
      if (s.status.recorder_state === 'error' || s.status.last_error) return 'bad';
      if (s.status.live && s.status.recorder_state === 'recording') return 'good';
      if (s.status.live) return 'warn';
      return 'off';
    },
    peakLog() {
      const list = this.peakFilter ? this.peaks.filter(p => p.streamer === this.peakFilter) : this.peaks;
      return list.slice().sort((a, b) => b.ts_peak - a.ts_peak).slice(0, 200);
    },
    clipForPeak(p) { return this.clips.find(c => c.peak_id === p.id) || null; },
    openClip(c) {
      const tab = Object.entries(this.tabStatuses).find(([, st]) => st.includes(c.status));
      this.clipTab = tab ? tab[0] : 'review';
      this.view = 'clips';
      this.$nextTick(() => document.querySelector(`.clip[data-id="${CSS.escape(c.id)}"]`)?.scrollIntoView({ block: 'center' }));
    },
    setSyncOffset(v) {
      if (!this.config || Number.isNaN(v)) return;
      this.config.capture.sync_offset_s = v;
      clearTimeout(this._syncTimer);
      this._syncTimer = setTimeout(() => this.patchConfig({ capture: { sync_offset_s: v } }), 300);
    },
    partLabel(name) {
      return {
        chat: 'Twitch chat', detector: 'Peak detector', recorder: 'Stream recorder', cutter: 'Clip cutter',
        renderer: 'Vertical renderer', uploader: 'TikTok uploader', upload_queue: 'Upload queue',
      }[name] || name;
    },

    /* ---- formatting and toasts ---------------------------------------------- */
    fmtClock: clock,
    fmtDur: duration,
    fmtRate(v) { return v == null ? '–' : (v >= 10 ? v.toFixed(0) : v.toFixed(1)); },
    fmtScore(v) { return `${(v || 0).toFixed(1)}×`; },
    fmtNum(v, step) {
      if (v == null || Number.isNaN(+v)) return '–';
      const decimals = step >= 1 ? 0 : step >= 0.1 ? 1 : 2;
      return (+v).toFixed(decimals);
    },
    fmtAgo(ts) {
      const s = this.now - ts;
      if (s < 10) return 'just now';
      if (s < 60) return `${Math.round(s)} s ago`;
      if (s < 3600) return `${Math.round(s / 60)} min ago`;
      if (s < 86400) return `${Math.round(s / 3600)} h ago`;
      return new Date(ts * 1000).toLocaleDateString();
    },
    toast(text, kind = 'ok') {
      const id = Math.random().toString(36).slice(2);
      this.toasts.push({ id, text, kind });
      setTimeout(() => { this.toasts = this.toasts.filter(t => t.id !== id); }, kind === 'bad' ? 7000 : 3500);
    },
  }));
});
