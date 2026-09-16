# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""Unit tests for Gemini 3.8 Live & 3.5 Flash-Lite integration."""

from ssl_auto_streamer.gemini.live_api_client import (
    GeminiConfig,
    GeminiLiveApiClient,
    normalize_live_model_name,
)
from ssl_auto_streamer.gemini.analysis_agent import AnalysisAgent
from ssl_auto_streamer.gemini.function_handler import FunctionHandler
from ssl_auto_streamer.statler.world_model_writer import WorldModelWriter


def test_normalize_live_model_name():
    """Verify normalization of user/config model strings."""
    assert normalize_live_model_name(None) == "gemini-3.8-live"
    assert normalize_live_model_name("") == "gemini-3.8-live"
    assert normalize_live_model_name("gemini-3.8-live") == "gemini-3.8-live"
    assert normalize_live_model_name("gemini-3.8-flash-live") == "gemini-3.8-live"
    assert normalize_live_model_name("gemini-3.8-flash-live-preview") == "gemini-3.8-live"
    assert normalize_live_model_name("gemini 3.8 flash live") == "gemini-3.8-live"
    assert normalize_live_model_name("gemini-3.8-live-extended-thinking") == "gemini-3.8-live-extended-thinking"


def test_gemini_config_defaults():
    """Verify GeminiConfig has optimal 3.8 Live defaults."""
    cfg = GeminiConfig()
    assert cfg.model == "gemini-3.8-live"
    assert cfg.output_transcription is True
    assert cfg.language_code == "ja-JP"


def test_analysis_agent_defaults():
    """Verify AnalysisAgent defaults to gemini-3.5-flash-lite."""
    writer = WorldModelWriter()
    agent = AnalysisAgent(writer=writer, config={})
    assert agent._model == "gemini-3.5-flash-lite"
    assert agent._timeout == 5


def test_function_handler_new_tools():
    """Verify get_field_geometry, get_team_profile, and get_ssl_rule."""
    writer = WorldModelWriter()
    team_profiles = {
        "profiles": {
            "ibis": {
                "reading": "アイビス",
                "country": "日本",
                "style": "パス主体",
            },
            "TIGERs Mannheim": {
                "reading": "タイガース",
                "style": "精密パス",
            }
        }
    }
    ssl_rules = {
        "basic_info": {
            "ball_speed_limit": 6.5,
            "robot_speed_in_stop": 1.5,
        },
        "fouls": {
            "BOT_KICKED_BALL_TOO_FAST": {
                "name_jp": "ボール速度超過",
                "description": "ボールを6.5m/s以上で蹴る",
                "penalty": "STOP後フリーキック",
            }
        }
    }

    handler = FunctionHandler(
        writer=writer,
        team_profiles=team_profiles,
        ssl_rules=ssl_rules,
    )

    # 1. get_field_geometry
    geom = handler.handle("get_field_geometry", {})
    assert "field_length_m" in geom
    assert "field_width_m" in geom
    assert "goal_width_m" in geom

    # 2. get_team_profile
    profile = handler.handle("get_team_profile", {"team": "all"})
    assert "blue" in profile
    assert "yellow" in profile

    # 3. get_ssl_rule with specific key
    rule_detail = handler.handle("get_ssl_rule", {"rule_key": "BOT_KICKED_BALL_TOO_FAST"})
    assert rule_detail.get("name_jp") == "ボール速度超過"

    # 4. get_ssl_rule without key (summary)
    rule_summary = handler.handle("get_ssl_rule", {})
    assert "basic_info" in rule_summary
    assert len(rule_summary.get("foul_types", [])) > 0


def test_live_api_thought_handling():
    """Verify that thought parts trigger thought_callback and not text_callback."""
    client = GeminiLiveApiClient()

    received_texts = []
    received_thoughts = []
    client.set_text_callback(lambda t: received_texts.append(t))
    client.set_thought_callback(lambda t: received_thoughts.append(t))

    # Simulate server response with thought and text parts
    data = {
        "serverContent": {
            "modelTurn": {
                "parts": [
                    {"thought": True, "text": "相手ロボットがパスコースを切っているため、右サイドの展開を予測する。"},
                    {"text": "青チームが素早くパスを回します。"}
                ]
            }
        }
    }

    client._handle_response(data)

    assert len(received_thoughts) == 1
    assert "相手ロボットがパスコースを切っている" in received_thoughts[0]
    assert len(received_texts) == 1
    assert "青チームが素早くパスを回します" in received_texts[0]
