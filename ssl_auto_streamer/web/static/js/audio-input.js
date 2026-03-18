/**
 * audio-input.js — マイク音声キャプチャ・PCM変換モジュール
 *
 * PTT用ストリーミング方式: 録音中はチャンク単位でコールバックを呼び出し、
 * stop() はPromiseを返す。マイクを1.5秒間継続して無音をストリーミングし、
 * Gemini VAD が「発話→無音」遷移を自然に検出できるようにする。
 */
class AudioInputCapture {
  constructor({ onAudioChunk } = {}) {
    this._onAudioChunk = onAudioChunk;
    this._stream = null;
    this._audioContext = null;
    this._processor = null;
    this._isCapturing = false;
    this._needsResample = false;
  }

  async start() {
    if (this._isCapturing) return;

    try {
      this._stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true }
      });
    } catch (e) {
      console.error('Microphone access denied:', e);
      return;
    }

    this._audioContext = new AudioContext({ sampleRate: 16000 });
    const source = this._audioContext.createMediaStreamSource(this._stream);
    const actualRate = this._audioContext.sampleRate;
    this._needsResample = actualRate !== 16000;

    this._processor = this._audioContext.createScriptProcessor(4096, 1, 1);
    this._processor.onaudioprocess = (e) => {
      if (!this._isCapturing) return;
      const inputData = e.inputBuffer.getChannelData(0);
      const pcmFloat = this._needsResample
        ? this._resample(inputData, actualRate, 16000)
        : inputData;

      const pcm16 = new Int16Array(pcmFloat.length);
      for (let i = 0; i < pcmFloat.length; i++) {
        const s = Math.max(-1, Math.min(1, pcmFloat[i]));
        pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
      }

      if (this._onAudioChunk) {
        this._onAudioChunk(this._toBase64(new Uint8Array(pcm16.buffer)));
      }
    };

    source.connect(this._processor);
    this._processor.connect(this._audioContext.destination);
    this._isCapturing = true;
  }

  /**
   * PTTリリース時に呼ぶ。マイクを1.5秒間継続して無音をストリーミングし、
   * Gemini VAD が発話終了を検出できるようにしてからクリーンアップする。
   * @returns {Promise<void>}
   */
  stop() {
    return new Promise((resolve) => {
      // マイクトラックを停止して実際の無音状態にする
      if (this._stream) {
        this._stream.getTracks().forEach(t => t.stop());
      }

      // onaudioprocess は AudioContext が生きている間継続して呼ばれる
      // 1.5秒間ストリーミングを維持してから終了
      setTimeout(() => {
        this._isCapturing = false;
        if (this._processor) { this._processor.disconnect(); this._processor = null; }
        if (this._audioContext) { this._audioContext.close(); this._audioContext = null; }
        this._stream = null;
        resolve();
      }, 1500);
    });
  }

  get isCapturing() { return this._isCapturing; }

  _resample(input, fromRate, toRate) {
    const ratio = fromRate / toRate;
    const newLen = Math.round(input.length / ratio);
    const out = new Float32Array(newLen);
    for (let i = 0; i < newLen; i++) {
      const src = i * ratio;
      const lo = Math.floor(src);
      const hi = Math.min(lo + 1, input.length - 1);
      out[i] = input[lo] * (1 - (src - lo)) + input[hi] * (src - lo);
    }
    return out;
  }

  _toBase64(bytes) {
    let binary = '';
    const CHUNK = 8192;
    for (let i = 0; i < bytes.length; i += CHUNK) {
      binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
    }
    return btoa(binary);
  }
}
