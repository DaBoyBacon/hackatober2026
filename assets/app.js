// Drink Refill Monitor — dashboard
// Talks to python/main.py over the WebUI brick's socket (see libs/arduino.js).

const $ = (sel) => document.querySelector(sel);

const STATE_LABEL = {
  setup: 'Setting up',
  no_cup: 'No cup',
  ok: 'Plenty left',
  low: 'Running low',
  empty: 'Empty',
  lifted: 'Cup lifted',
  fault: 'Sensor problem',
};

const HISTORY_SECONDS = 15 * 60;
const CUP_TOP = 26, CUP_BOTTOM = 188;   // inside of the cup in the SVG, in viewBox units

let settings = {};
let status = null;
let history = [];        // [t, liquid_g|null, state]
let lastHistoryT = 0;
let lastFrac = 0;

const ui = new WebUI();

// ------------------------------------------------------------- socket ---
ui.on_connect(() => {
  setConn(true);
  ui.send_message('get_initial_state');
});
ui.on_disconnect(() => setConn(false));

ui.on_message('status', (s) => { status = s; renderStatus(); recordHistory(s); });
ui.on_message('settings', (s) => { settings = s; renderSettings(); renderSteps(); });
ui.on_message('history', (h) => { history = h || []; lastHistoryT = history.length ? history[history.length - 1][0] : 0; drawSpark(); });
ui.on_message('alert', showAlert);
ui.on_message('toast', (t) => toast(t.text, t.kind));

function setConn(on) {
  const el = $('#conn');
  el.dataset.on = String(on);
  el.querySelector('span').textContent = on ? 'Live' : 'Disconnected';
  if (!on) $('#message').textContent = 'Lost connection to the board — retrying…';
}

// -------------------------------------------------------------- hero ---
function fmtG(g) { return g == null ? '—' : `${Math.round(g)} g`; }

function renderStatus() {
  const s = status;
  document.body.dataset.state = s.state;
  $('#state-pill').textContent = STATE_LABEL[s.state] || s.state;
  $('#message').textContent = s.message;

  const showLevel = ['ok', 'low', 'empty', 'lifted'].includes(s.state) && s.percent != null;
  $('#pct').textContent = showLevel ? s.percent : '—';
  $('#liquid-g').textContent = showLevel ? fmtG(s.liquid_g) : '—';
  $('#full-g').textContent = showLevel ? fmtG(s.full_liquid_g) : '—';
  $('#scale-g').textContent = settings.scale_calibrated ? fmtG(s.grams_on_scale) : '—';
  $('#raw').textContent = s.raw == null ? '—' : Math.round(s.raw).toLocaleString();
  $('#steady').textContent = s.state === 'setup' || s.state === 'fault'
    ? (s.stable ? 'Reading steady' : 'Waiting for a steady reading…')
    : (s.snoozed ? 'Reminders snoozed' : '');

  // Liquid height. While the cup is lifted, keep showing the last level.
  let frac = lastFrac;
  if (['ok', 'low', 'empty'].includes(s.state)) frac = (s.percent ?? 0) / 100;
  else if (s.state !== 'lifted') frac = 0;
  lastFrac = frac;
  const y = CUP_BOTTOM - frac * (CUP_BOTTOM - CUP_TOP - 8) + (frac === 0 ? 6 : 0);
  $('#liquid').style.transform = `translateY(${y}px)`;

  if (s.state !== 'empty' && $('#alert-banner').dataset.kind === 'empty') hideBanner();
  renderSteps();
}

// ------------------------------------------------------------- setup ---
function renderSteps() {
  const tareDone = settings.tare_offset != null;
  const calDone = !!settings.scale_calibrated;
  const cupDone = !!settings.cup_calibrated;
  const steps = [['#step-tare', tareDone], ['#step-cal', calDone], ['#step-cup', cupDone]];
  let currentMarked = false;
  for (const [sel, done] of steps) {
    const li = $(sel);
    li.classList.toggle('done', done);
    const isCurrent = !done && !currentMarked;
    li.classList.toggle('current', isCurrent);
    if (isCurrent) currentMarked = true;
  }
  $('#step-cal button').disabled = !tareDone;
  $('#step-cup button').disabled = !calDone;
  if (cupDone && settings.cup_weight_g != null) {
    $('#step-cup p').textContent = `Saved: ${Math.round(settings.cup_weight_g)} g. Using a different cup? Set it again.`;
  }
}

// ---------------------------------------------------------- settings ---
const form = $('#settings');

function renderSettings() {
  if (form.contains(document.activeElement)) return;   // don't clobber typing
  for (const el of form.elements) {
    if (el.name && settings[el.name] != null) el.value = settings[el.name];
  }
}

form.addEventListener('submit', (e) => {
  e.preventDefault();
  const data = {};
  for (const el of form.elements) if (el.name) data[el.name] = el.value;
  ui.send_message('save_settings', data);
});

document.querySelectorAll('[data-cmd]').forEach((btn) =>
  btn.addEventListener('click', () => { unlockAudio(); ui.send_message(btn.dataset.cmd); }));

$('#cal-btn').addEventListener('click', () => {
  const grams = parseFloat($('#known-g').value);
  if (!(grams > 0)) return toast('Enter the known weight in grams first.', 'error');
  ui.send_message('calibrate', { grams });
});

$('#reset-btn').addEventListener('click', () => {
  if (confirm('Clear the scale and cup calibration?')) ui.send_message('reset_calibration');
});

$('#snooze-btn').addEventListener('click', () => { ui.send_message('snooze'); hideBanner(); });

// ------------------------------------------------------------ alerts ---
let bannerTimer = null;

function showAlert(a) {
  const banner = $('#alert-banner');
  banner.dataset.kind = a.type;
  $('#alert-title').textContent = a.title;
  $('#alert-text').textContent = a.message;
  $('#snooze-btn').hidden = !(a.type === 'empty' || a.type === 'reminder');
  banner.hidden = false;
  clearTimeout(bannerTimer);
  if (a.type === 'refilled' || a.type === 'low' || a.test) {
    bannerTimer = setTimeout(hideBanner, 6000);
  }
  if ((a.type === 'empty' || a.type === 'reminder') && $('#sound-toggle').checked) chime();
  if (a.type === 'empty' || a.type === 'reminder') flashTitle(a.title);
}

function hideBanner() { $('#alert-banner').hidden = true; $('#alert-banner').dataset.kind = ''; }

let titleTimer = null;
function flashTitle(text) {
  clearInterval(titleTimer);
  const base = 'Refill Monitor';
  let on = false, n = 0;
  titleTimer = setInterval(() => {
    document.title = (on = !on) ? `🔴 ${text}` : base;
    if (++n > 20 || document.body.dataset.state !== 'empty') { clearInterval(titleTimer); document.title = base; }
  }, 1000);
}

// Browsers only allow sound after the page has been clicked once.
let audioCtx = null;
function unlockAudio() {
  if (!audioCtx) {
    try { audioCtx = new (window.AudioContext || window.webkitAudioContext)(); } catch { /* no audio */ }
  }
  audioCtx?.resume?.();
}
document.addEventListener('pointerdown', unlockAudio, { once: true });

function chime() {
  if (!audioCtx) return;
  const t0 = audioCtx.currentTime;
  [880, 660, 523].forEach((f, i) => {
    const o = audioCtx.createOscillator();
    const g = audioCtx.createGain();
    o.type = 'sine';
    o.frequency.value = f;
    g.gain.setValueAtTime(0.0001, t0 + i * 0.18);
    g.gain.exponentialRampToValueAtTime(0.25, t0 + i * 0.18 + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + i * 0.18 + 0.35);
    o.connect(g).connect(audioCtx.destination);
    o.start(t0 + i * 0.18);
    o.stop(t0 + i * 0.18 + 0.4);
  });
}

function toast(text, kind = 'info') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = text;
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), kind === 'error' ? 6000 : 3500);
}

// --------------------------------------------------------- sparkline ---
function recordHistory(s) {
  if (s.timestamp - lastHistoryT < 1) return;
  lastHistoryT = s.timestamp;
  const liquid = ['ok', 'low', 'empty'].includes(s.state) ? s.liquid_g : null;
  history.push([s.timestamp, liquid, s.state]);
  const cutoff = s.timestamp - HISTORY_SECONDS;
  while (history.length && history[0][0] < cutoff) history.shift();
  drawSpark();
}

function drawSpark() {
  const svg = $('#spark');
  const W = 600, H = 120, PAD = 6;
  const now = history.length ? history[history.length - 1][0] : Date.now() / 1000;
  const t0 = now - HISTORY_SECONDS;
  const vals = history.map((p) => p[1]).filter((v) => v != null);
  const full = status?.full_liquid_g || 0;
  const maxV = Math.max(full, ...vals, 50) * 1.1;
  const x = (t) => ((t - t0) / HISTORY_SECONDS) * W;
  const y = (v) => H - PAD - (v / maxV) * (H - 2 * PAD);

  // Split into runs wherever the cup was off the coaster.
  const runs = [];
  let run = [];
  for (const [t, v] of history) {
    if (v == null) { if (run.length) runs.push(run); run = []; continue; }
    run.push([x(t), y(v)]);
  }
  if (run.length) runs.push(run);

  let out = '';
  for (const r of runs) {
    const pts = r.map(([a, b]) => `${a.toFixed(1)},${b.toFixed(1)}`).join(' L');
    out += `<path class="area" d="M${r[0][0].toFixed(1)},${H} L${pts} L${r[r.length - 1][0].toFixed(1)},${H} Z"/>`;
    out += `<path class="line" d="M${pts}"/>`;
  }
  if (full > 0 && settings.low_percent) {
    const ly = y(full * settings.low_percent / 100).toFixed(1);
    out += `<line class="low-line" x1="0" x2="${W}" y1="${ly}" y2="${ly}"/>`;
  }
  if (!runs.length) {
    out += `<text class="empty-text" x="${W / 2}" y="${H / 2}" text-anchor="middle">No readings yet</text>`;
  }
  svg.innerHTML = out;
}

// ------------------------------------------------------------- clock ---
setInterval(() => { $('#clock').textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); }, 1000);
drawSpark();
