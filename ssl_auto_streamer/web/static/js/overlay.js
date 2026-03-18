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
  TIMEOUT:      4000,
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
  TIMEOUT:      'タイムアウト',
};

// CSS fade-out アニメーション時間 (overlay.css と合わせる)
const TICKER_FADEOUT_MS = 400;

// ===== 状態 =====
let wsClient = null;
const soundManager = new SoundManager();

// ===== 初期化 =====
document.addEventListener('DOMContentLoaded', () => {
  wsClient = createWSClient({
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
  document.addEventListener('click', () => soundManager.warmup(), { once: true });
});

// ===== メッセージ処理 =====
function handleMessage(msg) {
  if (msg.type === 'state') {
    updateHUD(msg);
  } else if (msg.type === 'event') {
    handleEvent(msg);
  } else if (msg.type === 'commentary') {
    showTicker(msg.text, 'commentary', 7000);
  } else if (msg.type === 'overlay_control') {
    applyControl(msg);
  }
}

// ===== HUD更新 =====
function updateHUD(state) {
  const gs = state.game_state || {};
  const teamInfo = state.team_info || {};
  const ours = teamInfo.ours || {};
  const theirs = teamInfo.theirs || {};

  // チーム名とスコア
  const ourColor = ours.color || 'blue';
  if (ourColor === 'blue') {
    setTeamSide('left',  ours.name   || 'Blue',   gs.score?.ours ?? '-');
    setTeamSide('right', theirs.name || 'Yellow',  gs.score?.theirs ?? '-');
  } else {
    setTeamSide('left',  theirs.name || 'Blue',    gs.score?.theirs ?? '-');
    setTeamSide('right', ours.name   || 'Yellow',  gs.score?.ours ?? '-');
  }

  // タイマー
  const mins = gs.elapsed_minutes ?? 0;
  const m = Math.floor(mins);
  const s = Math.floor((mins - m) * 60);
  document.getElementById('hud-timer').textContent =
    `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;

  // プレー状況
  document.getElementById('hud-situation').textContent =
    gs.play_situation_detail || gs.play_situation || '';
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

// ===== イベント処理 =====
function handleEvent(msg) {
  const duration = TICKER_DURATIONS[msg.event_type];
  if (!duration) return;

  let cssClass = '';
  if (msg.event_type === 'GOAL') cssClass = 'goal';
  else if (msg.event_type === 'FOUL') cssClass = 'foul';

  showTicker(EVENT_LABELS[msg.event_type] || msg.event_type, cssClass, duration);
  soundManager.playForEvent(msg.event_type);
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
  } else if (msg.action === 'manual_ticker') {
    showTicker(msg.text || '', '', msg.duration || 5000);
  }
}
