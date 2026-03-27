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

// 字幕管理
let _lastSpeakingText = '';
let _subtitleTimeout = null;

// ゴール演出有効フラグ
let _celebrationEnabled = true;

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

  // 字幕 (current_speaking は {id, text} オブジェクト)
  const speaking = state.pipeline_snapshot?.current_speaking?.text || '';
  updateSubtitle(speaking);
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
  } else if (msg.action === 'manual_ticker') {
    showTicker(msg.text || '', '', msg.duration || 5000);
  } else if (msg.action === 'show_stats') {
    document.getElementById('stats-panel').classList.toggle('hidden', msg.value === false);
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
