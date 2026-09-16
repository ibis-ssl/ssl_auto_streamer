# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""Structured AI Activity Logger for debugging tool calls, utterances, and playback progress."""

import datetime
import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)

# PCM 24000Hz, 16-bit mono -> 48000 bytes/sec -> 48 bytes/ms
SAMPLE_RATE = 24000
BYTES_PER_SAMPLE = 2  # 16-bit
BYTES_PER_SECOND = SAMPLE_RATE * BYTES_PER_SAMPLE  # 48000
BYTES_PER_MS = BYTES_PER_SECOND / 1000.0  # 48.0


def estimate_played_text(full_text: str, completion_rate: float) -> str:
    """音声再生完了率に基づいて、どこまで発音されたかのテキストを推定する。"""
    if not full_text:
        return ""

    rate = max(0.0, min(1.0, completion_rate))
    if rate >= 0.98:
        return full_text
    if rate <= 0.05:
        return f"{full_text[:max(1, int(len(full_text) * 0.1))]}… [開始直後で中断: {int(rate * 100)}%]"

    target_len = int(len(full_text) * rate)
    target_len = max(1, min(len(full_text), target_len))

    # 句読点（、。！？）が直前（最大4文字以内）にあれば、そこで区切る
    snippet = full_text[:target_len]
    punctuation = ("、", "。", "！", "？", " ", "\n")
    for offset in range(min(5, len(snippet))):
        idx = len(snippet) - 1 - offset
        if snippet[idx] in punctuation:
            snippet = snippet[: idx + 1]
            break

    return f"{snippet}… [{int(rate * 100)}%で中断]"


@dataclass
class TurnContext:
    """1つの実況・応答ターンにおけるAIの活動状態を保持するコンテキスト。"""

    turn_id: str
    trigger_type: str  # reflex / analyst / initial_context / team_update / test
    event_type: Optional[str] = None
    priority: int = 1
    prompt_payload: Any = None
    prompt_sent_at: float = field(default_factory=time.time)

    first_audio_at: Optional[float] = None
    first_transcription_at: Optional[float] = None
    transcription_chunks: List[str] = field(default_factory=list)
    thoughts: List[str] = field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)

    audio_chunks_count: int = 0
    audio_total_bytes: int = 0
    audio_discarded_bytes: int = 0
    audio_played_bytes: int = 0

    status: str = "in_progress"  # in_progress, completed, interrupted_by_barge_in, interrupted_by_server, cancelled
    interrupt_reason: Optional[str] = None
    completed_at: Optional[float] = None
    interrupted_at: Optional[float] = None

    @property
    def full_transcription(self) -> str:
        return "".join(self.transcription_chunks).strip()

    @property
    def audio_total_duration_ms(self) -> int:
        return int(self.audio_total_bytes / BYTES_PER_MS)

    @property
    def audio_played_duration_ms(self) -> int:
        return int(self.audio_played_bytes / BYTES_PER_MS)

    @property
    def completion_rate(self) -> float:
        if self.audio_total_bytes <= 0:
            return 1.0 if self.status == "completed" else 0.0
        return max(0.0, min(1.0, self.audio_played_bytes / self.audio_total_bytes))


class AiActivityLogger:
    """AIのツールコール、思考、発言内容、音声再生進捗を記録するロガー。"""

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        log_dir: Optional[Path] = None,
    ):
        cfg = config or {}
        self._enabled = cfg.get("enabled", True)
        self._save_to_file = cfg.get("save_to_file", True)

        if log_dir is not None:
            self._log_dir = Path(log_dir)
        else:
            configured_dir = cfg.get("log_dir", "logs/ai")
            self._log_dir = Path(configured_dir)
            if not self._log_dir.is_absolute():
                self._log_dir = Path(__file__).parent.parent.parent / configured_dir

        self._lock = threading.Lock()
        self._recent_logs: Deque[Dict[str, Any]] = deque(maxlen=200)
        self._active_turns: Dict[str, TurnContext] = {}
        self._latest_turn_id: Optional[str] = None
        self._turn_seq = 0

        self._file_handle = None
        self._session_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        if self._enabled and self._save_to_file:
            self._init_log_file()

    def _init_log_file(self) -> None:
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            log_path = self._log_dir / f"ai_session_{self._session_id}.jsonl"
            self._file_handle = open(log_path, "a", encoding="utf-8")
            logger.info(f"AI Activity Logger initialized: {log_path}")
        except Exception as e:
            logger.error(f"Failed to initialize AI log file: {e}")
            self._file_handle = None

    def _write_record(self, record: Dict[str, Any]) -> None:
        """ログレコードを内部バッファおよびファイルに書き出す。"""
        record_with_ts = {
            "ts": datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(),
            **record,
        }

        with self._lock:
            self._recent_logs.append(record_with_ts)
            if self._file_handle:
                try:
                    line = json.dumps(record_with_ts, ensure_ascii=False)
                    self._file_handle.write(line + "\n")
                    self._file_handle.flush()
                except Exception as e:
                    logger.error(f"Failed to write AI log record: {e}")

    def log_session_start(
        self,
        model: str,
        voice: str,
        sample_rate: int,
        thinking_level: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Gemini Live APIセッション開始を記録する。"""
        if not self._enabled:
            return
        payload = {
            "event": "session_start",
            "session_id": self._session_id,
            "model": model,
            "voice": voice,
            "sample_rate": sample_rate,
            "thinking_level": thinking_level,
        }
        if extra:
            payload["extra"] = extra
        self._write_record(payload)

    def log_session_end(self, reason: str = "normal") -> None:
        """Gemini Live APIセッション終了を記録する。"""
        if not self._enabled:
            return
        self._write_record({
            "event": "session_end",
            "session_id": self._session_id,
            "reason": reason,
        })

    def start_turn(
        self,
        trigger_type: str,
        event_type: Optional[str] = None,
        priority: int = 1,
        payload: Any = None,
        turn_id: Optional[str] = None,
    ) -> str:
        """新規ターンを開始し、プロンプト送信イベントを記録する。"""
        if not self._enabled:
            return turn_id or f"turn_{int(time.time() * 1000)}"

        with self._lock:
            self._turn_seq += 1
            if not turn_id:
                now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                turn_id = f"turn_{now_str}_{self._turn_seq:04d}"

            ctx = TurnContext(
                turn_id=turn_id,
                trigger_type=trigger_type,
                event_type=event_type,
                priority=priority,
                prompt_payload=payload,
                prompt_sent_at=time.time(),
            )
            self._active_turns[turn_id] = ctx
            self._latest_turn_id = turn_id

        self._write_record({
            "event": "prompt_sent",
            "turn_id": turn_id,
            "trigger": trigger_type,
            "event_type": event_type,
            "priority": priority,
            "payload": payload,
        })
        return turn_id

    def get_latest_turn_id(self) -> Optional[str]:
        with self._lock:
            return self._latest_turn_id

    def get_turn(self, turn_id: str) -> Optional[TurnContext]:
        with self._lock:
            return self._active_turns.get(turn_id)

    def log_thought(self, turn_id: Optional[str], text: str) -> None:
        """思考プロセス（thought）を記録する。"""
        if not self._enabled or not text:
            return
        t_id = turn_id or self.get_latest_turn_id()
        with self._lock:
            if t_id and t_id in self._active_turns:
                self._active_turns[t_id].thoughts.append(text)

        self._write_record({
            "event": "thought",
            "turn_id": t_id,
            "thought": text,
        })

    def log_transcription(self, turn_id: Optional[str], text: str) -> None:
        """文字起こしチャンクを記録し、ターンコンテキストに蓄積する。"""
        if not self._enabled or not text:
            return
        t_id = turn_id or self.get_latest_turn_id()
        now = time.time()
        with self._lock:
            if t_id and t_id in self._active_turns:
                ctx = self._active_turns[t_id]
                if ctx.first_transcription_at is None:
                    ctx.first_transcription_at = now
                ctx.transcription_chunks.append(text)

        self._write_record({
            "event": "transcription",
            "turn_id": t_id,
            "text": text,
        })

    def log_tool_call_start(
        self,
        turn_id: Optional[str],
        call_id: str,
        name: str,
        args: Dict[str, Any],
        source: str = "live_api",
    ) -> None:
        """ツールコールの呼び出し開始を記録する。"""
        if not self._enabled:
            return
        t_id = turn_id or self.get_latest_turn_id()
        self._write_record({
            "event": "tool_call_start",
            "turn_id": t_id,
            "call_id": call_id,
            "tool_name": name,
            "source": source,
            "args": args,
        })

    def log_tool_call_end(
        self,
        turn_id: Optional[str],
        call_id: str,
        name: str,
        result: Any,
        latency_ms: float,
        error: Optional[str] = None,
        source: str = "live_api",
    ) -> None:
        """ツールコールの完了とレイテンシ、実行結果を記録する。"""
        if not self._enabled:
            return
        t_id = turn_id or self.get_latest_turn_id()
        tool_entry = {
            "call_id": call_id,
            "name": name,
            "source": source,
            "latency_ms": round(latency_ms, 2),
            "status": "error" if error else "success",
            "error": error,
        }
        with self._lock:
            if t_id and t_id in self._active_turns:
                self._active_turns[t_id].tool_calls.append(tool_entry)

        # ログ肥大化を防ぐため結果の要約またはフル結果
        res_summary = result
        if isinstance(result, dict) and len(str(result)) > 1000:
            res_summary = {
                k: (v if len(str(v)) < 100 else f"<{type(v).__name__} len={len(str(v))}>")
                for k, v in result.items()
            }

        self._write_record({
            "event": "tool_call",
            "turn_id": t_id,
            "call_id": call_id,
            "tool_name": name,
            "source": source,
            "latency_ms": round(latency_ms, 2),
            "status": "error" if error else "success",
            "error": error,
            "result_summary": res_summary,
        })

    def log_audio_received(self, turn_id: Optional[str], chunk_bytes: int) -> None:
        """受信した音声バイト数をターンコンテキストに加算する。"""
        if not self._enabled or chunk_bytes <= 0:
            return
        t_id = turn_id or self.get_latest_turn_id()
        now = time.time()
        with self._lock:
            if t_id and t_id in self._active_turns:
                ctx = self._active_turns[t_id]
                if ctx.first_audio_at is None:
                    ctx.first_audio_at = now
                ctx.audio_chunks_count += 1
                ctx.audio_total_bytes += chunk_bytes

    def log_utterance_interrupted(
        self,
        turn_id: Optional[str],
        discarded_bytes: int,
        reason: str = "barge_in",
    ) -> Optional[Dict[str, Any]]:
        """割り込み（barge-inなど）による音声再生中断を記録する。"""
        if not self._enabled:
            return None

        t_id = turn_id or self.get_latest_turn_id()
        if not t_id:
            return None

        now = time.time()
        with self._lock:
            ctx = self._active_turns.get(t_id)
            if not ctx:
                return None

            ctx.status = (
                "interrupted_by_server"
                if reason == "server_interrupted"
                else "interrupted_by_barge_in"
            )
            ctx.interrupt_reason = reason
            ctx.interrupted_at = now
            ctx.audio_discarded_bytes = discarded_bytes
            ctx.audio_played_bytes = max(0, ctx.audio_total_bytes - discarded_bytes)

            full_text = ctx.full_transcription
            played_text = estimate_played_text(full_text, ctx.completion_rate)
            ttfb_ms = (
                int((ctx.first_audio_at - ctx.prompt_sent_at) * 1000)
                if ctx.first_audio_at
                else None
            )

            record = {
                "event": "utterance",
                "turn_id": t_id,
                "trigger": ctx.trigger_type,
                "event_type": ctx.event_type,
                "priority": ctx.priority,
                "transcription": full_text,
                "audio_total_duration_ms": ctx.audio_total_duration_ms,
                "audio_played_duration_ms": ctx.audio_played_duration_ms,
                "completion_rate": round(ctx.completion_rate, 3),
                "status": ctx.status,
                "interrupted_reason": reason,
                "played_text_estimate": played_text,
                "latency_to_first_audio_ms": ttfb_ms,
            }

        self._write_record(record)
        return record

    def log_utterance_complete(self, turn_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """発話ターン全体の正常再生完了を記録する。"""
        if not self._enabled:
            return None

        t_id = turn_id or self.get_latest_turn_id()
        if not t_id:
            return None

        now = time.time()
        with self._lock:
            ctx = self._active_turns.get(t_id)
            if not ctx:
                return None

            # すでに中断処理されている場合はスキップ
            if ctx.status.startswith("interrupted"):
                return None

            ctx.status = "completed"
            ctx.completed_at = now
            ctx.audio_discarded_bytes = 0
            ctx.audio_played_bytes = ctx.audio_total_bytes

            full_text = ctx.full_transcription
            ttfb_ms = (
                int((ctx.first_audio_at - ctx.prompt_sent_at) * 1000)
                if ctx.first_audio_at
                else None
            )

            record = {
                "event": "utterance",
                "turn_id": t_id,
                "trigger": ctx.trigger_type,
                "event_type": ctx.event_type,
                "priority": ctx.priority,
                "transcription": full_text,
                "audio_total_duration_ms": ctx.audio_total_duration_ms,
                "audio_played_duration_ms": ctx.audio_played_duration_ms,
                "completion_rate": 1.0,
                "status": "completed",
                "interrupted_reason": None,
                "played_text_estimate": full_text,
                "latency_to_first_audio_ms": ttfb_ms,
            }

        self._write_record(record)
        return record

    def get_recent_logs(
        self, limit: int = 50, event_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """メモリ内の最新ログを返却する。Web APIやテスト用。"""
        with self._lock:
            logs = list(self._recent_logs)

        if event_type:
            logs = [entry for entry in logs if entry.get("event") == event_type]

        return logs[-limit:]

    def close(self) -> None:
        """ログファイルを安全にクローズする。"""
        with self._lock:
            if self._file_handle:
                try:
                    self._file_handle.flush()
                    self._file_handle.close()
                except Exception as e:
                    logger.error(f"Error closing AI log file: {e}")
                finally:
                    self._file_handle = None
