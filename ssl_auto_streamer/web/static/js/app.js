/**
 * app.js — Main application logic, WebSocket handling, UI updates
 */

// ===== State =====
let fieldRenderer = null;
let ws = null;
let wsReconnectTimer = null;
let lastState = null;

// ===== Init =====
document.addEventListener('DOMContentLoaded', () => {
  // Tab switching
  document.getElementById('tab-dashboard').addEventListener('click', () => showTab('dashboard'));
  document.getElementById('tab-settings').addEventListener('click', () => showTab('settings'));

  // Settings form
  document.getElementById('config-apply-btn').addEventListener('click', applyConfig);

  // Field canvas
  const canvas = document.getElementById('field-canvas');
  fieldRenderer = new FieldRenderer(canvas);

  // Connect WebSocket
  connectWS();
});

function showTab(name) {
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
  document.getElementById(name).classList.add('active');
  document.getElementById('tab-' + name).classList.add('active');

  if (name === 'settings') {
    loadConfig();
  }
}

// ===== WebSocket =====
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/ws`);

  ws.onopen = () => {
    setWSStatus('connected', '接続中');
    if (wsReconnectTimer) { clearTimeout(wsReconnectTimer); wsReconnectTimer = null; }
  };

  ws.onclose = () => {
    setWSStatus('error', '切断');
    wsReconnectTimer = setTimeout(connectWS, 3000);
  };

  ws.onerror = () => {
    setWSStatus('error', 'エラー');
  };

  ws.onmessage = (evt) => {
    try {
      const msg = JSON.parse(evt.data);
      handleMessage(msg);
    } catch (e) {
      console.error('WS parse error:', e);
    }
  };
}

function setWSStatus(cls, text) {
  const dot = document.getElementById('ws-dot');
  const label = document.getElementById('ws-label');
  dot.className = cls;
  label.textContent = text;
}

// ===== Message Handling =====
function handleMessage(msg) {
  if (msg.type === 'state') {
    lastState = msg;
    updateDashboard(msg);
  } else if (msg.type === 'event') {
    appendEventLog(msg);
  } else if (msg.type === 'commentary') {
    appendCommentary(msg);
  }
}

function updateDashboard(state) {
  updateScoreboard(state.game_state, state.status);
  updateStatusIndicators(state.status);
  updateField(state);
  renderEventLog(state.event_log || []);
  renderCommentaryHistory(state.commentary_history || []);
}

// ===== Scoreboard =====
function updateScoreboard(gs, status) {
  if (!gs) return;

  const score = gs.score || {};
  document.getElementById('score-ours').textContent = score.ours ?? '-';
  document.getElementById('score-theirs').textContent = score.theirs ?? '-';

  const mins = gs.elapsed_minutes ?? 0;
  const m = Math.floor(mins);
  const s = Math.floor((mins - m) * 60);
  document.getElementById('elapsed-time').textContent =
    `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;

  document.getElementById('play-situation').textContent =
    gs.play_situation_detail || gs.play_situation || '-';

  const momentum = gs.momentum || 'NEUTRAL';
  const momentumEl = document.getElementById('momentum');
  momentumEl.textContent = momentum === 'OURS' ? '我々優勢' :
    momentum === 'THEIRS' ? '相手優勢' : 'イーブン';
  momentumEl.className = 'momentum-' + momentum.toLowerCase();
}

// ===== Status Indicators =====
function updateStatusIndicators(status) {
  if (!status) return;
  setStatusDot('status-gemini', status.gemini_connected, 'Gemini API');
  setStatusDot('status-tracker', status.tracker_receiving, 'Vision Tracker');
  setStatusDot('status-gc', status.gc_receiving, 'Game Controller');
}

function setStatusDot(id, ok, label) {
  const item = document.getElementById(id);
  if (!item) return;
  const dot = item.querySelector('.status-dot');
  const text = item.querySelector('.status-label');
  dot.className = 'status-dot ' + (ok ? 'ok' : 'ng');
  if (text) text.textContent = label + ': ' + (ok ? '受信中' : '未接続');
}

// ===== Field =====
function updateField(state) {
  if (!fieldRenderer) return;
  const snap = state.field_snapshot || {};
  // Attach trajectory to ball_trail from ball data
  if (state.ball && state.ball.trajectory) {
    snap.ball_trail = state.ball.trajectory.map(p => p.position);
  }
  fieldRenderer.draw(snap, state.game_state);
}

// ===== Log Rendering (shared helper) =====
function renderLogList(listId, items, renderContent) {
  const list = document.getElementById(listId);
  if (!list) return;
  list.innerHTML = '';
  const reversed = [...items].reverse();
  for (const item of reversed) {
    const li = document.createElement('li');
    li.className = 'log-item';
    li.innerHTML = `<span class="log-time">${formatTime(item.timestamp)}</span>` + renderContent(item);
    list.appendChild(li);
  }
}

function renderEventLog(events) {
  renderLogList('event-log-list', events,
    ev => `<span class="event-type ${ev.event_type}">${ev.event_type}</span>`
  );
}

function appendEventLog(ev) {
  const panel = document.getElementById('event-log-panel');
  if (panel) {
    panel.style.borderColor = '#388bfd';
    setTimeout(() => { panel.style.borderColor = ''; }, 300);
  }
}

function renderCommentaryHistory(history) {
  renderLogList('commentary-list', history,
    entry => escapeHtml(entry.text || '')
  );
}

function appendCommentary(msg) {
  renderCommentaryHistory((lastState?.commentary_history || []).concat([msg]));
}

// ===== Utilities =====
function formatTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
