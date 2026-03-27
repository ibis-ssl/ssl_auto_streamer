/**
 * sounds.js — Web Audio API によるブラウザ効果音マネージャ
 * 外部音声ファイル不要。合成音のみ使用。
 */

class SoundManager {
  constructor() {
    this._ctx = null;
    this._enabled = true;
  }

  enable()   { this._enabled = true; }
  disable()  { this._enabled = false; }
  toggle()   { this._enabled = !this._enabled; return this._enabled; }
  isEnabled() { return this._enabled; }

  /** ユーザー操作後に呼び出してAudioContextを初期化する（自動再生ポリシー対策） */
  warmup() { this._ctx_ensure(); }

  /** イベント種別に応じた音を再生 */
  playForEvent(eventType) {
    if (!this._enabled) return;
    switch (eventType) {
      case 'GOAL':            this._playGoal(); break;
      case 'SHOT':
      case 'FAST_SHOT':       this._playShot(); break;
      case 'FOUL':            this._playFoul(); break;
      case 'HALF_TIME':       this._playWhistle(1); break;
      case 'GAME_END':        this._playWhistle(3); break;
      case 'INPLAY_START':    this._playBeep(880, 0.1); break;
      case 'SAVE':            this._playBeep(660, 0.08); break;
    }
  }

  _ctx_ensure() {
    if (!this._ctx) {
      this._ctx = new (window.AudioContext || window.webkitAudioContext)();
    }
    if (this._ctx.state === 'suspended') {
      this._ctx.resume();
    }
    return this._ctx;
  }

  /** OscillatorNode + GainNode を生成し destination へ接続して返す */
  _tone(type = 'sine') {
    const ctx = this._ctx_ensure();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.type = type;
    return { ctx, osc, gain };
  }

  /** ゴール: 上昇する3音 */
  _playGoal() {
    const freqs = [523, 659, 784]; // C5, E5, G5
    freqs.forEach((freq, i) => {
      const { ctx, osc, gain } = this._tone('sine');
      osc.frequency.value = freq;
      const t = ctx.currentTime + i * 0.12;
      gain.gain.setValueAtTime(0, t);
      gain.gain.linearRampToValueAtTime(0.4, t + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.001, t + 0.5);
      osc.start(t);
      osc.stop(t + 0.5);
    });
  }

  /** シュート: 短い打撃音 */
  _playShot() {
    const { ctx, osc, gain } = this._tone('square');
    osc.frequency.setValueAtTime(300, ctx.currentTime);
    osc.frequency.exponentialRampToValueAtTime(80, ctx.currentTime + 0.1);
    gain.gain.setValueAtTime(0.2, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.15);
    osc.start(ctx.currentTime);
    osc.stop(ctx.currentTime + 0.15);
  }

  /** ファール: 短い警告ブザー */
  _playFoul() {
    for (let i = 0; i < 2; i++) {
      const { ctx, osc, gain } = this._tone('sawtooth');
      osc.frequency.value = 440;
      const t = ctx.currentTime + i * 0.18;
      gain.gain.setValueAtTime(0.15, t);
      gain.gain.exponentialRampToValueAtTime(0.001, t + 0.14);
      osc.start(t);
      osc.stop(t + 0.14);
    }
  }

  /**
   * ホイッスル: 滑らかに減衰する正弦波
   * @param {number} count 回数
   */
  _playWhistle(count) {
    for (let i = 0; i < count; i++) {
      const { ctx, osc, gain } = this._tone('sine');
      osc.frequency.value = 880;
      const t = ctx.currentTime + i * 0.55;
      gain.gain.setValueAtTime(0.3, t);
      gain.gain.exponentialRampToValueAtTime(0.001, t + 0.5);
      osc.start(t);
      osc.stop(t + 0.5);
    }
  }

  /** 単発ビープ */
  _playBeep(freq, duration) {
    const { ctx, osc, gain } = this._tone('sine');
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(0.2, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + duration);
    osc.start(ctx.currentTime);
    osc.stop(ctx.currentTime + duration);
  }
}
