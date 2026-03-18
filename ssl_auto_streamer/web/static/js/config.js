/**
 * config.js — Settings form handling
 */

async function loadConfig() {
  try {
    const res = await fetch('/api/config');
    if (!res.ok) return;
    const cfg = await res.json();
    populateForm(cfg);
  } catch (e) {
    console.error('Failed to load config:', e);
  }
}

function populateForm(cfg) {
  const ssl = cfg.ssl || {};
  const commentary = cfg.commentary || {};
  const gemini = cfg.gemini || {};
  const audio = cfg.audio || {};

  setVal('cfg-team-color', ssl.our_team_color || 'blue');
  setVal('cfg-team-name', ssl.our_team_name || '');
  setVal('cfg-tracker-addr', ssl.tracker_addr || '');
  setVal('cfg-tracker-port', ssl.tracker_port || '');
  setVal('cfg-gc-addr', ssl.gc_addr || '');
  setVal('cfg-gc-port', ssl.gc_port || '');

  setVal('cfg-silence-threshold', commentary.analyst_silence_threshold || '');
  setVal('cfg-update-rate', commentary.writer_update_rate || '');

  setVal('cfg-gemini-model', gemini.model || '');
  setVal('cfg-audio-device', audio.device || '');
}

function setVal(id, val) {
  const el = document.getElementById(id);
  if (el) el.value = val;
}

async function applyConfig() {
  const resultEl = document.getElementById('apply-result');
  resultEl.className = '';
  resultEl.style.display = 'none';

  const payload = {
    ssl: {
      our_team_color: getVal('cfg-team-color'),
      our_team_name: getVal('cfg-team-name'),
      tracker_addr: getVal('cfg-tracker-addr'),
      tracker_port: parseInt(getVal('cfg-tracker-port')) || undefined,
      gc_addr: getVal('cfg-gc-addr'),
      gc_port: parseInt(getVal('cfg-gc-port')) || undefined,
    },
    commentary: {
      analyst_silence_threshold: parseFloat(getVal('cfg-silence-threshold')) || undefined,
      writer_update_rate: parseFloat(getVal('cfg-update-rate')) || undefined,
    },
    gemini: {
      model: getVal('cfg-gemini-model'),
    },
    audio: {
      device: getVal('cfg-audio-device'),
    },
  };

  // Remove undefined values
  cleanPayload(payload);

  const apiKey = getVal('cfg-api-key');
  if (apiKey && apiKey !== '***') {
    payload.gemini.api_key = apiKey;
  }

  try {
    const res = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (data.success) {
      if (data.restart_required && data.restart_required.length > 0) {
        resultEl.textContent = `適用しました（再起動が必要な設定: ${data.restart_required.join(', ')}）`;
        resultEl.className = 'warn';
      } else {
        resultEl.textContent = '設定を適用しました';
        resultEl.className = 'success';
      }
    } else {
      resultEl.textContent = 'エラー: ' + (data.error || '不明なエラー');
      resultEl.className = 'error';
    }
    resultEl.style.display = 'block';
    setTimeout(() => { resultEl.style.display = 'none'; }, 5000);
  } catch (e) {
    resultEl.textContent = '通信エラー: ' + e.message;
    resultEl.className = 'error';
    resultEl.style.display = 'block';
  }
}

function getVal(id) {
  const el = document.getElementById(id);
  return el ? el.value.trim() : '';
}

function cleanPayload(obj) {
  for (const key of Object.keys(obj)) {
    if (obj[key] === undefined || obj[key] === null || obj[key] === '') {
      delete obj[key];
    } else if (typeof obj[key] === 'object') {
      cleanPayload(obj[key]);
    }
  }
}
