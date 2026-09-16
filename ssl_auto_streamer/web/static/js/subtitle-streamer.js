/**
 * subtitle-streamer.js — Smooth streaming typewriter subtitles synchronized with speech.
 */

class SubtitleStreamer {
  /**
   * @param {Object} options
   * @param {number} [options.charIntervalMs=35] Base typing interval per character (ms)
   * @param {number} [options.hideDelayMs=6500] Delay before hiding after typing completes (ms)
   * @param {number} [options.turnTimeoutMs=3500] Fallback interval to treat next chunk as a new turn (ms)
   * @param {Function} [options.onUpdate] Callback (text, isTyping) when displayed text changes
   * @param {Function} [options.onShow] Callback when subtitles should become visible
   * @param {Function} [options.onHide] Callback when subtitles should be hidden
   */
  constructor(options = {}) {
    this._charIntervalMs = options.charIntervalMs || 35;
    this._hideDelayMs = options.hideDelayMs || 6500;
    this._turnTimeoutMs = options.turnTimeoutMs || 3500;
    this._onUpdate = options.onUpdate || (() => {});
    this._onShow = options.onShow || (() => {});
    this._onHide = options.onHide || (() => {});

    this._currentTurnId = null;
    this._lastChunkTime = 0;
    this._targetText = '';
    this._displayedText = '';
    this._typingTimer = null;
    this._hideTimer = null;
    this._isTurnComplete = false;
  }

  /**
   * Receive a transcription text chunk from backend.
   * @param {string} chunk - Text segment
   * @param {string} [turnId] - Optional unique identifier for the turn
   */
  appendChunk(chunk, turnId) {
    if (!chunk) return;

    const now = performance.now();
    const isNewTurn = (turnId && turnId !== this._currentTurnId) ||
                      (!turnId && (now - this._lastChunkTime > this._turnTimeoutMs));

    if (isNewTurn) {
      this._startNewTurn(turnId, chunk);
    } else {
      this._targetText += chunk;
      this._isTurnComplete = false;
    }

    this._lastChunkTime = now;
    if (this._hideTimer) {
      clearTimeout(this._hideTimer);
      this._hideTimer = null;
    }

    this._onShow();
    this._ensureTypingLoop();
  }

  /**
   * Called when turn is marked complete by the server.
   * @param {string} [turnId]
   */
  completeTurn(turnId) {
    if (!turnId || turnId === this._currentTurnId) {
      this._isTurnComplete = true;
      this._checkCompletionAndScheduleHide();
    }
  }

  /**
   * Clear subtitles immediately.
   */
  clear() {
    this._stopTypingLoop();
    if (this._hideTimer) {
      clearTimeout(this._hideTimer);
      this._hideTimer = null;
    }
    this._targetText = '';
    this._displayedText = '';
    this._currentTurnId = null;
    this._onUpdate('', false);
    this._onHide();
  }

  _startNewTurn(turnId, initialChunk) {
    this._currentTurnId = turnId || `turn_${Date.now()}`;
    this._isTurnComplete = false;
    this._targetText = initialChunk;
    this._displayedText = '';
    this._onUpdate('', true);
  }

  _ensureTypingLoop() {
    if (this._typingTimer !== null) return;
    this._tick();
  }

  _stopTypingLoop() {
    if (this._typingTimer !== null) {
      clearTimeout(this._typingTimer);
      this._typingTimer = null;
    }
  }

  _tick() {
    this._typingTimer = null;

    if (this._displayedText.length < this._targetText.length) {
      const remaining = this._targetText.length - this._displayedText.length;
      // Adaptive speed: catch up if buffer has grown significantly
      const step = remaining > 25 ? 3 : (remaining > 12 ? 2 : 1);
      const nextIndex = Math.min(this._targetText.length, this._displayedText.length + step);
      this._displayedText = this._targetText.slice(0, nextIndex);
      this._onUpdate(this._displayedText, true);

      const delay = remaining > 15 ? Math.max(12, this._charIntervalMs - 15) : this._charIntervalMs;
      this._typingTimer = setTimeout(() => this._tick(), delay);
    } else {
      this._onUpdate(this._displayedText, false);
      this._checkCompletionAndScheduleHide();
    }
  }

  _checkCompletionAndScheduleHide() {
    if (this._displayedText.length >= this._targetText.length) {
      if (!this._hideTimer) {
        this._hideTimer = setTimeout(() => {
          this._onHide();
        }, this._hideDelayMs);
      }
    }
  }
}

// Export for browser
window.SubtitleStreamer = SubtitleStreamer;
