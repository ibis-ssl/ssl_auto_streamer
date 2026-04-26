/**
 * audio-output.js — Gemini output PCM playback in the browser.
 */

class AudioOutputPlayer {
  constructor() {
    this._ctx = null;
    this._enabled = false;
    this._nextStartTime = 0;
    this._sources = new Set();
  }

  get isEnabled() {
    return this._enabled;
  }

  async enable() {
    this._enabled = true;
    const ctx = this._ensureContext();
    if (ctx.state === 'suspended') {
      await ctx.resume();
    }
    this._nextStartTime = Math.max(this._nextStartTime, ctx.currentTime + 0.06);
  }

  disable() {
    this._enabled = false;
    this.clear();
  }

  clear() {
    for (const source of this._sources) {
      try {
        source.stop();
      } catch (e) {
        // Source may already have ended.
      }
    }
    this._sources.clear();
    if (this._ctx) {
      this._nextStartTime = this._ctx.currentTime + 0.06;
    }
  }

  enqueue({ data, sample_rate: sampleRate = 24000, channels = 1 } = {}) {
    if (!this._enabled || !data) return;

    const ctx = this._ensureContext();
    if (ctx.state === 'suspended') {
      ctx.resume();
    }

    const bytes = this._fromBase64(data);
    const frames = Math.floor(bytes.byteLength / (2 * channels));
    if (frames <= 0) return;

    const buffer = ctx.createBuffer(channels, frames, sampleRate);
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);

    for (let ch = 0; ch < channels; ch++) {
      const out = buffer.getChannelData(ch);
      for (let frame = 0; frame < frames; frame++) {
        const offset = (frame * channels + ch) * 2;
        out[frame] = view.getInt16(offset, true) / 32768;
      }
    }

    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(ctx.destination);
    source.onended = () => this._sources.delete(source);

    const startAt = Math.max(ctx.currentTime + 0.02, this._nextStartTime);
    source.start(startAt);
    this._nextStartTime = startAt + buffer.duration;
    this._sources.add(source);
  }

  _ensureContext() {
    if (!this._ctx) {
      this._ctx = new (window.AudioContext || window.webkitAudioContext)();
    }
    return this._ctx;
  }

  _fromBase64(b64) {
    const binary = atob(b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) {
      bytes[i] = binary.charCodeAt(i);
    }
    return bytes;
  }
}
