/**
 * app.js — Main application logic, WebSocket handling, UI updates
 */

// ===== State =====
let fieldRenderer = null;
let wsClient = null;
let lastState = null;
const soundManager = new SoundManager();
const audioOutputPlayer = new AudioOutputPlayer();
let streamingActive = false;
let clientAudioAvailable = false;
let _lastSpeakingText = '';

const EVENT_LOG_LABELS = {
  GOAL: 'ゴール',
  SHOT: 'シュート',
  FAST_SHOT: '高速シュート',
  SAVE: 'セーブ',
  FOUL: 'ファール',
  COLLISION: '接触',
  BALL_OUT: 'ボールアウト',
  KICKOFF: 'キックオフ',
  PENALTY: 'PK',
  FREE_KICK: 'フリーキック',
  BALL_PLACEMENT: 'ボールプレイスメント',
  BALL_PLACEMENT_SUCCEEDED: '配置成功',
  BALL_PLACEMENT_FAILED: '配置失敗',
  INVALID_GOAL: 'ノーゴール',
  PENALTY_KICK_FAILED: 'PK失敗',
  NO_PROGRESS: 'ノープログレス',
  BOT_SUBSTITUTION: 'ロボット交代',
  CHALLENGE_FLAG: 'チャレンジ',
  EMERGENCY_STOP: '緊急停止',
  PREPARED: '再開準備完了',
  HALF_TIME: 'ハーフタイム',
  GAME_END: '試合終了',
  INPLAY_START: 'プレー再開',
  HALT: '一時停止',
  STOP: 'ストップ',
  TIMEOUT: 'タイムアウト',
  GAME_EVENT: 'GCイベント',
};

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
      if (!streamingActive && clientAudioAvailable && !audioOutputPlayer.isEnabled) {
        try {
          await setClientAudioEnabled(true);
        } catch (e) {
          console.error('Client audio enable error:', e);
        }
      }
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

  const clientAudioBtn = document.getElementById('client-audio-btn');
  clientAudioBtn.addEventListener('click', async () => {
    try {
      await setClientAudioEnabled(!audioOutputPlayer.isEnabled);
    } catch (e) {
      console.error('Client audio toggle error:', e);
    }
  });

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

  // Overlay controls (embedded)
  document.getElementById('toggle-hud').addEventListener('change', (e) => {
    wsClient.send({ type: 'overlay_control', action: 'show_hud', value: e.target.checked });
  });

  document.getElementById('toggle-sound').addEventListener('change', (e) => {
    wsClient.send({ type: 'overlay_control', action: 'sound_enabled', value: e.target.checked });
  });

  document.getElementById('toggle-field').addEventListener('change', (e) => {
    wsClient.send({ type: 'overlay_control', action: 'show_field', value: e.target.checked });
  });

  document.getElementById('send-ticker-btn').addEventListener('click', () => {
    const input = document.getElementById('ticker-text');
    const text = input.value.trim();
    if (!text) return;
    wsClient.send({ type: 'overlay_control', action: 'manual_ticker', text, duration: 5000 });
    input.value = '';
  });

  // Connect WebSocket
  wsClient = createWSClient({
    onOpen:    () => {
      setWSStatus('connected', '接続中');
      if (audioOutputPlayer.isEnabled) {
        wsClient.send({ type: 'audio_output_subscribe', enabled: true });
      }
    },
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

async function setClientAudioEnabled(enabled) {
  if (enabled && !clientAudioAvailable) return;

  if (enabled) {
    await audioOutputPlayer.enable();
  } else {
    audioOutputPlayer.disable();
  }

  if (wsClient) {
    wsClient.send({ type: 'audio_output_subscribe', enabled });
  }
  updateClientAudioUI();
}

function updateClientAudioAvailability(status) {
  const mode = status.audio_output_mode || 'server';
  const available = mode === 'client' || mode === 'both';
  clientAudioAvailable = available;

  const section = document.getElementById('client-audio-section');
  if (section) section.classList.toggle('hidden', !available);

  if (!available && audioOutputPlayer.isEnabled) {
    audioOutputPlayer.disable();
    if (wsClient) wsClient.send({ type: 'audio_output_subscribe', enabled: false });
  }

  updateClientAudioUI();
}

function updateClientAudioUI() {
  const btn = document.getElementById('client-audio-btn');
  const label = document.getElementById('client-audio-label');
  const status = document.getElementById('client-audio-status');
  if (!btn || !label || !status) return;

  const enabled = audioOutputPlayer.isEnabled;
  btn.classList.toggle('active', enabled);
  btn.disabled = !clientAudioAvailable;
  label.textContent = enabled ? 'ブラウザ音声ON' : 'ブラウザ音声';
  status.textContent = enabled ? '有効' : '無効';
  status.classList.toggle('active', enabled);
}

// ===== Message Handling =====
function handleMessage(msg) {
  if (msg.type === 'state') {
    lastState = msg;
    updateDashboard(msg);
  } else if (msg.type === 'event') {
    flashEventPanel();
    soundManager.playForEvent(msg.event_type);
  } else if (msg.type === 'commentary') {
    appendCommentary(msg);
  } else if (msg.type === 'transcription') {
    updateTranscription(msg.text);
  } else if (msg.type === 'output_audio') {
    audioOutputPlayer.enqueue(msg);
  } else if (msg.type === 'output_audio_control') {
    if (msg.action === 'clear') audioOutputPlayer.clear();
  } else if (msg.type === 'audio_output_status') {
    updateClientAudioUI();
  }
}

function updateDashboard(state) {
  window._lastTeamInfo = state.team_info || {};
  updateTeamNames(state.team_info);
  updateScoreboard(state.game_state, state.status);
  updateGameStatePanel(state.game_state);
  updateStatusIndicators(state.status);
  updateStreamingControl(state.status);
  updateField(state);
  renderEventLog(state.event_log || []);
  renderCommentaryHistory(state.commentary_history || []);
}

function updateTeamNames(teamInfo) {
  if (!teamInfo) return;
  const blue = teamInfo.blue || {};
  const yellow = teamInfo.yellow || {};
  const blueEl = document.getElementById('team-name-blue');
  const yellowEl = document.getElementById('team-name-yellow');
  if (blueEl && blue.name) blueEl.textContent = blue.name + ' (Blue)';
  if (yellowEl && yellow.name) yellowEl.textContent = yellow.name + ' (Yellow)';
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
  document.getElementById('score-blue').textContent = score.blue ?? '-';
  document.getElementById('score-yellow').textContent = score.yellow ?? '-';

  const mins = gs.elapsed_minutes ?? 0;
  const m = Math.floor(mins);
  const s = Math.floor((mins - m) * 60);
  document.getElementById('elapsed-time').textContent =
    `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;

  document.getElementById('play-situation').textContent =
    gs.play_situation_detail || gs.play_situation || '-';

  const momentum = gs.momentum || 'NEUTRAL';
  const momentumEl = document.getElementById('momentum');
  const teamNames = window._lastTeamInfo || {};
  const blueName = (teamNames.blue && teamNames.blue.name) || '青';
  const yellowName = (teamNames.yellow && teamNames.yellow.name) || '黄';
  momentumEl.textContent = momentum === 'BLUE' ? `${blueName}優勢` :
    momentum === 'YELLOW' ? `${yellowName}優勢` : 'イーブン';
  momentumEl.className = 'momentum-' + momentum.toLowerCase();
}

function updateGameStatePanel(gs) {
  if (!gs) return;

  const mins = gs.elapsed_minutes ?? 0;
  const m = Math.floor(mins);
  const s = Math.floor((mins - m) * 60);
  const timeText = `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  const momentum = gs.momentum || 'NEUTRAL';
  const teamNames = window._lastTeamInfo || {};
  const blueName = (teamNames.blue && teamNames.blue.name) || '青';
  const yellowName = (teamNames.yellow && teamNames.yellow.name) || '黄';
  const momentumText = momentum === 'BLUE' ? `${blueName}優勢` :
    momentum === 'YELLOW' ? `${yellowName}優勢` : 'イーブン';

  setText('state-command', gs.play_situation_detail || gs.play_situation || '-');
  setText('state-time', timeText);
  setText('state-momentum', momentumText);
  setText('state-events-count', String((gs.recent_events || []).length));

  const placement = gs.ball_placement || {};
  const placementEl = document.getElementById('placement-state');
  if (placementEl) placementEl.classList.toggle('active', !!placement.active);

  if (!placement.active) {
    setText('placement-summary', 'なし');
    setText('placement-target', '-');
    setText('placement-next-command', placement.next_command || '-');
    setText('placement-time-left', formatSeconds(placement.time_remaining_sec));
    return;
  }

  const team = placement.team;
  const teamLabel = team === 'blue' ? blueName :
    team === 'yellow' ? yellowName : team || '-';
  setText('placement-summary', teamLabel);
  setText('placement-target', formatPoint(placement.target_position));
  setText('placement-next-command', placement.next_command || '-');
  setText('placement-time-left', formatSeconds(placement.time_remaining_sec));
}

function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

function formatPoint(point) {
  if (!point) return '-';
  const x = Number(point.x);
  const y = Number(point.y);
  if (!Number.isFinite(x) || !Number.isFinite(y)) return '-';
  return `${x.toFixed(2)}, ${y.toFixed(2)} m`;
}

function formatSeconds(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '-';
  return `${n.toFixed(1)} s`;
}

// ===== Status Indicators =====
function updateStatusIndicators(status) {
  if (!status) return;
  setStatusDot('status-gemini', status.gemini_connected, 'Gemini API');
  setStatusDot('status-tracker', status.tracker_receiving, 'Vision Tracker');
  setStatusDot('status-gc', status.gc_receiving, 'Game Controller');
  updateClientAudioAvailability(status);
  if (typeof updatePortStatusUI === 'function') {
    updatePortStatusUI(status.port_status);
  }
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
  const snap = state.field_snapshot ? { ...state.field_snapshot } : {};
  // Attach trajectory to ball_trail from ball data
  if (state.ball && state.ball.trajectory) {
    snap.ball_trail = state.ball.trajectory.map(p => p.position);
  }
  fieldRenderer.draw(snap, state.game_state);
}

// ===== Log Rendering (shared helper with change detection) =====
const _logCache = {};

function renderLogList(listId, items, renderContent) {
  const list = document.getElementById(listId);
  if (!list) return;
  const lastTs = items.length > 0 ? items[items.length - 1].timestamp : 0;
  const key = items.length + ':' + lastTs;
  if (_logCache[listId] === key) return;
  _logCache[listId] = key;
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
                : EVENT_LOG_LABELS[ev.event_type] || ev.event_type;
    const detail = formatEventLogDetail(ev);
    return `<span class="event-type ${ev.event_type}">${label}</span>${detail}`;
  });
}

function formatEventLogDetail(ev) {
  const data = ev.data || {};
  if (data.text) {
    return `<span class="log-event-detail">${escapeHtml(data.text)}</span>`;
  }

  const meta = data.metadata || {};
  const parts = [];
  if (meta.gc_event_type) parts.push(`GC:${meta.gc_event_type}`);
  if (meta.command) parts.push(`cmd:${meta.command}`);
  if (meta.team_name || meta.by_team_name) {
    parts.push(meta.team_name || meta.by_team_name);
  } else if (meta.team || meta.by_team) {
    parts.push(meta.team || meta.by_team);
  }
  if (meta.target_position) {
    const p = meta.target_position;
    parts.push(`target(${Number(p.x).toFixed(2)}, ${Number(p.y).toFixed(2)})`);
  }
  if (parts.length === 0) return '';
  return `<span class="log-event-detail">${escapeHtml(parts.join(' / '))}</span>`;
}

function flashEventPanel() {
  const panel = document.getElementById('event-log-panel');
  if (panel) {
    panel.style.borderColor = 'var(--md-ext-blue)';
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

let _transcriptionClearTimer = null;
function updateTranscription(text) {
  const el = document.getElementById('transcription-text');
  if (el) el.textContent = text;

  const banner = document.getElementById('speaking-banner');
  const bannerText = document.getElementById('speaking-banner-text');
  if (banner && bannerText && text !== _lastSpeakingText) {
    _lastSpeakingText = text;
    bannerText.textContent = text;
    banner.classList.toggle('hidden', !text);
  }

  if (_transcriptionClearTimer) clearTimeout(_transcriptionClearTimer);
  if (text) {
    _transcriptionClearTimer = setTimeout(() => {
      const el2 = document.getElementById('transcription-text');
      if (el2) el2.textContent = '';
      const banner2 = document.getElementById('speaking-banner');
      if (banner2) banner2.classList.add('hidden');
      _lastSpeakingText = '';
    }, 8000);
  }
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
