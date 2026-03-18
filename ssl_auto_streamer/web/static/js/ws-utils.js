/**
 * ws-utils.js — WebSocket接続の共通ユーティリティ
 */

/**
 * 自動再接続付きWebSocketクライアントを生成する。
 * @param {object} opts
 * @param {function} [opts.onOpen]    接続成功時コールバック
 * @param {function} [opts.onClose]   切断時コールバック
 * @param {function} [opts.onError]   エラー時コールバック
 * @param {function} [opts.onMessage] メッセージ受信時コールバック (MessageEvent)
 * @param {number}   [opts.retryMs=3000] 再接続間隔 (ms)
 * @returns {{ connect: function, send: function }}
 */
function createWSClient({ onOpen, onClose, onError, onMessage, retryMs = 3000 } = {}) {
  let ws = null;
  let reconnectTimer = null;

  function connect() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${proto}//${location.host}/ws`);

    ws.onopen = () => {
      if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
      onOpen?.();
    };

    ws.onclose = () => {
      reconnectTimer = setTimeout(connect, retryMs);
      onClose?.();
    };

    ws.onerror = () => {
      onError?.();
    };

    ws.onmessage = onMessage;
  }

  function send(data) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(typeof data === 'string' ? data : JSON.stringify(data));
    }
  }

  return { connect, send };
}
