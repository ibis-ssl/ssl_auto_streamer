# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""VOICEVOX TTS wrapper for local text-to-speech synthesis via HTTP API."""

import logging
import re
import struct
from typing import AsyncGenerator, List, Optional

import aiohttp

logger = logging.getLogger(__name__)


class VoicevoxTTS:
    """
    Local TTS using VOICEVOX Engine HTTP API.

    Converts text to 24kHz int16 PCM bytes compatible with PcmAudioOutput.
    Requires VOICEVOX Engine running locally (default: http://localhost:50021).
    """

    SAMPLE_RATE = 24000
    MAX_CHARS = 140

    def __init__(
        self,
        host: str = "http://localhost:50021",
        speaker: int = 3,
        speed_scale: float = 1.0,
    ):
        self._host = host.rstrip("/")
        self._speaker = speaker
        self._speed_scale = speed_scale
        self._session: Optional[aiohttp.ClientSession] = None
        logger.info(
            f"VoicevoxTTS initialized: host={host}, speaker={speaker}, speed={speed_scale}"
        )

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    @classmethod
    def _split_text(cls, text: str) -> List[str]:
        """テキストを MAX_CHARS 以下のチャンクに分割する。"""
        parts = re.split(r"(?<=[。！？、\n])", text)
        chunks: List[str] = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            while len(part) > cls.MAX_CHARS:
                chunks.append(part[: cls.MAX_CHARS])
                part = part[cls.MAX_CHARS :]
            if part:
                chunks.append(part)
        return chunks

    @staticmethod
    def _strip_wav_header(wav_data: bytes) -> bytes:
        """WAV ヘッダーを除去して raw PCM データを返す。"""
        if len(wav_data) < 44 or wav_data[:4] != b"RIFF":
            return wav_data
        pos = 12
        while pos < len(wav_data) - 8:
            chunk_id = wav_data[pos : pos + 4]
            chunk_size = struct.unpack_from("<I", wav_data, pos + 4)[0]
            if chunk_id == b"data":
                return wav_data[pos + 8 : pos + 8 + chunk_size]
            pos += 8 + chunk_size
        return wav_data[44:]

    async def synthesize_stream(self, text: str) -> AsyncGenerator[bytes, None]:
        """Generate PCM audio chunks from text, one chunk per sentence."""
        if not text.strip():
            return

        session = await self._get_session()
        for chunk_text in self._split_text(text):
            try:
                async with session.post(
                    f"{self._host}/audio_query",
                    params={"text": chunk_text, "speaker": self._speaker},
                ) as resp:
                    if resp.status != 200:
                        logger.error(
                            f"VOICEVOX audio_query failed: {resp.status}"
                        )
                        continue
                    query = await resp.json()

                query["speedScale"] = self._speed_scale

                async with session.post(
                    f"{self._host}/synthesis",
                    params={"speaker": self._speaker},
                    json=query,
                ) as resp:
                    if resp.status != 200:
                        logger.error(
                            f"VOICEVOX synthesis failed: {resp.status}"
                        )
                        continue
                    wav_data = await resp.read()

                pcm = self._strip_wav_header(wav_data)
                if pcm:
                    yield pcm
            except Exception as e:
                logger.error(
                    f"VoicevoxTTS synthesis error (chunk={chunk_text!r}): {e}"
                )

    async def synthesize(self, text: str) -> bytes:
        """Generate PCM audio from text as a single bytes object."""
        chunks = []
        async for chunk in self.synthesize_stream(text):
            chunks.append(chunk)
        return b"".join(chunks)
