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


def test_visual_streamer_rendering():
    """Verify FieldVisualStreamer generates valid JPEG image bytes from game state."""
    from ssl_auto_streamer.gemini.visual_streamer import FieldVisualStreamer, PIL_AVAILABLE
    from ssl_auto_streamer.statler.world_model_writer import RobotSnapshot

    assert PIL_AVAILABLE is True

    writer = WorldModelWriter()
    # Add dummy robot and ball data
    writer._current_ball_pos = (1.2, -0.5, 0.0)
    writer._current_ball_vel = (2.0, 1.0, 0.0)
    writer._robot_snapshots_blue[3] = RobotSnapshot(
        robot_id=3,
        team="blue",
        position=(-1.0, 0.5, 0.78),
        velocity=(1.0, 0.0),
        is_available=True,
        has_ball_contact=True,
    )
    writer._robot_snapshots_yellow[0] = RobotSnapshot(
        robot_id=0,
        team="yellow",
        position=(4.0, 0.0, 3.14),
        velocity=(0.0, 0.0),
        is_available=True,
        has_ball_contact=False,
    )

    streamer = FieldVisualStreamer(writer, width=320, height=240, jpeg_quality=70)
    assert streamer.is_available is True

    frame_bytes = streamer.render_frame_bytes()
    assert frame_bytes is not None
    assert len(frame_bytes) > 500
    # JPEG magic bytes: 0xFF, 0xD8, 0xFF
    assert frame_bytes[:3] == b"\xff\xd8\xff"
    assert streamer.get_last_frame() == frame_bytes


def test_live_api_client_send_image():
    """Verify GeminiLiveApiClient.send_image sends correct realtime_input payload."""
    import asyncio
    import json

    async def _test():
        client = GeminiLiveApiClient(GeminiConfig(api_key="dummy_key"))
        client._connected = True

        sent_messages = []

        class MockWs:
            async def send(self, data):
                sent_messages.append(data)

        client._ws = MockWs()

        dummy_image = b"\xff\xd8\xff\xe0dummy_jpeg_bytes"
        await client.send_image(dummy_image, mime_type="image/jpeg")

        assert len(sent_messages) == 1
        payload = json.loads(sent_messages[0])
        assert "realtime_input" in payload
        chunks = payload["realtime_input"]["media_chunks"]
        assert len(chunks) == 1
        assert chunks[0]["mime_type"] == "image/jpeg"
        import base64
        decoded = base64.b64decode(chunks[0]["data"])
        assert decoded == dummy_image

    asyncio.run(_test())


def test_switch_model():
    """Verify switch_model updates client config."""
    import asyncio

    async def _test():
        client = GeminiLiveApiClient(GeminiConfig(model="gemini-3.8-live"))
        assert client._config.model == "gemini-3.8-live"

        await client.switch_model("gemini-3.8-live-extended-thinking")
        assert client._config.model == "gemini-3.8-live-extended-thinking"

    asyncio.run(_test())

