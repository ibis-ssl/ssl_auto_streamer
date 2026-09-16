# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""Tests for AiActivityLogger, tool call tracking, and utterance progress estimation."""

import json
import tempfile
from pathlib import Path

from ssl_auto_streamer.logger import (
    AiActivityLogger,
    estimate_played_text,
)
from ssl_auto_streamer.audio.pcm_output import PcmAudioOutput


def test_estimate_played_text():
    """発話テキストの中断推定の精度と句読点境界テスト。"""
    full_text = "青の8番がシュートを放ちました！キーパーがファインセーブ！"

    # 完了時（98%以上）
    assert estimate_played_text(full_text, 1.0) == full_text
    assert estimate_played_text(full_text, 0.99) == full_text

    # 開始直後（5%以下）
    early = estimate_played_text(full_text, 0.03)
    assert "開始直後で中断" in early

    # 中盤（約60%）: 句読点（！）の境界で綺麗に切れるか
    mid = estimate_played_text(full_text, 0.6)
    assert "放ちました！" in mid
    assert "[60%で中断]" in mid

    # 空文字
    assert estimate_played_text("", 0.5) == ""


def test_ai_activity_logger_full_turn():
    """ターン開始からツールコール、文字起こし、音声受信、正常再生完了までのライフサイクルテスト。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        ai_logger = AiActivityLogger(
            config={"enabled": True, "save_to_file": True},
            log_dir=tmp_path,
        )

        # セッション開始
        ai_logger.log_session_start(
            model="gemini-3.8-live",
            voice="Aoede",
            sample_rate=24000,
            thinking_level="medium",
        )

        # ターン開始（シュート実況）
        turn_id = ai_logger.start_turn(
            trigger_type="reflex",
            event_type="SHOT",
            priority=2,
            payload={"event": "SHOT", "ball_speed": 7.5},
        )
        assert turn_id is not None

        # 思考（thought）
        ai_logger.log_thought(turn_id, "相手GKの位置を確認してゴール期待値を算出中")

        # ツールコール（開始〜完了）
        ai_logger.log_tool_call_start(
            turn_id,
            call_id="call_001",
            name="get_ball_trajectory",
            args={"seconds": 2.0},
        )
        ai_logger.log_tool_call_end(
            turn_id,
            call_id="call_001",
            name="get_ball_trajectory",
            result={"points": 10, "speed": 7.4},
            latency_ms=4.5,
        )

        # 文字起こしチャンク
        ai_logger.log_transcription(turn_id, "青の8番が")
        ai_logger.log_transcription(turn_id, "強烈なシュート！")

        # 音声チャンク受信 (24000Hz 16bit mono = 48000 bytes/s = 48 bytes/ms)
        # 48000 bytes = 1000ms
        ai_logger.log_audio_received(turn_id, 24000)
        ai_logger.log_audio_received(turn_id, 24000)

        # 正常再生完了
        rec = ai_logger.log_utterance_complete(turn_id)
        assert rec is not None
        assert rec["event"] == "utterance"
        assert rec["status"] == "completed"
        assert rec["audio_total_duration_ms"] == 1000
        assert rec["audio_played_duration_ms"] == 1000
        assert rec["completion_rate"] == 1.0
        assert rec["transcription"] == "青の8番が強烈なシュート！"
        assert rec["played_text_estimate"] == "青の8番が強烈なシュート！"

        ai_logger.close()

        # ファイル出力の検証
        jsonl_files = list(tmp_path.glob("ai_session_*.jsonl"))
        assert len(jsonl_files) == 1
        lines = [json.loads(line) for line in jsonl_files[0].read_text(encoding="utf-8").strip().split("\n")]

        events = [line["event"] for line in lines]
        assert "session_start" in events
        assert "prompt_sent" in events
        assert "thought" in events
        assert "tool_call_start" in events
        assert "tool_call" in events
        assert "transcription" in events
        assert "utterance" in events


def test_ai_activity_logger_barge_in_interruption():
    """優先度の高いイベント発生によるbarge-in割り込み中断テスト。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        ai_logger = AiActivityLogger(
            config={"enabled": True, "save_to_file": True},
            log_dir=tmp_path,
        )

        turn_id = ai_logger.start_turn(
            trigger_type="reflex",
            event_type="PASS",
            priority=1,
            payload={"event": "PASS"},
        )

        ai_logger.log_transcription(turn_id, "キックスが中盤で落ち着いてボールを回しています。")
        # 96000 bytes = 2000ms 分の音声データを受信
        ai_logger.log_audio_received(turn_id, 96000)

        # 途中でGOALが発生し、未再生バッファ38400 bytes (800ms) が破棄された
        # 実際に再生されたのは 96000 - 38400 = 57600 bytes (1200ms -> 60%)
        rec = ai_logger.log_utterance_interrupted(
            turn_id,
            discarded_bytes=38400,
            reason="barge_in_by_GOAL",
        )

        assert rec is not None
        assert rec["status"] == "interrupted_by_barge_in"
        assert rec["interrupted_reason"] == "barge_in_by_GOAL"
        assert rec["audio_total_duration_ms"] == 2000
        assert rec["audio_played_duration_ms"] == 1200
        assert rec["completion_rate"] == 0.6
        assert "[60%で中断]" in rec["played_text_estimate"]

        ai_logger.close()


def test_pcm_output_discarded_bytes_tracking():
    """PcmAudioOutputのclear_bufferによる破棄バイト数計測テスト。"""
    audio = PcmAudioOutput(sample_rate=24000)
    audio._running = True  # 再生スレッドを起動せずにバッファロジックをテスト
    chunk = b"\x00\x01" * 1000  # 2000 bytes
    audio.play(chunk)
    audio.play(chunk)

    assert audio.get_buffered_bytes() == 4000
    discarded = audio.clear_buffer()
    assert discarded == 4000
    assert audio.get_buffered_bytes() == 0


def test_recent_logs_ring_buffer():
    """メモリ内リングバッファとフィルタリングのテスト。"""
    ai_logger = AiActivityLogger(config={"enabled": True, "save_to_file": False})
    t1 = ai_logger.start_turn("reflex", "SHOT")
    ai_logger.log_thought(t1, "思考1")
    ai_logger.log_tool_call_start(t1, "c1", "test_tool", {})
    ai_logger.log_tool_call_end(t1, "c1", "test_tool", {"ok": True}, 1.2)

    all_logs = ai_logger.get_recent_logs(limit=10)
    assert len(all_logs) == 4

    tool_logs = ai_logger.get_recent_logs(limit=10, event_type="tool_call")
    assert len(tool_logs) == 1
    assert tool_logs[0]["tool_name"] == "test_tool"
    assert tool_logs[0]["latency_ms"] == 1.2


def test_commentary_app_ai_logger_integration():
    """CommentaryApp内でのAiActivityLogger初期化とコールバック連携のテスト。"""
    from ssl_auto_streamer.app import CommentaryApp

    with tempfile.TemporaryDirectory() as tmpdir:
        config = {
            "ai_logger": {"enabled": True, "log_dir": tmpdir, "save_to_file": True},
            "web": {"enabled": False},
            "audio": {"output_mode": "off"},
        }
        app = CommentaryApp(config)
        assert app._ai_logger is not None

        # ツールコールのシミュレーション
        app._on_tool_call_start("call_test", "get_game_state", {})
        app._on_tool_call_end("call_test", "get_game_state", {"state": "Halt"}, 2.3)

        logs = app._ai_logger.get_recent_logs(limit=10)
        assert len(logs) >= 2
        assert any(log.get("event") == "tool_call_start" for log in logs)
        assert any(log.get("event") == "tool_call" for log in logs)

        app._ai_logger.close()
