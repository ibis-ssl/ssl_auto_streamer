# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""Function Call Handler for Gemini Live API."""

import logging
from typing import TYPE_CHECKING, Any, Dict, Optional

from ssl_auto_streamer.statler import WorldModelWriter

if TYPE_CHECKING:
    from ssl_auto_streamer.gemini.analysis_agent import AnalysisAgent

logger = logging.getLogger(__name__)


class FunctionHandler:
    """Routes function calls from Gemini to WorldModelWriter data providers."""

    def __init__(self, writer: WorldModelWriter):
        self._writer = writer
        self._analysis_agent: Optional["AnalysisAgent"] = None

    def set_analysis_agent(self, agent: "AnalysisAgent") -> None:
        """AnalysisAgent を登録する。"""
        self._analysis_agent = agent

    async def handle_async(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """非同期関数呼び出しをハンドルする（request_analysis 対応）。"""
        if name == "request_analysis":
            if self._analysis_agent is None:
                return {"error": "AnalysisAgent is not configured"}
            analysis_type = args.get("analysis_type", "momentum")
            context = args.get("context")
            return await self._analysis_agent.analyze(analysis_type, context)
        return self.handle(name, args)

    def handle(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle a function call from Gemini."""
        handlers = {
            "get_game_state": self._handle_get_game_state,
            "get_ball_trajectory": self._handle_get_ball_trajectory,
            "get_robot_status": self._handle_get_robot_status,
            "get_all_robots_summary": self._handle_get_all_robots_summary,
            "get_team_cards_and_fouls": self._handle_get_team_cards_and_fouls,
            "get_match_stats": self._handle_get_match_stats,
            "get_event_history": self._handle_get_event_history,
            "get_highlight_details": self._handle_get_highlight_details,
        }

        handler = handlers.get(name)
        if handler:
            try:
                return handler(args)
            except Exception as e:
                logger.error(f"Error handling {name}: {e}")
                return {"error": str(e)}
        else:
            logger.warning(f"Unknown function: {name}")
            return {"error": f"Unknown function: {name}"}

    def _handle_get_game_state(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return self._writer.get_game_state_data()

    def _handle_get_ball_trajectory(self, args: Dict[str, Any]) -> Dict[str, Any]:
        seconds = args.get("seconds", 3.0)
        return self._writer.get_ball_trajectory_data(seconds)

    def _handle_get_robot_status(self, args: Dict[str, Any]) -> Dict[str, Any]:
        robot_id = args.get("robot_id")
        team = args.get("team")
        if robot_id is None or team is None:
            return {"error": "robot_id and team are required"}
        if team not in ("blue", "yellow"):
            return {"error": "team must be 'blue' or 'yellow'"}
        return self._writer.get_robot_status_data(int(robot_id), team)

    def _handle_get_all_robots_summary(self, args: Dict[str, Any]) -> Dict[str, Any]:
        team = args.get("team", "all")
        return self._writer.get_all_robots_summary_data(team)

    def _handle_get_team_cards_and_fouls(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return self._writer.get_team_cards_and_fouls_data()

    def _handle_get_match_stats(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return self._writer.get_match_stats_data()

    def _handle_get_event_history(self, args: Dict[str, Any]) -> Dict[str, Any]:
        count = args.get("count", 5)
        return self._writer.get_event_history_data(int(count))

    def _handle_get_highlight_details(self, args: Dict[str, Any]) -> Dict[str, Any]:
        highlight_type = args.get("highlight_type", "any")
        count = args.get("count", 1)
        return self._writer.get_highlight_details_data(highlight_type, count)
