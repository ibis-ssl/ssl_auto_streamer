# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""Utterance Queue - 発話の逐次処理と読み上げ管理エージェントとの連携。

fire-and-forget パターンを置き換え、TTSの順序保証と鮮度管理を実現する。
"""

import asyncio
import logging
from collections import deque
from typing import Any, Callable, Dict, List, Optional

from ssl_auto_streamer.gemini.reading_manager import ReadingManager, Utterance

logger = logging.getLogger(__name__)


class UtteranceQueue:
    """
    発話キュー + 逐次TTSワーカー。

    発話候補を蓄積し、バックログが2件以上の場合は ReadingManager に
    選別を依頼してから逐次的に合成・再生する。
    1件のみの場合は ReadingManager を呼ばずに即座に読み上げる。
    """

    def __init__(
        self,
        tts: Any,  # VoicevoxTTS
        audio_output: Any,  # PcmAudioOutput
        reading_manager: ReadingManager,
        writer: Any,  # WorldModelWriter
        max_recently_spoken: int = 5,
    ):
        self._tts = tts
        self._audio_output = audio_output
        self._reading_manager = reading_manager
        self._writer = writer

        self._pending: List[Utterance] = []
        self._pending_event = asyncio.Event()
        self._cancel_event = asyncio.Event()
        self._recently_spoken: deque = deque(maxlen=max_recently_spoken)
        self._is_synthesizing: bool = False
        self._current_speaking: Optional[Utterance] = None

        self._id_counter: int = 0
        self._on_pipeline_event: Optional[Callable[[str, Dict[str, Any]], None]] = None

        self._worker_task: Optional[asyncio.Task] = None

    @property
    def is_busy(self) -> bool:
        """発話キューが処理中（ペンディングあり or 合成中）かどうか。"""
        return bool(self._pending) or self._is_synthesizing

    def set_pipeline_callback(
        self, callback: Callable[[str, Dict[str, Any]], None]
    ) -> None:
        """パイプラインイベントのコールバックを設定する。"""
        self._on_pipeline_event = callback

    def _emit(self, event: str, data: Dict[str, Any]) -> None:
        """パイプラインイベントをコールバックに通知する。"""
        if self._on_pipeline_event:
            try:
                self._on_pipeline_event(event, data)
            except Exception as e:
                logger.warning(f"Pipeline callback error ({event}): {e}")

    def get_pipeline_snapshot(self) -> Dict[str, Any]:
        """現在のパイプライン状態のスナップショットを返す。"""
        return {
            "pending": [
                {
                    "id": u.id,
                    "text": u.text,
                    "priority": u.priority,
                    "event_type": u.event_type,
                }
                for u in self._pending
            ],
            "is_synthesizing": self._is_synthesizing,
            "is_playing": self._audio_output.is_playing,
            "current_speaking": (
                {"id": self._current_speaking.id, "text": self._current_speaking.text}
                if self._current_speaking
                else None
            ),
        }

    def start(self) -> None:
        """ワーカータスクを開始する。イベントループ起動後に呼ぶこと。"""
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker())
            logger.info("UtteranceQueue worker started")

    async def stop(self) -> None:
        """ワーカータスクを停止する。"""
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        self._worker_task = None
        logger.info("UtteranceQueue worker stopped")

    def enqueue(self, text: str, priority: int, event_type: Optional[str] = None) -> None:
        """発話候補をキューに追加する。"""
        self._id_counter += 1
        utt = Utterance(text=text, priority=priority, event_type=event_type, id=self._id_counter)
        self._pending.append(utt)
        self._pending_event.set()
        self._emit("enqueue", {"id": utt.id, "text": text, "priority": priority, "event_type": event_type})

    def interrupt(self, new_priority: int) -> None:
        """
        バージイン: new_priority より低い優先度の発話を削除し、
        現在の合成をキャンセル、PCMバッファをクリアする。
        """
        dropped_items = [u for u in self._pending if u.priority < new_priority]
        self._pending = [u for u in self._pending if u.priority >= new_priority]
        dropped = len(dropped_items)

        self._cancel_event.set()
        self._audio_output.clear_buffer()

        if dropped:
            logger.info(
                f"UtteranceQueue interrupt (priority>={new_priority}): "
                f"dropped {dropped}, kept {len(self._pending)}"
            )
        self._emit("interrupt", {
            "new_priority": new_priority,
            "dropped_count": dropped,
            "dropped": [{"id": u.id, "text": u.text} for u in dropped_items],
        })

    def clear(self) -> None:
        """全発話を破棄し、現在の合成をキャンセルする。"""
        count = len(self._pending)
        self._pending.clear()
        self._cancel_event.set()
        self._audio_output.clear_buffer()
        if count:
            logger.info(f"UtteranceQueue cleared ({count} items dropped)")
        self._emit("clear", {"dropped_count": count})

    async def _worker(self) -> None:
        """メインループ: 発話を逐次処理する。"""
        while True:
            await self._pending_event.wait()
            self._pending_event.clear()

            # 同一ループで追加された発話も拾うために一度制御を返す
            await asyncio.sleep(0)

            if not self._pending:
                continue

            candidates = list(self._pending)
            self._pending.clear()

            if len(candidates) == 1:
                selected = candidates
                self._emit("select", {"id": candidates[0].id, "text": candidates[0].text})
            else:
                game_context = self._build_game_context()
                recently = list(self._recently_spoken)
                indices = await self._reading_manager.select(
                    candidates, game_context, recently
                )
                # select() は最大 timeout_seconds かかるため、完了後にキャンセルを確認
                if self._cancel_event.is_set():
                    for u in candidates:
                        self._emit("cancel", {"id": u.id, "text": u.text})
                    continue
                selected_set = set(indices)
                selected = [candidates[i] for i in indices]
                dropped = len(candidates) - len(selected)
                if dropped:
                    logger.info(
                        f"ReadingManager dropped {dropped}/{len(candidates)} utterances"
                    )
                for i, u in enumerate(candidates):
                    if i in selected_set:
                        self._emit("select", {"id": u.id, "text": u.text})
                    else:
                        self._emit("discard", {"id": u.id, "text": u.text})

            self._cancel_event.clear()
            self._is_synthesizing = True
            try:
                for utt in selected:
                    if self._cancel_event.is_set():
                        logger.debug("UtteranceQueue: synthesis cancelled")
                        self._emit("cancel", {"id": utt.id, "text": utt.text})
                        break
                    self._current_speaking = utt
                    self._emit("speak_start", {"id": utt.id, "text": utt.text})
                    await self._synthesize_and_play(utt)
                    self._current_speaking = None
                    self._emit("speak_end", {"id": utt.id, "text": utt.text})
                    self._recently_spoken.append(utt.text)
            finally:
                self._current_speaking = None
                self._is_synthesizing = False

    async def _synthesize_and_play(self, utt: Utterance) -> None:
        """1発話を合成して再生する。実際の音声再生完了まで待機する。キャンセル可能。"""
        async for pcm_chunk in self._tts.synthesize_stream(
            utt.text, cancel_event=self._cancel_event
        ):
            self._audio_output.play(pcm_chunk)

        # 末尾の端数バイトをフラッシュし、実際の音声再生が完了するまで待機する
        if not self._cancel_event.is_set():
            self._audio_output.flush_buffer()
            await self._audio_output.wait_until_drained()

    def _build_game_context(self) -> Dict[str, Any]:
        """WorldModelWriter から試合状況の要約を構築する。"""
        try:
            ctx = self._writer.get_context()
            return {
                "score": {"blue": ctx.blue_score, "yellow": ctx.yellow_score},
                "elapsed_minutes": round(ctx.elapsed_seconds / 60.0, 1),
                "momentum": ctx.momentum,
                "recent_events": ctx.recent_events[-3:],
            }
        except Exception as e:
            logger.debug(f"Failed to build game context: {e}")
            return {}
