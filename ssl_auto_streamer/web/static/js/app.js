/**
 * app.js — Main application logic, WebSocket handling, UI updates
 */

// ===== State =====
let fieldRenderer = null;
let wsClient = null;
let lastState = null;
const soundManager = new SoundManager();
let streamingActive = false;

// ===== Init =====
document.addEventListener('DOMContentLoaded', () => {
  // Tab switching
  document.getElementById('tab-dashboard').addEventListener('click', () => showTab('dashboard'));
  document.getElementById('tab-settings').addEventListener('click', () => showTab('settings'));

  // Streaming control
  const streamingBtn = document.getElementById('streaming-btn');
  streamingBtn.addEventListener('click', async () => {
    streamingBtn.disabled = true;
    try {
      const endpoint = streamingActive ? '/api/streaming/stop' : '/api/streaming/start';
      await fetch(endpoint, { method: 'POST' });
    } catch (e) {
      console.error('Streaming control error:', e);
    } finally {
      streamingBtn.disabled = false;
    }
  });

  // Settings form
  document.getElementById('config-apply-btn').addEventListener('click', applyConfig);

  // Field canvas
  const canvas = document.getElementById('field-canvas');
  fieldRenderer = new FieldRenderer(canvas);

  // Sound toggle button
  const soundBtn = document.getElementById('sound-toggle-btn');
  soundBtn.addEventListener('click', () => {
    const enabled = soundManager.toggle();
    soundBtn.textContent = enabled ? '🔊 効果音' : '🔇 効果音';
    soundBtn.classList.toggle('active', enabled);
    if (enabled) soundManager.warmup();
  });
  soundBtn.classList.add('active');

  // User text input
  const textInput = document.getElementById('user-text-input');
  const sendBtn = document.getElementById('send-text-btn');

  function sendUserText() {
    const text = textInput.value.trim();
    if (!text) return;
    wsClient.send({ type: 'user_text', text });
    textInput.value = '';
  }

  textInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); sendUserText(); }
  });
  sendBtn.addEventListener('click', sendUserText);

  // Push-to-Talk
  let audioCapture = null;
  let pttStopping = false;
  const pttIndicator = document.getElementById('ptt-indicator');
  const pttLabel = document.getElementById('ptt-label');

  function startPTT() {
    if (audioCapture || pttStopping) return;
    audioCapture = new AudioInputCapture({
      onAudioChunk: (b64) => wsClient.send({ type: 'audio_chunk', data: b64 }),
    });
    audioCapture.start();
    pttIndicator.className = 'ptt-recording';
    pttLabel.textContent = '🔴 録音中...';
  }

  async function stopPTT() {
    if (!audioCapture || pttStopping) return;
    pttStopping = true;
    pttIndicator.className = 'ptt-processing';
    pttLabel.textContent = '⏳ 処理中...';

    const capture = audioCapture;
    audioCapture = null;
    await capture.stop();  // 1.5秒間無音をストリーミングしてから完了

    wsClient.send({ type: 'audio_end' });
    pttIndicator.className = 'ptt-idle';
    pttLabel.textContent = '🎤 スペースキー長押しで音声入力';
    pttStopping = false;
  }

  document.addEventListener('keydown', (e) => {
    if (e.code !== 'Space' || e.repeat) return;
    const tag = document.activeElement && document.activeElement.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    e.preventDefault();
    startPTT();
  });

  document.addEventListener('keyup', (e) => {
    if (e.code === 'Space') stopPTT();
  });

  // Connect WebSocket
  wsClient = createWSClient({
    onOpen:    () => setWSStatus('connected', '接続中'),
    onClose:   () => setWSStatus('error', '切断'),
    onError:   () => setWSStatus('error', 'エラー'),
    onMessage: (evt) => {
      try {
        handleMessage(JSON.parse(evt.data));
      } catch (e) {
        console.error('WS parse error:', e);
      }
    },
  });
  wsClient.connect();
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

function setWSStatus(cls, text) {
  document.getElementById('ws-dot').className = cls;
  document.getElementById('ws-label').textContent = text;
}

// ===== Message Handling =====
function handleMessage(msg) {
  if (msg.type === 'state') {
    lastState = msg;
    updateDashboard(msg);
  } else if (msg.type === 'event') {
    appendEventLog(msg);
    soundManager.playForEvent(msg.event_type);
  } else if (msg.type === 'commentary') {
    appendCommentary(msg);
  }
}

function updateDashboard(state) {
  updateScoreboard(state.game_state, state.status);
  updateStatusIndicators(state.status);
  updateStreamingControl(state.status);
  updateField(state);
  renderEventLog(state.event_log || []);
  renderCommentaryHistory(state.commentary_history || []);
}

function updateStreamingControl(status) {
  if (!status) return;
  const active = !!status.streaming;
  if (active === streamingActive) return;
  streamingActive = active;

  const btn = document.getElementById('streaming-btn');
  const label = document.getElementById('streaming-status');
  if (active) {
    btn.textContent = '⏹ 実況停止';
    btn.classList.remove('btn-start');
    btn.classList.add('btn-stop');
    label.textContent = '実況中';
    label.className = 'streaming-status-label active';
  } else {
    btn.textContent = '▶ 実況開始';
    btn.classList.remove('btn-stop');
    btn.classList.add('btn-start');
    label.textContent = '停止中';
    label.className = 'streaming-status-label';
  }

  // Dim user input panel when not streaming
  const userInputPanel = document.getElementById('user-input-panel');
  if (userInputPanel) {
    userInputPanel.style.opacity = active ? '' : '0.5';
    userInputPanel.style.pointerEvents = active ? '' : 'none';
  }
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
  renderLogList('event-log-list', events, ev => {
    const label = ev.event_type === 'USER_TEXT' ? '💬 テキスト入力'
                : ev.event_type === 'USER_AUDIO' ? '🎤 音声入力'
                : ev.event_type;
    const text = ev.data && ev.data.text
      ? `<span class="log-event-detail">${escapeHtml(ev.data.text)}</span>`
      : '';
    return `<span class="event-type ${ev.event_type}">${label}</span>${text}`;
  });
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
