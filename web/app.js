/* argus dashboard. Plain script, no build step: Alpine.js drives the page,
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

let focusVideoEl = null;  // the focus view's <video>; kept out of Alpine's reactive state

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

function showTip(u, label = clock) {
  const tip = u.root.closest('.chart-wrap')?.querySelector('.chart-tip');
  if (!tip) return;
  const i = u.cursor.idx;
  if (i == null || u.data[0][i] == null) { tip.hidden = true; return; }
  const [ts, r, b, t] = [u.data[0][i], u.data[1][i], u.data[2][i], u.data[3][i]];
  tip.hidden = false;
  tip.innerHTML = `<time>${label(ts)}</time><span class="t-rate">${r.toFixed(1)} msg/s</span>` +
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
  for (const [id, entry] of clipCharts) {
    if (!entry.el.isConnected) dropClipChart(id);
  }
  for (const [video, io] of clipVideos) {
    if (!video.isConnected) { io.disconnect(); clipVideos.delete(video); }
  }
}

/* ---- clip replay charts -----------------------------------------------------------
 * Each clip card draws the chat rate over its window and replays it with the video:
 * x is seconds of video (chat time minus clip.window_start), the rate line is solid
 * up to the playhead and faint after it. Kept apart from `charts`, which tick() slides
 * along wall-clock time every second.
 */
const CLIP_PAD_S = 5;            // the rates endpoint pads the window this much each side
const clipCharts = new Map();    // clip id -> { id, el, ws, len, peakId, u, ro, raf, video, w, h, drag }
const clipRates = new Map();     // clip id -> RateSample[] (successful fetches only)
const clipVideos = new Map();    // clip <video> -> IntersectionObserver (loads the rates when seen)

function offsetLabel(v) {
  const s = Math.round(Math.abs(v));
  return `${v < -0.5 ? '−' : ''}${Math.floor(s / 60)}:${pad(s % 60)}`;
}

function clipVideo(entry) {
  if (!entry.video?.isConnected) entry.video = entry.el.closest('.clip')?.querySelector('.clip-player video') || null;
  return entry.video;
}

function playheadTime(entry) {
  const v = clipVideo(entry);
  return v && Number.isFinite(v.currentTime) ? v.currentTime : 0;
}

function videoLength(entry) {
  const v = clipVideo(entry);
  return v && Number.isFinite(v.duration) && v.duration > 0 ? v.duration : entry.len;
}

function clipChartOpts(entry, width, height) {
  const ink2 = cssVar('--ink-2'), grid = cssVar('--grid');
  const font = `500 11px ${cssVar('--font-ui')}`;
  const axis = { stroke: ink2, font, grid: { stroke: grid, width: 1 }, ticks: { show: false }, size: 26, gap: 4 };
  return {
    width, height,
    padding: [10, 18, 0, 0],
    legend: { show: false },
    cursor: { y: false, points: { size: 7, width: 2, fill: cssVar('--panel') }, drag: { x: false, y: false } },
    scales: {
      // Fixed to the padded window, so x is always seconds of video whatever the data covers.
      x: { time: false, range: () => [-CLIP_PAD_S, entry.len + CLIP_PAD_S] },
      y: { range: (u, min, max) => [0, Math.max(4, Math.ceil(max * 1.15))] },
    },
    axes: [
      { ...axis, space: 56, values: (u, splits) => splits.map(offsetLabel) },
      { ...axis, size: 34, space: 24, values: (u, splits) => splits.map(v => (Number.isInteger(v) ? v : v.toFixed(1))) },
    ],
    series: [
      {},
      // The whole line, faint: the part not played yet. drawReplay() draws the played part solid.
      { label: 'Chat rate', stroke: cssVar('--trace'), width: 2, alpha: 0.22, points: { show: false } },
      { label: 'Normal', stroke: cssVar('--base-line'), width: 1.5, points: { show: false } },
      { label: 'Clip threshold', stroke: cssVar('--peak'), width: 1.25, dash: [5, 4], points: { show: false } },
    ],
    hooks: {
      drawClear: [u => drawClipBackdrop(u, entry)],
      draw: [u => drawReplay(u, entry)],
      setCursor: [u => showTip(u, offsetLabel)],
    },
  };
}

// Under the series: shade what isn't in the video, and mark the peak like the stream charts do.
function drawClipBackdrop(u, entry) {
  const ctx = u.ctx;
  const { left, top, width, height } = u.bbox;
  const dpr = devicePixelRatio || 1;
  const X = v => u.valToPos(v, 'x', true);
  ctx.save();
  ctx.beginPath(); ctx.rect(left, top, width, height); ctx.clip();
  ctx.fillStyle = cssVar('--grid');
  const x0 = X(0), x1 = X(videoLength(entry));
  ctx.fillRect(left, top, Math.max(x0 - left, 0), height);
  ctx.fillRect(x1, top, Math.max(left + width - x1, 0), height);
  const p = entry.peakId ? peaksRef.find(q => q.id === entry.peakId) : null;
  if (p) {
    const a = X(p.ts_start - entry.ws), b = X(Math.max(p.ts_end, p.ts_start + 1) - entry.ws);
    ctx.fillStyle = cssVar('--peak-band');
    ctx.fillRect(a, top, Math.max(b - a, 2 * dpr), height);
    const xp = Math.round(X(p.ts_peak - entry.ws));
    ctx.fillStyle = cssVar('--peak');
    ctx.fillRect(xp - dpr, top, 2 * dpr, height);
    const s = 5 * dpr;
    ctx.beginPath(); ctx.moveTo(xp - s, top); ctx.lineTo(xp + s, top); ctx.lineTo(xp, top + s * 1.4); ctx.closePath(); ctx.fill();
  }
  ctx.restore();
}

// Over the series: the played part of the rate line, solid, and the playhead.
function drawReplay(u, entry) {
  const ctx = u.ctx;
  const { left, top, width, height } = u.bbox;
  const dpr = devicePixelRatio || 1;
  const [xs, ys] = u.data;
  const t = playheadTime(entry);
  const xPlay = Math.min(Math.max(u.valToPos(t, 'x', true), left), left + width);
  ctx.save();
  ctx.beginPath(); ctx.rect(left, top, xPlay - left, height); ctx.clip();
  if (xs.length) {
    const line = new Path2D();
    xs.forEach((x, i) => {
      const px = u.valToPos(x, 'x', true), py = u.valToPos(ys[i], 'y', true);
      if (i) line.lineTo(px, py); else line.moveTo(px, py);
    });
    const area = new Path2D(line);
    area.lineTo(u.valToPos(xs[xs.length - 1], 'x', true), top + height);
    area.lineTo(u.valToPos(xs[0], 'x', true), top + height);
    area.closePath();
    ctx.fillStyle = cssVar('--trace-fill'); ctx.fill(area);
    ctx.setLineDash([]);  // uPlot leaves the threshold's dash set
    ctx.strokeStyle = cssVar('--trace'); ctx.lineWidth = 2 * dpr; ctx.lineJoin = 'round'; ctx.stroke(line);
  }
  ctx.restore();

  // The playhead, with a dot where it meets the rate line.
  ctx.save();
  ctx.fillStyle = cssVar('--ink');
  ctx.fillRect(Math.round(xPlay - dpr / 2), top, Math.max(1, Math.round(1.5 * dpr)), height);
  let j = 0;
  while (j < xs.length && xs[j] < t) j++;
  if (j > 0 && j < xs.length) {
    const f = (t - xs[j - 1]) / (xs[j] - xs[j - 1] || 1);
    const y = ys[j - 1] + (ys[j] - ys[j - 1]) * f;
    ctx.beginPath(); ctx.arc(xPlay, u.valToPos(y, 'y', true), 4 * dpr, 0, 2 * Math.PI);
    ctx.fillStyle = cssVar('--trace'); ctx.fill();
    ctx.lineWidth = 2 * dpr; ctx.strokeStyle = cssVar('--panel'); ctx.stroke();
  }
  ctx.restore();
  // Where the playhead was drawn, in CSS pixels (the browser test reads these).
  entry.el.dataset.playhead = t.toFixed(3);
  entry.el.dataset.playheadX = (xPlay / dpr).toFixed(1);
}

function clipChartData(entry) {
  const list = clipRates.get(entry.id) || [];
  return [list.map(r => r.ts - entry.ws), list.map(r => r.rate), list.map(r => r.baseline), list.map(r => r.threshold)];
}

function buildClipChart(entry) {
  const el = entry.el;
  if (!clipRates.get(entry.id)?.length || !el.clientWidth) return;
  const w = el.clientWidth, h = Math.max(el.clientHeight, 80);
  entry.w = w; entry.h = h;
  entry.u?.destroy();
  el.innerHTML = '';
  el.parentElement.querySelector('.chart-tip')?.remove();
  const tip = document.createElement('div');
  tip.className = 'chart-tip'; tip.hidden = true;
  el.parentElement.appendChild(tip);
  const u = new uPlot(clipChartOpts(entry, w, h), clipChartData(entry), el);
  entry.u = u;
  // Click or drag on the chart to seek the video.
  const seek = e => {
    const v = clipVideo(entry);
    if (!v) return;
    const max = videoLength(entry);
    v.currentTime = Math.min(Math.max(u.posToVal(e.offsetX, 'x'), 0), Math.max(max - 0.05, 0));
    u.redraw(false);
  };
  u.over.addEventListener('pointerdown', e => {
    if (e.button !== 0) return;
    entry.drag = true; u.over.setPointerCapture(e.pointerId); seek(e);
  });
  u.over.addEventListener('pointermove', e => { if (entry.drag) seek(e); });
  const end = () => { entry.drag = false; };
  u.over.addEventListener('pointerup', end);
  u.over.addEventListener('pointercancel', end);
  el.addEventListener('mouseleave', () => { tip.hidden = true; });
  const v = clipVideo(entry);
  if (v && !v.paused) startReplay(entry.id);
}

function redrawClip(id) { clipCharts.get(id)?.u?.redraw(false); }

// Follow the video every frame while it plays; the loop ends itself once it stops.
function startReplay(id) {
  const entry = clipCharts.get(id);
  if (!entry || entry.raf) return;
  const step = () => {
    entry.raf = 0;
    if (!entry.el.isConnected) return;
    entry.u?.redraw(false);
    const v = clipVideo(entry);
    if (v && v.isConnected && !v.paused && !v.ended) entry.raf = requestAnimationFrame(step);
  };
  entry.raf = requestAnimationFrame(step);
}

function dropClipChart(id) {
  const entry = clipCharts.get(id);
  if (!entry) return;
  cancelAnimationFrame(entry.raf);
  entry.ro.disconnect(); entry.u?.destroy();
  clipCharts.delete(id);
}

/* ---- the Alpine component -------------------------------------------------------- */
document.addEventListener('alpine:init', () => {
  Alpine.data('dash', () => ({
    views: [
      { id: 'streams', label: 'Streams' }, { id: 'clips', label: 'Clips' }, { id: 'accounts', label: 'Accounts' },
      { id: 'debug', label: 'Debug' }, { id: 'settings', label: 'Settings' },
    ],
    clipTabs: [
      { id: 'review', label: 'To review' }, { id: 'queued', label: 'Queued' },
      { id: 'uploaded', label: 'Uploaded' }, { id: 'failed', label: 'Failed and dropped' },
    ],
    layouts: [{ id: 'blur_fit', label: 'Blur fit' }, { id: 'crop', label: 'Crop' }, { id: 'stacked', label: 'Stacked' }],
    // Detection sliders. Common ones show in the Detection card; `adv` ones in the
    // folded "Advanced detection" card. All of them work globally or per streamer.
    detFields: [
      { key: 'multiplier', label: 'Times normal', min: 1, max: 10, step: 0.1, unit: '×', help: 'A peak needs chat at this many times its normal rate.' },
      { key: 'z_threshold', label: 'Unusualness (z)', min: 0, max: 12, step: 0.1, unit: '', help: 'How far above normal, in standard deviations.' },
      { key: 'min_msgs_per_sec', label: 'Busiest second, at least', min: 0, max: 30, step: 0.5, unit: 'msg/s', help: 'Ignores spikes in very quiet chats.' },
      { key: 'min_unique_chatters', label: 'Different chatters, at least', min: 0, max: 60, step: 1, unit: '', help: 'Stops one person spamming from counting.' },
      { key: 'min_sustain_s', label: 'Chat must stay up for', min: 0, max: 60, step: 1, unit: 's', help: 'Shorter bursts are one-off spikes and are not clipped.' },
      { key: 'cooldown_s', label: 'Wait between peaks', min: 0, max: 600, step: 5, unit: 's', help: 'No new peak this soon after the last one.' },
      { key: 'preroll_s', label: 'Keep before chat rose', min: 0, max: 60, step: 1, unit: 's', help: 'Seconds of video kept before chat started rising.' },
      { key: 'max_after_peak_s', label: 'Keep after the busiest second', min: 2, max: 60, step: 1, unit: 's', help: 'The clip ends at most this long after the peak.' },
      { key: 'max_clip_s', label: 'Longest clip', min: 10, max: 180, step: 5, unit: 's', help: 'A clip never runs longer than this.' },
      { key: 'manual_preroll_s', label: 'Clip now: before the press', min: 0, max: 60, step: 1, unit: 's', help: 'Seconds kept before you pressed Clip now.' },
      { key: 'end_ratio', adv: true, label: 'Peak ends below', min: 1, max: 5, step: 0.1, unit: '× normal', help: 'When chat falls under this, the peak is over.' },
      { key: 'postroll_s', adv: true, label: 'Keep after chat fell back', min: 0, max: 30, step: 1, unit: 's', help: 'Seconds kept after the peak ended (within the limit above).' },
      { key: 'manual_postroll_s', adv: true, label: 'Clip now: after the press', min: 0, max: 60, step: 1, unit: 's', help: 'Seconds kept after you pressed Clip now.' },
      { key: 'smooth_s', adv: true, label: 'Smoothing', min: 1, max: 30, step: 1, unit: 's', help: 'Averages the rate over this many seconds.' },
      { key: 'baseline_window_s', adv: true, label: 'Normal is measured over', min: 60, max: 1800, step: 30, unit: 's', help: 'How far back "normal chat" looks. Shorter follows chat faster.' },
      { key: 'recent_baseline_s', adv: true, label: 'Quick lift of normal', min: 0, max: 600, step: 10, unit: 's', help: 'If chat over this long is busier, that becomes normal (0 = off).' },
      { key: 'bin_s', adv: true, label: 'Bin size', min: 0.5, max: 5, step: 0.5, unit: 's', help: 'Chat is counted in bins this long. Changing it restarts detection.' },
    ],
    // Global settings in the folded "Advanced: recording, rendering and app" card.
    sysFields: [
      { sec: 'capture', key: 'buffer_minutes', type: 'range', label: 'Video buffer', min: 1, max: 15, step: 0.5, unit: 'min', help: 'How far back a clip can reach. About 26 MB of disk per minute per stream.' },
      { sec: 'capture', key: 'snapshot_interval_s', type: 'range', label: 'Snapshot every', min: 1, max: 30, step: 1, unit: 's', help: 'How often the stream cards get a new still frame.' },
      { sec: 'capture', key: 'quality', type: 'text', label: 'Stream quality', help: 'streamlink quality preference, best first, e.g. 720p60,720p,best.' },
      { sec: 'capture', key: 'low_latency', type: 'bool', label: 'Low latency', help: 'Twitch low-latency mode: smaller, steadier chat-to-video offset.' },
      { sec: 'capture', key: 'filter_ads', type: 'bool', label: 'Filter ads', help: 'Drop ad segments (clips overlapping an ad gap are dropped).' },
      { sec: 'twitch', key: 'live_check_interval_s', type: 'range', label: 'Live check every', min: 5, max: 300, step: 5, unit: 's', help: 'How often every streamer is checked for being live.' },
      { sec: 'general', key: 'retention_days', type: 'range', label: 'Keep clip files for', min: 0, max: 60, step: 1, unit: 'days', help: '0 keeps everything.' },
      { sec: 'general', key: 'max_concurrent_renders', type: 'range', label: 'Renders at once', min: 1, max: 4, step: 1, unit: '', help: 'More is faster but uses more CPU.' },
      { sec: 'render', key: 'default_layout', type: 'select', label: 'Default layout', options: ['blur_fit', 'crop', 'stacked'], help: 'For streamers without their own layout.' },
      { sec: 'render', key: 'fit_zoom', type: 'range', label: 'Blur fit zoom', min: 0.5, max: 3, step: 0.05, unit: '×', help: 'Above 1 enlarges the picture and trims the sides.' },
      { sec: 'render', key: 'burn_title', type: 'bool', label: 'Burn in the streamer name', help: 'Draw the channel name on the video.' },
      { sec: 'tiktok', key: 'headless', type: 'bool', label: 'Hide the upload browser', help: 'Off shows the browser window while uploading.' },
      { sec: 'tiktok', key: 'browser_channel', type: 'select', options: ['chromium', 'chrome', 'msedge'], label: 'Upload browser', help: 'chrome or msedge need that browser installed.' },
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
    accounts: null,     // GET /api/accounts rows, null until loaded
    upload: null,       // GET /api/upload
    accountsApi: 'unknown',  // unknown | ok | missing (404: this argus has no accounts API)

    // ui state
    selected: null,
    busy: {},
    drafts: {},
    layoutPick: {},
    noPreview: {},
    clipRateState: {}, // clip id -> loading | ok | empty | error
    clipTab: 'review',
    toasts: [],
    confirmRemove: null,
    newStreamer: '',
    addError: '',
    limits: { dirty: false },
    maxStreamers: 0,
    groupsOpen: { unwatched: false, offline: false },
    confirmDelete: null,
    newAcct: { name: '', max_uploads_per_day: 6, min_minutes_between_uploads: 45, browser_profile_dir: '' },
    acctError: '',
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
        const g = JSON.parse(localStorage.getItem('lw.groups') || 'null');
        if (g) this.groupsOpen = { ...this.groupsOpen, ...g };
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
        if (v === 'accounts' || v === 'clips') this.loadUploadState();
      });
      this.$watch('clipTab', t => { if (t === 'queued') this.loadUploadState(); });
      this.$watch('groupsOpen', g => { try { localStorage.setItem('lw.groups', JSON.stringify(g)); } catch { /* ignore */ } }, { deep: true });
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
        // Its own loader: an old server without the accounts API must not blank the page.
        if (this.accountsApi === 'missing') this.accountsApi = 'unknown';
        if (this.view === 'accounts' || this.view === 'clips') this.loadUploadState();
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
      this.maxStreamers = config.general.max_streamers ?? 0;
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
        case 'clip': this.upsertClip(data); this.soonUploadState(); break;
        case 'stream_status': {
          const s = this.streamers.find(x => x.name === data.streamer);
          if (s) s.status = data;
          break;
        }
        case 'snapshot': this.onSnapshot(data.streamer, data.ts); break;
        case 'config_changed':
          api('GET', '/config').then(c => this.setConfig(c)).catch(() => {});
          if (data.patch && ('streamers' in data.patch || 'general' in data.patch || 'tiktok' in data.patch)) this.refreshState();
          if (data.patch && 'tiktok' in data.patch) this.soonUploadState();
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
      const list = this.recorded;  // the cards on screen, in their order
      if (!list.length) return;
      e.preventDefault();
      const i = list.findIndex(s => s.name === this.selected);
      const step = e.key === 'ArrowRight' ? 1 : -1;
      const next = i < 0 ? (step > 0 ? 0 : list.length - 1) : (i + step + list.length) % list.length;
      this.select(list[next].name);
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
    // Recorded (enabled, live, within max_streamers) first, by viewers; then live but not
    // recorded; then everything else. Unknown viewer counts sort last.
    streamGroup(s) {
      if (!s.enabled || !s.status.live) return 'offline';
      return s.status.watched === false ? 'unwatched' : 'recorded';
    },
    byViewers(list) {
      return list.slice().sort((a, b) => (b.status.viewers ?? -1) - (a.status.viewers ?? -1) || a.name.localeCompare(b.name));
    },
    get recorded() { return this.byViewers(this.streamers.filter(s => this.streamGroup(s) === 'recorded')); },
    get unwatched() { return this.byViewers(this.streamers.filter(s => this.streamGroup(s) === 'unwatched')); },
    get offline() {
      // Watched ones first, then the ones turned off, each alphabetical.
      return this.streamers.filter(s => this.streamGroup(s) === 'offline')
        .sort((a, b) => (b.enabled - a.enabled) || a.name.localeCompare(b.name));
    },
    liveSummary() {
      if (!this.loaded) return 'Loading…';
      const watched = this.streamers.filter(s => s.enabled);
      if (!watched.length) return 'Nobody is being watched';
      const rec = this.recorded.length, unw = this.unwatched.length;
      const live = rec + unw;
      let text = `${live} of ${watched.length} live`;
      if (unw) text += `, ${rec} recorded`;
      return text;
    },
    fmtViewers(n) {
      if (n == null) return '';
      if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1).replace(/\.0$/, '')}M`;
      if (n >= 1e3) return `${(n / 1e3).toFixed(n >= 1e5 ? 0 : 1).replace(/\.0$/, '')}K`;
      return String(n);
    },
    viewersText(s) {
      const n = s.status.viewers;
      return n == null ? '' : `${this.fmtViewers(n)} viewer${n === 1 ? '' : 's'}`;
    },
    offlineNote(s) {
      if (!s.enabled) return 'turned off in Settings';
      if (s.status.recorder_state === 'error') return s.status.last_error || 'recorder error';
      return 'offline';
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
      if (s.status.watched === false) return "Not recorded (over the max streamers limit), so it can't be clipped.";
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

    attachLive(video, name, sound = false) {
      const src = `/api/streamers/${encodeURIComponent(name)}/live/live.m3u8`;
      // Stream cards are always muted; the focus view plays sound unless muted by hand.
      video.muted = sound ? this.focusMuted : true;
      if (sound) focusVideoEl = video;
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
        video.play?.().catch(() => {
          // Sound needs a user gesture first; fall back to muted and show the button.
          if (sound && !video.muted) { video.muted = true; this.focusMuted = true; video.play().catch(() => {}); }
        });
      };
      const stop = () => { entry.hls?.destroy(); entry.hls = null; video.pause(); };
      // Re-sorting by viewers moves cards, and moving a <video> pauses it: start it again.
      video.addEventListener('pause', () => {
        if (entry.hls) setTimeout(() => { if (entry.hls && video.isConnected && video.paused) video.play().catch(() => {}); }, 0);
      });
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
      for (const e of clipCharts.values()) {
        if (e.el.clientWidth && e.u) e.u.setSize({ width: e.el.clientWidth, height: e.el.clientHeight });
        else buildClipChart(e);
      }
    },
    rebuildCharts() {
      for (const e of charts.values()) if (e.el.clientWidth) buildChart(e);
      for (const e of clipCharts.values()) buildClipChart(e);
    },

    /* ---- clip replay charts -------------------------------------------------- */
    mountClipChart(el, c) {
      dropClipChart(c.id);
      const entry = {
        id: c.id, el, ws: c.window_start, len: Math.max(c.window_end - c.window_start, 1), peakId: c.peak_id,
        u: null, ro: null, raf: 0, video: null, w: 0, h: 0, drag: false,
      };
      entry.ro = new ResizeObserver(() => {
        const w = el.clientWidth, h = el.clientHeight;
        if (!w || !h || (w === entry.w && h === entry.h)) return;
        if (entry.u) { entry.w = w; entry.h = h; entry.u.setSize({ width: w, height: h }); } else buildClipChart(entry);
      });
      entry.ro.observe(el);
      clipCharts.set(c.id, entry);
      if (clipRates.has(c.id)) this.$nextTick(() => buildClipChart(entry));
    },

    // Wires a clip's <video> to its chart, and fetches the chart's rates once the player is seen.
    bindClipVideo(video, c) {
      const id = c.id;
      video.addEventListener('play', () => {
        for (const other of document.querySelectorAll('.clip-player video')) if (other !== video && !other.paused) other.pause();
        startReplay(id);
      });
      for (const ev of ['pause', 'ended', 'seeking', 'seeked', 'timeupdate', 'loadedmetadata', 'durationchange']) {
        video.addEventListener(ev, () => redrawClip(id));
      }
      const io = new IntersectionObserver(([e]) => { if (e.isIntersecting) this.loadClipRates(id); }, { threshold: 0.05 });
      io.observe(video);
      clipVideos.set(video, io);
    },

    async loadClipRates(id) {
      if (clipRates.has(id) || this.clipRateState[id] === 'loading') return;
      this.clipRateState[id] = 'loading';
      try {
        const list = await api('GET', `/clips/${encodeURIComponent(id)}/rates`);
        clipRates.set(id, Array.isArray(list) ? list : []);
        this.clipRateState[id] = clipRates.get(id).length ? 'ok' : 'empty';
        this.$nextTick(() => { const e = clipCharts.get(id); if (e) buildClipChart(e); });
      } catch {
        this.clipRateState[id] = 'error';  // not cached: the next time the player is seen tries again
      }
    },

    replayText(c) {
      if (!this.hasRender(c) || this.noPreview[c.id]) return 'No chat data for this clip';
      const st = this.clipRateState[c.id];
      if (st === 'empty') return 'No chat data for this clip';
      if (st === 'error') return "Couldn't load the chat data";
      if (st === 'ok') return '';
      return 'Loading chat data…';
    },

    /* ---- clips ------------------------------------------------------------ */
    purge: { open: false, busy: false, older_than_h: 0, groups: { review: false, uploaded: true, failed: true } },
    purgeGroups: [
      { id: 'review', label: 'To review' }, { id: 'uploaded', label: 'Uploaded and dry runs' },
      { id: 'failed', label: 'Failed, dropped and rejected' },
    ],
    confirmDeleteClip: null,
    ignoreDraft: null,  // the ignored-chatters text while it's being edited
    focusMuted: false,  // the focus view's sound; stays as you set it while switching streams
    clipSize: (() => { try { const v = +localStorage.getItem('argus.clipSize'); return v >= 25 && v <= 90 ? v : 70; } catch { return 70; } })(),
    tabStatuses: {
      review: ['pending_review'],
      queued: ['queued', 'uploading'],
      uploaded: ['uploaded', 'dry_run_ok'],
      failed: ['failed', 'dropped', 'rejected'],
    },
    get reviewCount() { return this.clips.filter(c => c.status === 'pending_review').length; },
    tabClips() {
      const st = this.tabStatuses[this.clipTab];
      const list = this.clips.filter(c => st.includes(c.status));
      if (this.clipTab === 'queued') return list.sort((a, b) => this.queueRank(a, b));
      return list.sort((a, b) => b.created_at - a.created_at);
    },
    // The upload queue's order (argus/upload/queue.py): the one uploading now, then
    // manual clips, then the highest rank halved every rank_half_life_h hours of age,
    // then the oldest.
    queuePriority(c) {
      const rank = c.rank || c.score || 0;
      const half = this.config?.tiktok?.rank_half_life_h ?? 0;
      if (!(half > 0)) return rank;
      const ageH = Math.max(0, Date.now() / 1000 - c.created_at) / 3600;
      return rank * 0.5 ** (ageH / half);
    },
    queueRank(a, b) {
      return ((b.status === 'uploading') - (a.status === 'uploading'))
        || (this.isManual(b) - this.isManual(a))
        || (this.queuePriority(b) - this.queuePriority(a))
        || (a.created_at - b.created_at);
    },
    isManual(c) { return this.peakFor(c)?.source === 'manual'; },
    scoreText(c) {
      if (this.isManual(c)) return 'manual';
      return c.score > 0 ? `${c.score.toFixed(1)}× chat` : '';
    },
    scoreTitle(c) {
      if (this.isManual(c)) return 'Clipped by hand: goes first in the upload queue';
      return 'Peak chat rate divided by normal chat. When upload slots are short, clips go by rank: '
        + 'this, how many people chatted and how much of it was laughter or shock (not votes or goodbyes), '
        + 'counting fresher clips higher.';
    },
    showAccount(c) { return !!c.account && ['uploading', 'uploaded', 'dry_run_ok', 'failed'].includes(c.status); },
    accountText(c) {
      return (c.status === 'uploaded' ? 'Posted to ' : c.status === 'dry_run_ok' ? 'Dry run on ' : c.status === 'uploading' ? 'Uploading to ' : 'Tried ') + c.account;
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
    canApprove(c) { return ['pending_review', 'dry_run_ok', 'failed', 'rejected', 'dropped'].includes(c.status) && this.hasRender(c); },
    canReject(c) { return ['pending_review', 'dry_run_ok', 'failed', 'queued', 'waiting', 'cutting', 'rendering'].includes(c.status); },
    approveLabel(c) {
      return { pending_review: 'Approve', failed: 'Retry upload', dry_run_ok: 'Queue again', rejected: 'Approve after all', dropped: 'Queue again' }[c.status] || 'Approve';
    },

    purgeStatuses(ids) { return ids.flatMap(id => this.tabStatuses[id]); },
    purgeCount(ids) {
      const st = new Set(this.purgeStatuses(ids));
      const cutoff = Date.now() / 1000 - (+this.purge.older_than_h || 0) * 3600;
      return this.clips.filter(c => st.has(c.status) && (!(+this.purge.older_than_h) || c.created_at <= cutoff)).length;
    },
    purgeSelectedCount() { return this.purgeCount(this.purgeGroups.map(g => g.id).filter(id => this.purge.groups[id])); },
    async runPurge() {
      const statuses = this.purgeStatuses(this.purgeGroups.map(g => g.id).filter(id => this.purge.groups[id]));
      if (!statuses.length) return;
      this.purge.busy = true;
      try {
        const r = await api('POST', '/clips/purge', { statuses, older_than_h: +this.purge.older_than_h || 0 });
        this.clips = await api('GET', '/clips?limit=500');
        this.toast(`Deleted ${r.clips} clips and ${r.files} files.`);
        this.purge.open = false;
      } catch (e) {
        this.toast(`Couldn't purge: ${e.message}`, 'bad');
      } finally {
        this.purge.busy = false;
      }
    },
    canDelete(c) { return !['waiting', 'cutting', 'rendering', 'queued', 'uploading'].includes(c.status); },
    async deleteClip(c) {
      this.confirmDeleteClip = null;
      try {
        await api('DELETE', `/clips/${c.id}`);
        this.clips = this.clips.filter(x => x.id !== c.id);
        this.toast(`Deleted the ${c.streamer} clip.`);
      } catch (e) {
        this.toast(`Couldn't delete the clip: ${e.message}`, 'bad');
      }
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

    /* ---- accounts and the upload queue -------------------------------------- */
    // There is no WebSocket topic for these, so they're fetched when they're on screen,
    // after clip and config events, and every few seconds while a login window is open.
    async loadUploadState() {
      if (this.accountsApi === 'missing') return;
      const one = path => api('GET', path).then(d => ({ ok: true, d }), e => ({ ok: false, e }));
      const [a, u] = await Promise.all([one('/accounts'), one('/upload')]);
      if ((!a.ok && a.e.status === 404) || (!u.ok && u.e.status === 404)) {
        this.accountsApi = 'missing';
        this.accounts = null; this.upload = null;
        clearInterval(this._loginPoll); this._loginPoll = null;
        return;
      }
      if (a.ok && Array.isArray(a.d)) { this.accounts = a.d; this.accountsApi = 'ok'; }
      if (u.ok && u.d) this.upload = u.d;
      this.watchLogins();
    },
    soonUploadState() {
      if (this.view !== 'accounts' && this.view !== 'clips') return;
      clearTimeout(this._uploadTimer);
      this._uploadTimer = setTimeout(() => this.loadUploadState(), 800);
    },
    watchLogins() {
      const open = (this.accounts || []).some(a => a.login_open);
      if (open && !this._loginPoll) this._loginPoll = setInterval(() => this.loadUploadState(), 3000);
      if (!open && this._loginPoll) { clearInterval(this._loginPoll); this._loginPoll = null; }
    },
    get implicitAccount() { return !!this.config && !(this.config.tiktok.accounts || []).length; },
    // One row per account: the config's list (or the implicit default) joined with what
    // /api/accounts knows about each.
    accountRows() {
      if (!this.config) return [];
      const t = this.config.tiktok;
      const base = this.implicitAccount
        ? [{ name: 'default', enabled: true, max_uploads_per_day: t.max_uploads_per_day, min_minutes_between_uploads: t.min_minutes_between_uploads, browser_profile_dir: t.browser_profile_dir }]
        : t.accounts;
      return base.map(a => ({ ...a, live: (this.accounts || []).find(x => x.name === a.name) || null }));
    },
    loginText(row) {
      const l = row.live;
      if (!l) return '';
      if (l.login_open) return 'Browser window open, log in there…';
      if (l.logged_in === true) return '✓ Logged in';
      if (l.logged_in === false) return '✗ Logged out';
      return 'Not checked';
    },
    loginClass(row) {
      const l = row.live;
      if (!l) return '';
      return l.login_open ? 'open' : l.logged_in === true ? 'good' : l.logged_in === false ? 'bad' : 'unknown';
    },
    slotText(row) {
      const l = row.live;
      if (!l) return '';
      if (!row.enabled) return 'Turned off';
      const nf = l.next_free_ts;
      if (nf != null && nf > this.now) return `Next slot ${this.fmtIn(nf)}`;
      if (nf == null && l.uploads_24h >= l.max_uploads_per_day) return 'No slot today';
      return 'Free now';
    },
    accountList() { return JSON.parse(JSON.stringify(this.config.tiktok.accounts || [])); },
    async saveAccounts(list, okText) {
      const cfg = await this.patchConfig({ tiktok: { accounts: list } }, okText);
      if (cfg) this.loadUploadState();
      return cfg;
    },
    async setAccountEnabled(name, on) {
      const list = this.accountList();
      const a = list.find(x => x.name === name);
      if (!a) return;
      a.enabled = on;
      await this.saveAccounts(list, on ? `${name} is posted to again.` : `${name} won't be posted to.`);
    },
    // f: the row's own form ({ max_uploads_per_day, min_minutes_between_uploads, dirty }).
    async saveAccountLimits(row, f) {
      const limits = { max_uploads_per_day: +f.max_uploads_per_day, min_minutes_between_uploads: +f.min_minutes_between_uploads };
      let cfg;
      if (this.implicitAccount) {
        cfg = await this.patchConfig({ tiktok: limits }, 'Limits saved.');
        if (cfg) this.loadUploadState();
      } else {
        const list = this.accountList();
        const a = list.find(x => x.name === row.name);
        if (!a) return;
        Object.assign(a, limits);
        cfg = await this.saveAccounts(list, `Limits for ${row.name} saved.`);
      }
      if (cfg) f.dirty = false;
    },
    async addAccount() {
      const n = this.newAcct;
      const name = n.name.trim();
      this.acctError = '';
      if (!/^[A-Za-z0-9_.-]+$/.test(name)) { this.acctError = 'A name is letters, digits, dots, dashes or underscores, with no spaces.'; return; }
      const list = this.accountList();
      if (list.some(a => a.name.toLowerCase() === name.toLowerCase())) { this.acctError = `There is already an account called ${name}.`; return; }
      const acct = { name, enabled: true, max_uploads_per_day: +n.max_uploads_per_day, min_minutes_between_uploads: +n.min_minutes_between_uploads };
      if (n.browser_profile_dir.trim()) acct.browser_profile_dir = n.browser_profile_dir.trim();
      list.push(acct);
      if (await this.saveAccounts(list, `Added ${name}. Log in to it before it can post.`)) {
        this.newAcct = { name: '', max_uploads_per_day: 6, min_minutes_between_uploads: 45, browser_profile_dir: '' };
      }
    },
    async deleteAccount(name) {
      this.confirmDelete = null;
      await this.saveAccounts(this.accountList().filter(a => a.name !== name), `Removed ${name}. Its profile folder is still on disk.`);
    },
    setCycle(on) {
      this.config.tiktok.cycle_accounts = on;
      this.patchConfig({ tiktok: { cycle_accounts: on } }, on ? 'Cycling: each upload goes to the next account in turn.' : 'Not cycling: uploads go to the first account with a free slot.')
        .then(cfg => { if (cfg) this.loadUploadState(); });
    },
    async login(name) {
      this.busy['login:' + name] = true;
      try {
        await api('POST', `/accounts/${encodeURIComponent(name)}/login`);
        this.toast(`A browser window opened for ${name}. Log in to TikTok there, then close it.`);
      } catch (e) {
        if (e.status === 409) this.toast(`A login window for ${name} is already open.`);
        else { this.toast(`Couldn't open the login window: ${e.message}`, 'bad'); return; }
      } finally {
        this.busy['login:' + name] = false;
      }
      const row = (this.accounts || []).find(a => a.name === name);
      if (row) row.login_open = true;  // the next poll confirms it
      this.watchLogins();
      this.loadUploadState();
    },
    async checkLogin(name) {
      this.busy['check:' + name] = true;
      try {
        const r = await api('POST', `/accounts/${encodeURIComponent(name)}/check`);
        const row = (this.accounts || []).find(a => a.name === name);
        if (row) row.logged_in = r.logged_in;
        this.toast(r.logged_in ? `${name} is logged in.` : `${name} is logged out. Use Log in.`, r.logged_in ? 'ok' : 'bad');
      } catch (e) {
        this.toast(`Couldn't check ${name}: ${e.message}`, 'bad');
      } finally {
        this.busy['check:' + name] = false;
      }
    },
    uploadSummary() {
      const u = this.upload;
      if (!u) return '';
      const n = u.queued;
      return `${n} clip${n === 1 ? '' : 's'} queued` + (u.next_account ? `, next goes to ${u.next_account}` : '');
    },

    /* ---- config ------------------------------------------------------------ */
    get autoUpload() { return !!this.config?.general.auto_upload; },
    toggleFocusSound() {
      this.focusMuted = !this.focusMuted;
      const v = focusVideoEl;
      if (v && v.isConnected) { v.muted = this.focusMuted; v.play().catch(() => {}); }
    },

    // Divider between a clip and its graph: drag to set the (page-wide) clip size.
    startClipDrag(ev) {
      if (ev.button !== 0) return;
      const handle = ev.currentTarget;
      const player = handle.parentElement.querySelector('.clip-player');
      if (!player) return;
      ev.preventDefault();
      const x0 = ev.clientX, w0 = player.getBoundingClientRect().width, s0 = this.clipSize;
      handle.setPointerCapture(ev.pointerId);
      handle.classList.add('dragging');
      document.body.classList.add('resizing-clips');
      // The player's width is proportional to the size, so scale the size by the width.
      const move = e => { if (w0 > 0) this.setClipSize(s0 * (w0 + e.clientX - x0) / w0, false); };
      const end = () => {
        handle.removeEventListener('pointermove', move);
        handle.removeEventListener('pointerup', end);
        handle.removeEventListener('pointercancel', end);
        handle.classList.remove('dragging');
        document.body.classList.remove('resizing-clips');
        this.setClipSize(this.clipSize);  // remember it
      };
      handle.addEventListener('pointermove', move);
      handle.addEventListener('pointerup', end);
      handle.addEventListener('pointercancel', end);
    },

    setClipSize(v, save = true) {
      this.clipSize = Math.min(90, Math.max(25, Math.round(v)));
      if (!save) return;
      try { localStorage.setItem('argus.clipSize', String(this.clipSize)); } catch { /* private window: not remembered */ }
    },
    get autoClip() { return !!this.config && this.config.general.auto_clip !== false; },

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

    setAutoClip(on) {
      this.config.general.auto_clip = on;  // move the switch at once; the reply confirms it
      this.patchConfig({ general: { auto_clip: on } },
        on ? 'Auto-clip is on: peaks are clipped.' : 'Auto-clip is off: peaks are logged, only Clip now makes clips.');
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
    sysValue(f) { return this.config?.[f.sec]?.[f.key]; },
    setSys(f, value) {
      if (!this.config) return;
      this.config[f.sec][f.key] = value;  // move the control at once; the reply confirms it
      clearTimeout((this._sysTimers ||= {})[f.key]);
      this._sysTimers[f.key] = setTimeout(() => this.patchConfig({ [f.sec]: { [f.key]: value } }), f.type === 'range' ? 300 : 0);
    },
    detIgnoreText() {
      const v = this.detValue('ignore_users');
      return Array.isArray(v) ? v.join(', ') : '';
    },
    setDetIgnore(text) {
      this.setDet('ignore_users', text.split(/[\s,]+/).map(x => x.trim().toLowerCase()).filter(Boolean));
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
        category_captions: JSON.parse(JSON.stringify(t.category_captions || [])),
        max_queue_age_h: t.max_queue_age_h ?? 6,
        dirty: false,
      };
    },
    async saveMaxStreamers() {
      const n = Math.max(0, Math.round(+this.maxStreamers || 0));
      this.maxStreamers = n;
      if (n === this.config?.general.max_streamers) return;
      await this.patchConfig({ general: { max_streamers: n } }, n ? `Recording at most ${n} streamers at once.` : 'Recording every live streamer.');
    },
    async saveLimits() {
      const { dirty, ...tiktok } = this.limits;
      tiktok.max_queue_age_h = +tiktok.max_queue_age_h;
      // rows left half-empty are dropped; the list replaces the saved one
      tiktok.category_captions = (tiktok.category_captions || [])
        .map(r => ({ category: (r.category || '').trim(), caption: (r.caption || '').trim() }))
        .filter(r => r.category && r.caption);
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
      this.$nextTick(() => document.querySelector(`.clip[data-id="${CSS.escape(c.id)}"]`)?.scrollIntoView({ block: 'start' }));
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
    fmtIn(ts) {
      const s = ts - this.now;
      if (s < 60) return 'in under a minute';
      if (s < 3600) return `in ${Math.round(s / 60)} min`;
      const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
      return m ? `in ${h} h ${m} min` : `in ${h} h`;
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
