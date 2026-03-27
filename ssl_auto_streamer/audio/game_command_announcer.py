# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""Game Command Announcer - ゲームコマンドを即座に読み上げるためのプリ合成キャッシュ。

HALT/STOP/INPLAY_START 等の GC コマンドは、Gemini API と VOICEVOX 合成を経由すると
500ms〜2000ms の遅延が生じる。本モジュールでは起動時にフレーズをプリ合成してキャッシュし、
コマンド検出時に PCM を直接 PcmAudioOutput に渡すことで即時再生を実現する。
"""

import asyncio
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

_COMMAND_PHRASES: Dict[str, str] = {
    "HALT": "ハルトです。",
    "STOP": "ストップです。",
    "INPLAY_START": "プレー再開です。",
    "TIMEOUT": "タイムアウトです。",
    "HALF_TIME": "前半終了です。",
    "GAME_END": "試合終了です。",
}

GAME_COMMAND_TYPES = frozenset(_COMMAND_PHRASES.keys())


class GameCommandAnnouncer:
    """
    ゲームコマンドの即時アナウンスを担当するクラス。

    起動後に presynthesize() を呼ぶと、全コマンドのフレーズを VOICEVOX で
    並列合成してメモリにキャッシュする。以降は play() でキャッシュした PCM を
    即座に PcmAudioOutput に渡せる。
    """

    def __init__(self, tts: Any, audio_output: Any):
        self._tts = tts
        self._audio_output = audio_output
        self._cache: Dict[str, bytes] = {}

    async def presynthesize(self) -> None:
        """全コマンドフレーズを並列合成してキャッシュする。既キャッシュ済みならスキップ。"""
        missing = {k: v for k, v in _COMMAND_PHRASES.items() if k not in self._cache}
        if not missing:
            return

        logger.info(f"GameCommandAnnouncer: synthesizing {len(missing)} phrases...")

        async def _synth(event_type: str, phrase: str) -> None:
            try:
                pcm = await self._tts.synthesize(phrase)
                if pcm:
                    self._cache[event_type] = pcm
            except Exception as e:
                logger.warning(f"pre-synthesis failed for {event_type}: {e}")

        await asyncio.gather(*(_synth(k, v) for k, v in missing.items()))
        logger.info(f"GameCommandAnnouncer: {len(self._cache)}/{len(_COMMAND_PHRASES)} phrases cached")

    def play(self, event_type: str) -> bool:
        """プリ合成済み音声を即座に再生する。キャッシュがなければ False を返す。"""
        pcm = self._cache.get(event_type)
        if not pcm:
            return False
        self._audio_output.play(pcm)
        logger.debug(f"GameCommandAnnouncer: instant play {event_type} ({len(pcm)} bytes)")
        return True
