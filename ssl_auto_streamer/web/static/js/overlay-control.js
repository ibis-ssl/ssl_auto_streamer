/**
 * overlay-control.js — オーバーレイ管理画面スクリプト
 * WebSocket経由でoverlay_controlコマンドをブロードキャスト
 */

let wsClient = null;

document.addEventListener('DOMContentLoaded', () => {
  // オーバーレイのURLを現在のホストに合わせて更新
  const link = document.getElementById('overlay-link');
  link.href = `${location.origin}/overlay`;
  link.textContent = `${location.origin}/overlay`;

  wsClient = createWSClient({
    onOpen:  () => setStatus(true),
    onClose: () => setStatus(false),
    onError: () => setStatus(false),
  });
  wsClient.connect();

  document.getElementById('toggle-hud').addEventListener('change', (e) => {
    wsClient.send({ type: 'overlay_control', action: 'show_hud', value: e.target.checked });
  });

  document.getElementById('toggle-sound').addEventListener('change', (e) => {
    wsClient.send({ type: 'overlay_control', action: 'sound_enabled', value: e.target.checked });
  });

  document.getElementById('send-ticker-btn').addEventListener('click', () => {
    const text = document.getElementById('ticker-text').value.trim();
    if (!text) return;
    wsClient.send({ type: 'overlay_control', action: 'manual_ticker', text, duration: 5000 });
    document.getElementById('ticker-text').value = '';
  });
});

function setStatus(ok) {
  document.getElementById('ws-dot-ctrl').className = ok ? 'ok' : '';
  document.getElementById('ws-label-ctrl').textContent = ok ? 'WebSocket 接続中' : '切断';
}
