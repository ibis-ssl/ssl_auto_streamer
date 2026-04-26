/**
 * overlay.js — OBS配信オーバーレイ用スクリプト
 * WebSocketで状態を受信し、スコア・タイマー・テロップを更新する
 */

// ===== 定数 =====
// 値が存在しないイベントはテロップを表示しない
const TICKER_DURATIONS = {
  GOAL:         8000,
  SHOT:         3000,
  FAST_SHOT:    2500,
  FOUL:         4000,
  HALF_TIME:    6000,
  GAME_END:     8000,
  INPLAY_START: 2000,
  SAVE:         3000,
  HALT:         2000,
  STOP:         2000,
  KICKOFF:      3000,
  PENALTY:      4000,
  FREE_KICK:    3000,
  BALL_PLACEMENT: 4000,
  BALL_PLACEMENT_SUCCEEDED: 3000,
  BALL_PLACEMENT_FAILED: 4000,
  TIMEOUT:      4000,
  COLLISION:    3000,
  INVALID_GOAL: 5000,
  PENALTY_KICK_FAILED: 4000,
  NO_PROGRESS:  4000,
  BOT_SUBSTITUTION: 4000,
  CHALLENGE_FLAG: 4000,
  EMERGENCY_STOP: 6000,
  PREPARED:     2500,
};

const EVENT_LABELS = {
  GOAL:         'ゴール！',
  SHOT:         'シュート',
  FAST_SHOT:    '高速シュート',
  FOUL:         'ファール',
  SAVE:         'セーブ',
  HALF_TIME:    'ハーフタイム',
  GAME_END:     '試合終了',
  INPLAY_START: 'プレー再開',
  HALT:         '一時停止',
  STOP:         'ストップ',
  KICKOFF:      'キックオフ',
  PENALTY:      'ペナルティーキック',
  FREE_KICK:    'フリーキック',
  BALL_PLACEMENT: 'ボールプレイスメント',
  BALL_PLACEMENT_SUCCEEDED: '配置成功',
  BALL_PLACEMENT_FAILED: '配置失敗',
  TIMEOUT:      'タイムアウト',
  COLLISION:    '接触',
  INVALID_GOAL: 'ノーゴール',
  PENALTY_KICK_FAILED: 'PK失敗',
  NO_PROGRESS:  'ノープログレス',
  BOT_SUBSTITUTION: 'ロボット交代',
  CHALLENGE_FLAG: 'チャレンジ',
  EMERGENCY_STOP: '緊急停止',
  PREPARED:     '再開準備完了',
};

// CSS fade-out アニメーション時間 (overlay.css と合わせる)
const TICKER_FADEOUT_MS = 400;

// ===== 状態 =====
let wsClient = null;
let fieldRenderer = null;
const soundManager = new SoundManager();
const audioOutputPlayer = new AudioOutputPlayer();

// 字幕管理
let _lastSpeakingText = '';
let _subtitleTimeout = null;

// ゴール演出有効フラグ
let _celebrationEnabled = true;
let _fieldVisible = false;

// OBS側の実況音声出力
let _outputAudioEnabled = true;
let _outputAudioAvailable = false;
let _outputAudioSubscribed = false;

// ===== 初期化 =====
document.addEventListener('DOMContentLoaded', () => {
  const fieldCanvas = document.getElementById('overlay-field-canvas');
  if (fieldCanvas) {
    fieldRenderer = new FieldRenderer(fieldCanvas);
  }

  wsClient = createWSClient({
    onOpen: () => {
      _outputAudioSubscribed = false;
      updateOutputAudioSubscription();
    },
    onClose: () => {
      _outputAudioSubscribed = false;
    },
    onMessage: (evt) => {
      try {
        handleMessage(JSON.parse(evt.data));
      } catch (e) {
        console.error('WS parse error:', e);
      }
    },
  });
  wsClient.connect();

  // OBSブラウザ内でAudioContextを初期化するためのトリガー
  document.addEventListener('click', () => {
    soundManager.warmup();
    if (_outputAudioEnabled && _outputAudioAvailable) {
      enableOverlayAudio();
    }
  }, { once: true });
});

// ===== メッセージ処理 =====
function handleMessage(msg) {
  if (msg.type === 'state') {
    updateHUD(msg);
  } else if (msg.type === 'event') {
    handleEvent(msg);
  } else if (msg.type === 'commentary') {
    showTicker(msg.text, 'commentary', 7000);
  } else if (msg.type === 'transcription') {
    updateSubtitle(msg.text);
  } else if (msg.type === 'overlay_control') {
    applyControl(msg);
  } else if (msg.type === 'output_audio') {
    audioOutputPlayer.enqueue(msg);
  } else if (msg.type === 'output_audio_control') {
    if (msg.action === 'clear') audioOutputPlayer.clear();
  } else if (msg.type === 'audio_output_status') {
    _outputAudioSubscribed = !!msg.subscribed;
  }
}

// ===== HUD更新 =====
function updateHUD(state) {
  updateOutputAudioAvailability(state.status || {});
  updateFieldOverlay(state);

  const gs = state.game_state || {};
  const teamInfo = state.team_info || {};
  const blue = teamInfo.blue || {};
  const yellow = teamInfo.yellow || {};

  // チーム名とスコア (blue=左, yellow=右 固定)
  setTeamSide('left',  blue.name   || '青チーム', gs.score?.blue ?? '-');
  setTeamSide('right', yellow.name || '黄チーム', gs.score?.yellow ?? '-');

  // タイマー
  const mins = gs.elapsed_minutes ?? 0;
  const m = Math.floor(mins);
  const s = Math.floor((mins - m) * 60);
  document.getElementById('hud-timer').textContent =
    `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;

  // プレー状況
  document.getElementById('hud-situation').textContent =
    gs.play_situation_detail || gs.play_situation || '';

  // モメンタムインジケータ
  updateMomentum(gs.momentum);

  // カードバッジ
  if (state.cards) {
    updateCards('left',  state.cards.blue   || {});
    updateCards('right', state.cards.yellow || {});
  }

  // ポゼッションバーとスタッツパネル
  if (state.match_stats) {
    updatePossessionBar(state.match_stats);
    updateStatsPanel(state.match_stats);
  }

  // 字幕は transcription イベントで更新（HUD更新では変更しない）
}

function updateFieldOverlay(state) {
  if (!fieldRenderer) return;

  const snap = state.field_snapshot || {};
  if (state.ball && state.ball.trajectory) {
    snap.ball_trail = state.ball.trajectory.map(p => p.position);
  }
  fieldRenderer.draw(snap, state.game_state);
}

function updateOutputAudioAvailability(status) {
  const mode = status.audio_output_mode || 'server';
  const available = mode === 'client' || mode === 'both';
  if (_outputAudioAvailable === available) {
    updateOutputAudioSubscription();
    return;
  }

  _outputAudioAvailable = available;
  if (!available) {
    audioOutputPlayer.disable();
  }
  updateOutputAudioSubscription();
}

function enableOverlayAudio() {
  audioOutputPlayer.enable().catch((e) => {
    console.warn('Output audio enable failed:', e);
  });
}

function updateOutputAudioSubscription() {
  const shouldSubscribe = _outputAudioEnabled && _outputAudioAvailable;

  if (shouldSubscribe) {
    enableOverlayAudio();
  } else {
    audioOutputPlayer.disable();
  }

  if (!wsClient || shouldSubscribe === _outputAudioSubscribed) return;
  wsClient.send({ type: 'audio_output_subscribe', enabled: shouldSubscribe });
  _outputAudioSubscribed = shouldSubscribe;
}

function setTeamSide(side, name, score) {
  document.getElementById(`name-${side}`).textContent = name;

  const scoreEl = document.getElementById(`score-${side}`);
  const newScore = String(score);
  if (scoreEl.dataset.prev !== undefined && scoreEl.dataset.prev !== newScore && newScore !== '-') {
    scoreEl.classList.remove('flash');
    void scoreEl.offsetWidth; // reflow
    scoreEl.classList.add('flash');
  }
  scoreEl.textContent = newScore;
  scoreEl.dataset.prev = newScore;
}

// ===== モメンタムインジケータ =====
function updateMomentum(momentum) {
  const el = document.getElementById('hud-momentum');
  el.className = '';
  el.textContent = '';
  if (momentum === 'BLUE') {
    el.className = 'blue';
    el.textContent = '◀ 優勢';
  } else if (momentum === 'YELLOW') {
    el.className = 'yellow';
    el.textContent = '優勢 ▶';
  }
}

// ===== カードバッジ =====
function updateCards(side, cards) {
  const ycEl = document.getElementById(`yc-${side}`);
  const rcEl = document.getElementById(`rc-${side}`);
  const yc = cards.yellow_cards || 0;
  const rc = cards.red_cards || 0;

  ycEl.textContent = yc;
  ycEl.style.display = yc > 0 ? 'block' : 'none';
  rcEl.textContent = rc;
  rcEl.style.display = rc > 0 ? 'block' : 'none';
}

// ===== ポゼッションバー =====
function updatePossessionBar(matchStats) {
  const bluePct = matchStats.blue?.ball_possession_percent ?? 50;
  document.getElementById('possession-fill-blue').style.width = `${bluePct}%`;
}

// ===== スタッツパネル =====
function updateStatsPanel(matchStats) {
  const b = matchStats.blue || {};
  const y = matchStats.yellow || {};

  const rows = [
    { key: 'possession', bVal: `${b.ball_possession_percent ?? 50}%`, yVal: `${y.ball_possession_percent ?? 50}%`,
      bN: b.ball_possession_percent ?? 50, yN: y.ball_possession_percent ?? 50 },
    { key: 'shots',  bVal: b.shots ?? 0,  yVal: y.shots ?? 0,  bN: b.shots ?? 0,  yN: y.shots ?? 0 },
    { key: 'saves',  bVal: b.saves ?? 0,  yVal: y.saves ?? 0,  bN: b.saves ?? 0,  yN: y.saves ?? 0 },
    { key: 'passes', bVal: b.passes ?? 0, yVal: y.passes ?? 0, bN: b.passes ?? 0, yN: y.passes ?? 0 },
    { key: 'fouls',  bVal: b.fouls_committed ?? 0, yVal: y.fouls_committed ?? 0,
      bN: b.fouls_committed ?? 0, yN: y.fouls_committed ?? 0 },
  ];

  for (const row of rows) {
    const total = (row.bN + row.yN) || 1;
    const bluePct = (row.bN / total) * 100;
    const yellowPct = (row.yN / total) * 100;

    document.getElementById(`sv-${row.key}-blue`).textContent = row.bVal;
    document.getElementById(`sv-${row.key}-yellow`).textContent = row.yVal;
    document.getElementById(`sb-${row.key}-blue`).style.width = `${bluePct}%`;
    document.getElementById(`sb-${row.key}-yellow`).style.width = `${yellowPct}%`;
  }
}

// ===== 字幕 =====
function updateSubtitle(text) {
  if (text && text !== _lastSpeakingText) {
    clearTimeout(_subtitleTimeout);
    document.getElementById('subtitle-text').textContent = text;
    document.getElementById('subtitle-area').classList.remove('hidden');
    _lastSpeakingText = text;
  } else if (!text && _lastSpeakingText) {
    _subtitleTimeout = setTimeout(() => {
      document.getElementById('subtitle-area').classList.add('hidden');
      _lastSpeakingText = '';
    }, 1500);
  }
}

// ===== イベント処理 =====
function handleEvent(msg) {
  const duration = TICKER_DURATIONS[msg.event_type];
  if (!duration) return;

  let cssClass = '';
  if (msg.event_type === 'GOAL') {
    cssClass = 'goal';
    if (_celebrationEnabled) {
      triggerGoalCelebration(msg.data?.primary_robot?.team);
    }
  } else if (msg.event_type === 'FOUL') {
    cssClass = 'foul';
  }

  showTicker(EVENT_LABELS[msg.event_type] || msg.event_type, cssClass, duration);
  soundManager.playForEvent(msg.event_type);
}

// ===== ゴール演出 =====
function triggerGoalCelebration(team) {
  const el = document.getElementById('goal-celebration');
  el.classList.remove('hidden', 'flash-blue', 'flash-yellow');
  void el.offsetWidth; // reflow
  const cls = team === 'yellow' ? 'flash-yellow' : 'flash-blue';
  el.classList.add(cls);
  setTimeout(() => {
    el.classList.add('hidden');
  }, 1500);
}

// ===== テロップ =====
function showTicker(text, cssClass, durationMs) {
  const area = document.getElementById('ticker-area');
  const el = document.createElement('div');
  el.className = 'ticker' + (cssClass ? ` ${cssClass}` : '');
  el.textContent = text;
  area.appendChild(el);

  // 古いテロップを除去(最大3件)
  while (area.children.length > 3) {
    area.removeChild(area.firstChild);
  }

  // フェードアウト後に削除（animationend 未発火時のフォールバックあり）
  setTimeout(() => {
    el.classList.add('fade-out');
    const cleanup = () => el.remove();
    el.addEventListener('animationend', cleanup, { once: true });
    setTimeout(cleanup, TICKER_FADEOUT_MS + 100);
  }, durationMs);
}

// ===== オーバーレイ制御 =====
function applyControl(msg) {
  if (msg.action === 'show_hud') {
    document.getElementById('hud').classList.toggle('hidden', msg.value === false);
  } else if (msg.action === 'sound_enabled') {
    msg.value ? soundManager.enable() : soundManager.disable();
  } else if (msg.action === 'output_audio_enabled') {
    _outputAudioEnabled = msg.value !== false;
    updateOutputAudioSubscription();
  } else if (msg.action === 'manual_ticker') {
    showTicker(msg.text || '', '', msg.duration || 5000);
  } else if (msg.action === 'show_stats') {
    document.getElementById('stats-panel').classList.toggle('hidden', msg.value === false);
  } else if (msg.action === 'show_field') {
    _fieldVisible = msg.value === true;
    document.getElementById('field-overlay').classList.toggle('hidden', !_fieldVisible);
  } else if (msg.action === 'show_subtitles') {
    if (msg.value === false) {
      document.getElementById('subtitle-area').classList.add('hidden');
    }
    // 有効化は次のテキスト受信時に自動で表示される
  } else if (msg.action === 'show_possession') {
    document.getElementById('possession-bar').classList.toggle('hidden', msg.value === false);
  } else if (msg.action === 'show_celebration') {
    _celebrationEnabled = msg.value !== false;
  }
}
