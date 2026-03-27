# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""Pipeline Logger - 発話パイプラインの構造化ログ記録。

各発話のライフサイクル（追加・選択・破棄・読み上げ開始/終了・
キャンセル・割り込み）を JSONL 形式でファイルに記録する。
後から分析して実況システムの改善に役立てることを目的とする。
"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class PipelineLogger:
    """
    実況パイプラインの構造化ログレコーダー。

    UtteranceQueue の _emit コールバックを受け取り、
    各発話のライフサイクルイベントを JSONL ファイルに記録する。
    書き込みごとに flush するためクラッシュ時もデータロスを最小化する。

    ログ形式（1行 = 1イベント）:
        {"ts": "...", "elapsed_ms": 123, "event": "enqueue", "utt_id": 1, ...}
    """

    def __init__(self, log_dir: str = "logs/pipeline") -> None:
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)

        session_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = self._log_dir / f"pipeline_{session_ts}.jsonl"
        self._file = open(log_path, "w", encoding="utf-8")

        self._session_start = time.monotonic()

        # wait_ms / age_ms / speak_duration_ms の算出に使う発話ごとの開始時刻
        self._enqueued_at: Dict[int, float] = {}
        self._speak_started_at: Dict[int, float] = {}

        logger.info(f"PipelineLogger started: {log_path}")

    def on_event(self, event: str, data: Dict[str, Any]) -> None:
        """パイプラインイベントを受け取り、JSONL に書き込む。"""
        now = time.monotonic()
        elapsed_ms = round((now - self._session_start) * 1000)
        ts = datetime.now().isoformat(timespec="milliseconds")

        record: Dict[str, Any] = {
            "ts": ts,
            "elapsed_ms": elapsed_ms,
            "event": event,
        }

        utt_id: Optional[int] = data.get("id")
        if utt_id is not None:
            record["utt_id"] = utt_id

        if event == "enqueue":
            self._enqueued_at[utt_id] = now
            record.update({
                "text": data.get("text", ""),
                "priority": data.get("priority"),
                "event_type": data.get("event_type"),
                "pending_count": data.get("pending_count"),
            })

        elif event == "select":
            record.update({
                "text": data.get("text", ""),
                "candidates": data.get("candidates"),
                "selected": data.get("selected"),
                "select_method": data.get("select_method"),
            })
            if utt_id in self._enqueued_at:
                record["wait_ms"] = round((now - self._enqueued_at[utt_id]) * 1000)

        elif event == "discard":
            record.update({
                "text": data.get("text", ""),
                "candidates": data.get("candidates"),
                "reason": data.get("reason"),
            })
            if utt_id in self._enqueued_at:
                record["age_ms"] = round((now - self._enqueued_at.pop(utt_id)) * 1000)

        elif event == "speak_start":
            self._speak_started_at[utt_id] = now
            record.update({"text": data.get("text", "")})
            if utt_id in self._enqueued_at:
                record["wait_ms"] = round((now - self._enqueued_at[utt_id]) * 1000)

        elif event == "speak_end":
            record.update({"text": data.get("text", "")})
            if utt_id in self._speak_started_at:
                record["speak_duration_ms"] = round(
                    (now - self._speak_started_at.pop(utt_id)) * 1000
                )
            self._enqueued_at.pop(utt_id, None)

        elif event == "cancel":
            record.update({"text": data.get("text", "")})
            if utt_id in self._enqueued_at:
                record["age_ms"] = round((now - self._enqueued_at.pop(utt_id)) * 1000)
            self._speak_started_at.pop(utt_id, None)

        elif event == "interrupt":
            dropped = data.get("dropped", [])
            for u in dropped:
                self._enqueued_at.pop(u.get("id"), None)
                self._speak_started_at.pop(u.get("id"), None)
            record.update({
                "new_priority": data.get("new_priority"),
                "dropped_count": data.get("dropped_count"),
                "dropped": [{"utt_id": u.get("id"), "text": u.get("text")} for u in dropped],
            })

        elif event == "clear":
            self._enqueued_at.clear()
            self._speak_started_at.clear()
            record["dropped_count"] = data.get("dropped_count")

        self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._file.flush()

    def close(self) -> None:
        """ログファイルを閉じる。"""
        try:
            self._file.close()
        except Exception:
            pass
        logger.info("PipelineLogger closed")
