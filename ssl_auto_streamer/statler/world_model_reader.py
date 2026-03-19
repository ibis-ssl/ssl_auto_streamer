# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""World Model Reader - Generates commentary from game state."""

import json
from enum import Enum
from typing import Dict, Any, Optional
from dataclasses import dataclass

from .world_model_writer import WorldModelWriter, GameContext


class CommentaryMode(Enum):
    """Commentary generation mode."""

    REFLEX = "reflex"
    ANALYST = "analyst"


@dataclass
class CommentaryRequest:
    """Request for commentary generation."""

    mode: CommentaryMode
    event_type: Optional[str] = None
    event_data: Optional[Dict[str, Any]] = None
    context: Optional[GameContext] = None
    priority: int = 1


class WorldModelReader:
    """
    Statler Architecture - Reader Component.

    Generates commentary content based on events and game state.
    """

    def __init__(self, writer: WorldModelWriter):
        self._writer = writer
        self._current_mode = CommentaryMode.REFLEX

        self._reflex_templates = {
            "GOAL": {
                "hint": "ゴール！",
                "instruction": "得点したチーム名とロボットIDを叫び、スコアを更新して伝える。シュートの速度や距離にも触れる。",
                "suggested_function": "get_highlight_details",
            },
            "FAST_SHOT": {
                "hint": "強烈なシュート！",
                "instruction": "シュート速度を秒速で読み上げ、シューターのIDを伝える。制限速度秒速6.5メートルとの比較も言及する。",
                "suggested_function": "get_highlight_details",
            },
            "SHOT": {
                "hint": "シュート！",
                "instruction": "シューターのIDと位置、シュートの方向を短く伝える。",
                "suggested_function": None,
            },
            "SAVE": {
                "hint": "ナイスセーブ！",
                "instruction": "キーパーのIDを呼び、どの方向に飛んできたシュートを止めたか伝える。",
                "suggested_function": "get_highlight_details",
            },
            "INTERCEPTION": {
                "hint": "カット！",
                "instruction": "インターセプトしたチームとロボットIDを短く伝える。",
                "suggested_function": None,
            },
            "POSSESSION_CHANGE": {
                "hint": "",
                "instruction": "ボール保持が変わったことを一言で。過度に強調しない。",
                "suggested_function": None,
            },
            "BALL_OUT": {
                "hint": "アウト！",
                "instruction": "どちらのチームが出したか、フィールドのどの辺から出たかを伝える。",
                "suggested_function": None,
            },
            "SET_PLAY": {
                "hint": "",
                "instruction": "セットプレーの種類（フリーキック/キックオフ/PK等）とどちらのチームかを伝える。",
                "suggested_function": "get_game_state",
            },
            "FOUL": {
                "hint": "ファール！",
                "instruction": "ファールの種類名と違反内容を具体的に説明する。数値がある場合は日本語で読み上げる。",
                "suggested_function": "get_game_state",
            },
        }

    def set_mode(self, mode: CommentaryMode) -> None:
        self._current_mode = mode

    def get_mode(self) -> CommentaryMode:
        return self._current_mode

    def generate_reflex(
        self, event_type: str, event_data: Dict[str, Any]
    ) -> CommentaryRequest:
        """Generate reflex-mode commentary for an event."""
        context = self._writer.get_context()

        priority = 1
        if event_type in ["GOAL", "FAST_SHOT", "SAVE", "FOUL"]:
            priority = 2
        elif event_type in ["POSSESSION_CHANGE", "BALL_OUT"]:
            priority = 0

        return CommentaryRequest(
            mode=CommentaryMode.REFLEX,
            event_type=event_type,
            event_data=event_data,
            context=context,
            priority=priority,
        )

    def generate_analysis(self) -> Optional[CommentaryRequest]:
        """Generate analyst-mode commentary."""
        context = self._writer.get_context()
        highlights = self._writer.get_pending_highlights()

        if not highlights and not context.recent_events:
            return None

        analysis_type = self._determine_analysis_type(context, highlights)

        payload = {
            "mode": "analyst",
            "analysis_type": analysis_type,
            "instruction": self._ANALYSIS_INSTRUCTIONS.get(analysis_type, "試合状況を分析して解説する。"),
            "recommended_functions": self._get_recommended_functions(analysis_type),
            "context": {
                "score": {"ours": context.our_score, "theirs": context.their_score},
                "elapsed_minutes": context.elapsed_seconds / 60.0,
                "momentum": context.momentum,
            },
        }

        if highlights:
            top_highlight = max(highlights, key=lambda h: h.score)
            payload["highlight"] = {
                "type": top_highlight.event_type,
                "data": top_highlight.data,
                "importance": top_highlight.score,
            }

        return CommentaryRequest(
            mode=CommentaryMode.ANALYST,
            event_data=payload,
            context=context,
            priority=1,
        )

    def to_gemini_json(self, request: CommentaryRequest) -> str:
        """Convert CommentaryRequest to JSON for Gemini API."""
        if request.mode == CommentaryMode.REFLEX:
            template = self._reflex_templates.get(request.event_type, {})
            event_payload: Dict[str, Any] = {
                "type": request.event_type,
                "hint": template.get("hint", "") if isinstance(template, dict) else template,
                "instruction": template.get("instruction", "") if isinstance(template, dict) else "",
                "data": request.event_data or {},
            }
            if isinstance(template, dict) and template.get("suggested_function"):
                event_payload["suggested_function"] = template["suggested_function"]
            payload = {
                "mode": "reflex",
                "event": event_payload,
                "context": self._context_to_dict(request.context),
            }
        else:
            payload = {
                "mode": "analyst",
                "data": request.event_data or {},
                "context": self._context_to_dict(request.context),
            }

        return json.dumps(payload, ensure_ascii=False)

    def _context_to_dict(self, context: Optional[GameContext]) -> Dict[str, Any]:
        if not context:
            return {}
        return {
            "score": {"ours": context.our_score, "theirs": context.their_score},
            "elapsed_minutes": context.elapsed_seconds / 60.0,
            "momentum": context.momentum,
            "recent_events": context.recent_events[-3:],
        }

    _ANALYSIS_INSTRUCTIONS: Dict[str, str] = {
        "goal_replay": "直前のゴールを詳しく振り返る。シュート速度、距離、シューターのID、スコアの変動を具体的に伝える。",
        "shot_analysis": "直前のシュートを分析する。コース、速度、キーパーの反応を語る。",
        "save_highlight": "キーパーのファインセーブを称える。反応速度やポジショニングに触れる。",
        "game_summary": "ここまでの試合を総括する。スコア、主要なハイライト、両チームの戦い方を分析する。",
        "team_introduction": "両チームの特徴と布陣を紹介する。注目ポイントを伝える。",
        "tactical_analysis": "現在の戦術的状況を分析する。布陣、数的優位、攻撃パターンを語る。",
    }

    def _determine_analysis_type(self, context: GameContext, highlights: list) -> str:
        if highlights:
            top = max(highlights, key=lambda h: h.score)
            if top.event_type == "GOAL":
                return "goal_replay"
            elif top.event_type in ["FAST_SHOT", "SHOT"]:
                return "shot_analysis"
            elif top.event_type == "SAVE":
                return "save_highlight"

        if context.elapsed_seconds > 60 * 5:
            return "game_summary"
        else:
            return "team_introduction"

    def _get_recommended_functions(self, analysis_type: str) -> list:
        recommendations: Dict[str, list] = {
            "goal_replay": [
                {"function": "get_highlight_details", "args": {"highlight_type": "goal"}, "purpose": "ゴールの詳細データ取得"},
                {"function": "get_robot_status", "purpose": "シューターの位置と状態確認"},
            ],
            "shot_analysis": [
                {"function": "get_highlight_details", "args": {"highlight_type": "shot"}, "purpose": "シュートの速度とコース"},
                {"function": "get_ball_trajectory", "purpose": "ボールの軌跡確認"},
            ],
            "save_highlight": [
                {"function": "get_highlight_details", "args": {"highlight_type": "save"}, "purpose": "セーブの詳細"},
                {"function": "get_robot_status", "purpose": "キーパーの反応確認"},
            ],
            "game_summary": [
                {"function": "get_game_state", "purpose": "スコアと試合状況"},
                {"function": "get_formation_analysis", "purpose": "両チームの布陣"},
                {"function": "get_highlight_details", "args": {"highlight_type": "any", "count": 3}, "purpose": "主要ハイライト"},
            ],
            "team_introduction": [
                {"function": "get_all_robots_summary", "purpose": "出場ロボット一覧"},
                {"function": "get_formation_analysis", "purpose": "開始時の布陣"},
            ],
            "tactical_analysis": [
                {"function": "get_formation_analysis", "purpose": "布陣分析"},
                {"function": "get_all_robots_summary", "purpose": "全ロボット概要"},
                {"function": "get_game_state", "purpose": "試合の流れ"},
            ],
        }
        return recommendations.get(analysis_type, [{"function": "get_game_state", "purpose": "試合状況確認"}])
