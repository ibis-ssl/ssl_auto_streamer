# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""Analysis Agent - REST API で上位 Gemini モデルによる深い試合分析を行う。"""

import json
import logging
from typing import Any, Callable, Dict, List, Optional

try:
    import aiohttp

    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

from ssl_auto_streamer.statler import WorldModelWriter

logger = logging.getLogger(__name__)

# 分析タイプごとのプロンプトテンプレート
_PROMPT_TEMPLATES: Dict[str, str] = {
    "momentum": (
        "あなたはRoboCup Small Size League（SSL）の実況解説者です。"
        "以下の初期データをもとに、必要であれば追加でツールを呼び出してデータを補完し、"
        "現在の試合の流れ・どちらが優勢か・転機となった出来事を2〜4文で分析してください。"
        "実況者が読み上げることを前提に、断定的で自然な日本語で記述してください。\n\n"
        "{data}"
    ),
    "player_spotlight": (
        "あなたはRoboCup Small Size League（SSL）の実況解説者です。"
        "以下の初期データをもとに、必要であれば追加でツールを呼び出してデータを補完し、"
        "注目すべきロボットの活躍・役割・パフォーマンスを2〜4文で紹介してください。"
        "{context_hint}"
        "実況者が読み上げることを前提に、断定的で自然な日本語で記述してください。\n\n"
        "{data}"
    ),
    "match_prediction": (
        "あなたはRoboCup Small Size League（SSL）の実況解説者です。"
        "以下の初期データをもとに、必要であれば追加でツールを呼び出してデータを補完し、"
        "残り時間と現在の状況から今後の展開予測を2〜4文で述べてください。"
        "実況者が読み上げることを前提に、断定的で自然な日本語で記述してください。\n\n"
        "{data}"
    ),
    "halftime_summary": (
        "あなたはRoboCup Small Size League（SSL）の実況解説者です。"
        "以下の初期データをもとに、必要であれば追加でツールを呼び出してデータを補完し、"
        "前半の総括（得点経緯・支配的だったチーム・両チームの課題）を2〜4文でまとめてください。"
        "実況者が読み上げることを前提に、断定的で自然な日本語で記述してください。\n\n"
        "{data}"
    ),
}


class AnalysisAgent:
    """REST API で上位 Gemini モデルによる深い試合分析を行うエージェント。

    tool_declarations: Live API と共有する function_declarations のうち
        request_analysis を除いたリスト（再帰防止）。
    tool_executor: FunctionHandler.handle（同期）を渡す。
    """

    def __init__(
        self,
        writer: WorldModelWriter,
        config: Dict[str, Any],
        tool_declarations: Optional[List[Dict[str, Any]]] = None,
        tool_executor: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
    ):
        self._writer = writer
        self._enabled = config.get("enabled", True)
        self._model = config.get("model", "gemini-3-flash-preview")
        self._api_key = config.get("api_key", "")
        self._temperature = config.get("temperature", 0.7)
        self._max_tokens = config.get("max_output_tokens", 512)
        self._timeout = config.get("timeout_seconds", 8)
        self._max_tool_iterations = config.get("max_tool_iterations", 3)
        self._tool_declarations = tool_declarations or []
        self._tool_executor = tool_executor
        self._session: Optional[Any] = None  # aiohttp.ClientSession

    async def start(self) -> None:
        """HTTP セッションを開始する。"""
        if not AIOHTTP_AVAILABLE:
            logger.warning("aiohttp not available; AnalysisAgent will use fallback only")
            return
        self._session = aiohttp.ClientSession()
        logger.info(
            f"AnalysisAgent started (model={self._model}, "
            f"tools={len(self._tool_declarations)})"
        )

    async def close(self) -> None:
        """HTTP セッションを閉じる。"""
        if self._session:
            await self._session.close()
            self._session = None

    async def analyze(self, analysis_type: str, context: Optional[str] = None) -> Dict[str, Any]:
        """指定した分析タイプで試合データを収集し、上位モデルに分析させる。"""
        if not self._enabled:
            return {"analysis": "分析機能は現在無効です。"}

        data = self._collect_initial_data(analysis_type)
        prompt = self._build_prompt(analysis_type, data, context)

        if not self._api_key:
            logger.warning("AnalysisAgent: api_key not set, using fallback")
            return self._fallback_analysis(analysis_type, data)

        if not AIOHTTP_AVAILABLE or not self._session:
            logger.warning("AnalysisAgent: aiohttp unavailable, using fallback")
            return self._fallback_analysis(analysis_type, data)

        try:
            result_text = await self._call_rest_api(prompt)
            logger.info(f"AnalysisAgent: {analysis_type} analysis completed")
            return {"analysis": result_text}
        except Exception as e:
            logger.warning(f"AnalysisAgent REST API error ({analysis_type}): {e}, using fallback")
            return self._fallback_analysis(analysis_type, data)

    def _collect_initial_data(self, analysis_type: str) -> Dict[str, Any]:
        """分析タイプに応じて WorldModelWriter から初期データを収集する。"""
        collectors = {
            "momentum": lambda: {
                "game_state": self._writer.get_game_state_data(),
                "recent_events": self._writer.get_event_history_data(10),
                "stats": self._writer.get_match_stats_data(),
            },
            "player_spotlight": lambda: {
                "robots": self._writer.get_all_robots_summary_data("all"),
                "recent_events": self._writer.get_event_history_data(10),
                "stats": self._writer.get_match_stats_data(),
            },
            "match_prediction": lambda: {
                "game_state": self._writer.get_game_state_data(),
                "stats": self._writer.get_match_stats_data(),
                "cards_and_fouls": self._writer.get_team_cards_and_fouls_data(),
            },
            "halftime_summary": lambda: {
                "game_state": self._writer.get_game_state_data(),
                "robots": self._writer.get_all_robots_summary_data("all"),
                "stats": self._writer.get_match_stats_data(),
                "recent_events": self._writer.get_event_history_data(10),
                "cards_and_fouls": self._writer.get_team_cards_and_fouls_data(),
            },
        }

        collector = collectors.get(analysis_type)
        if collector:
            try:
                return collector()
            except Exception as e:
                logger.warning(f"Data collection error for {analysis_type}: {e}")
                return {}
        return {}

    def _build_prompt(
        self, analysis_type: str, data: Dict[str, Any], context: Optional[str]
    ) -> str:
        """プロンプトを構築する。"""
        template = _PROMPT_TEMPLATES.get(analysis_type, _PROMPT_TEMPLATES["momentum"])
        context_hint = f"着目点: {context}\n" if context else ""
        data_str = json.dumps(data, ensure_ascii=False, indent=2)

        return template.format(data=data_str, context_hint=context_hint)

    async def _call_rest_api(self, prompt: str) -> str:
        """Gemini REST API を Function Calling ループ付きで呼び出し、最終テキストを返す。"""
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/"
            f"models/{self._model}:generateContent?key={self._api_key}"
        )

        contents: List[Dict[str, Any]] = [
            {"role": "user", "parts": [{"text": prompt}]}
        ]
        payload: Dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": self._temperature,
                "maxOutputTokens": self._max_tokens,
            },
        }
        if self._tool_declarations:
            payload["tools"] = [{"function_declarations": self._tool_declarations}]

        timeout = aiohttp.ClientTimeout(total=self._timeout)

        for iteration in range(self._max_tool_iterations + 1):
            async with self._session.post(url, json=payload, timeout=timeout) as resp:
                resp.raise_for_status()
                body = await resp.json()

            candidates = body.get("candidates", [])
            if not candidates:
                raise ValueError("No candidates in response")

            response_content = candidates[0].get("content", {})
            parts = response_content.get("parts", [])

            function_call_parts = [p for p in parts if "functionCall" in p]
            text_parts = [p for p in parts if "text" in p]

            if not function_call_parts:
                if text_parts:
                    return " ".join(p["text"] for p in text_parts).strip()
                raise ValueError("No text in final response")

            # モデルの関数呼び出しをコンテキストに追加
            contents.append({"role": "model", "parts": parts})

            # 各関数を実行してレスポンスを構築
            function_response_parts = []
            for fc_part in function_call_parts:
                fc = fc_part["functionCall"]
                fc_name = fc.get("name", "")
                fc_args = fc.get("args", {})
                logger.debug(f"AnalysisAgent tool call [{iteration+1}]: {fc_name}({fc_args})")

                if self._tool_executor:
                    result = self._tool_executor(fc_name, fc_args)
                else:
                    result = {"error": f"No tool executor configured: {fc_name}"}

                function_response_parts.append({
                    "functionResponse": {
                        "name": fc_name,
                        "response": result,
                    }
                })

            contents.append({"role": "user", "parts": function_response_parts})
            payload["contents"] = contents

        raise ValueError(f"Max tool iterations ({self._max_tool_iterations}) reached without final text response")

    def _fallback_analysis(self, analysis_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """API が利用できない場合のフォールバック分析テキストを生成する。"""
        game_state = data.get("game_state", {})
        score = game_state.get("score", {})
        score_blue = score.get("blue", "?")
        score_yellow = score.get("yellow", "?")

        messages = {
            "momentum": f"スコア青{score_blue}対黄{score_yellow}で試合は続いている。",
            "player_spotlight": "各ロボットがそれぞれの役割を果たしている。",
            "match_prediction": f"スコア青{score_blue}対黄{score_yellow}。このまま終盤まで接戦が続くか。",
            "halftime_summary": f"前半はスコア青{score_blue}対黄{score_yellow}で折り返した。",
        }

        text = messages.get(analysis_type, "試合は続いている。")
        return {"analysis": text}
