# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""PCM Audio Output using PyAudio (matching Google's official sample)."""

import logging
import threading
from typing import Optional

import pyaudio

logger = logging.getLogger(__name__)

# Audio format settings (from Google's official sample)
FORMAT = pyaudio.paInt16
CHANNELS = 1
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE = 2048   # frames per write (~85ms at 24000Hz) — larger = fewer write calls
BYTES_PER_FRAME = 2  # paInt16 = 2 bytes/sample


class PcmAudioOutput:
    """
    PCM Audio Output for Gemini Live API response.

    Accumulates incoming PCM chunks into a bytearray and feeds PyAudio
    in fixed-size blocks to avoid choppiness caused by variable chunk sizes.
    Call flush_buffer() on turn_complete to ensure tail audio is played.
    """

    def __init__(
        self,
        sample_rate: int = RECEIVE_SAMPLE_RATE,
        channels: int = CHANNELS,
        device: Optional[str] = None,
    ):
        self._sample_rate = sample_rate
        self._channels = channels
        self._device_index = None  # TODO: device name to index conversion

        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._flush_event = threading.Event()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._pyaudio: Optional[pyaudio.PyAudio] = None
        self._stream = None

    def start(self) -> None:
        """Start audio output thread."""
        if self._running:
            return

        try:
            self._pyaudio = pyaudio.PyAudio()
            self._stream = self._pyaudio.open(
                format=FORMAT,
                channels=self._channels,
                rate=self._sample_rate,
                output=True,
                frames_per_buffer=CHUNK_SIZE,
            )

            self._stop_event.clear()
            self._flush_event.clear()
            self._running = True
            self._thread = threading.Thread(target=self._playback_loop, daemon=True)
            self._thread.start()

            logger.info(
                f"Audio output started: {self._sample_rate}Hz, {self._channels}ch (PyAudio)"
            )
        except Exception as e:
            logger.error(f"Failed to start audio output: {e}")
            logger.warning("Audio output will be disabled.")
            if self._stream:
                self._stream.close()
                self._stream = None
            if self._pyaudio:
                self._pyaudio.terminate()
                self._pyaudio = None
            self._running = False

    def stop(self) -> None:
        """Stop audio output thread."""
        if not self._running:
            return

        self._running = False
        self._stop_event.set()

        if self._thread:
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                logger.warning("Playback thread did not terminate in time")
            self._thread = None

        if self._stream:
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None

        if self._pyaudio:
            self._pyaudio.terminate()
            self._pyaudio = None

        logger.info("Audio output stopped")

    def play(self, pcm_data: bytes) -> None:
        """Append PCM audio data to playback buffer."""
        if not self._running:
            return
        with self._lock:
            self._buffer.extend(pcm_data)

    def flush_buffer(self) -> None:
        """Signal playback thread to drain remaining bytes (call on turn_complete)."""
        self._flush_event.set()

    def clear_buffer(self) -> None:
        """Discard all buffered audio (for barge-in support)."""
        with self._lock:
            self._buffer.clear()

    def _playback_loop(self) -> None:
        """Background thread: drain buffer in fixed-size blocks."""
        block_bytes = CHUNK_SIZE * BYTES_PER_FRAME
        while not self._stop_event.is_set():
            with self._lock:
                if len(self._buffer) >= block_bytes:
                    chunk = bytes(self._buffer[:block_bytes])
                    del self._buffer[:block_bytes]
                elif self._flush_event.is_set() and self._buffer:
                    # Drain tail bytes smaller than a full block
                    chunk = bytes(self._buffer)
                    self._buffer.clear()
                    self._flush_event.clear()
                else:
                    chunk = None

            if chunk:
                try:
                    if self._stream:
                        self._stream.write(chunk, exception_on_underflow=False)
                except Exception as e:
                    logger.error(f"Playback error: {e}")
            else:
                # Wait until stop is signalled or next check interval
                self._stop_event.wait(timeout=0.005)
