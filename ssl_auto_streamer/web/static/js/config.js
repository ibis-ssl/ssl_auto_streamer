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
    ssl: {},
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

/**
 * Port switching — called by button clicks in SSL settings panel.
 * @param {string} source  'tracker' | 'gc' | 'vision'
 * @param {number} portIndex  0 or 1
 */
async function switchPort(source, portIndex) {
  // Read the current port number from the button label
  const btn = document.getElementById(`btn-${source}-${portIndex}`);
  if (!btn) return;
  const port = parseInt(btn.textContent, 10);

  try {
    const res = await fetch('/api/ssl/switch-port', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source, port }),
    });
    const data = await res.json();
    if (!data.success) {
      console.error('Port switch failed:', data.error);
    }
  } catch (e) {
    console.error('Port switch error:', e);
  }
}

/**
 * Update port status buttons from the status payload.
 * Called with the port_status object from /api/status WebSocket messages.
 */
function updatePortStatusUI(portStatus) {
  if (!portStatus) return;

  const sources = ['vision', 'gc', 'tracker'];
  for (const src of sources) {
    const info = portStatus[src];
    if (!info) continue;

    info.ports.forEach((p, i) => {
      const btn = document.getElementById(`btn-${src}-${i}`);
      if (!btn) return;

      // Update port number label in case config changed
      btn.textContent = p.port;

      // Style: active = solid, receiving = green dot indicator
      const isActive = p.port === info.active;
      const isReceiving = p.receiving;

      btn.className = 'btn btn-port';
      if (isActive) btn.classList.add('active');
      if (isReceiving) btn.classList.add('receiving');
    });
  }
}
