import argparse
import base64

from ssl_auto_streamer.audio_mode import (
    normalize_audio_output_mode,
    uses_client_audio,
    uses_server_audio,
)
from ssl_auto_streamer.main import load_config
from ssl_auto_streamer.web.server import WebServer


def _args(config_path, audio_output_mode=None):
    return argparse.Namespace(
        config=str(config_path),
        gemini_api_key=None,
        tracker_addr=None,
        tracker_ports=None,
        gc_addr=None,
        gc_ports=None,
        vision_addr=None,
        vision_ports=None,
        web_port=None,
        audio_output_mode=audio_output_mode,
    )


def test_audio_output_mode_helpers():
    assert normalize_audio_output_mode("CLIENT") == "client"
    assert normalize_audio_output_mode("invalid") == "server"
    assert uses_server_audio("server")
    assert uses_server_audio("both")
    assert not uses_server_audio("client")
    assert uses_client_audio("client")
    assert uses_client_audio("both")
    assert not uses_client_audio("off")


def test_load_config_applies_audio_output_mode_cli_override(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("audio:\n  output_mode: server\n", encoding="utf-8")

    config = load_config(_args(config_path, audio_output_mode="client"))

    assert config["audio"]["output_mode"] == "client"


def test_load_config_normalizes_invalid_audio_output_mode(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("audio:\n  output_mode: invalid\n", encoding="utf-8")

    config = load_config(_args(config_path))

    assert config["audio"]["output_mode"] == "server"


class _FakeGeminiClient:
    def is_connected(self):
        return False


def test_web_server_builds_pcm_audio_chunk_message(tmp_path):
    server = WebServer(
        host="127.0.0.1",
        port=0,
        writer=object(),
        gemini_client=_FakeGeminiClient(),
        config={"audio": {"output_mode": "client"}},
        config_dir=tmp_path,
    )
    pcm = b"\x00\x00\xff\x7f"

    message = server.build_audio_chunk_message(pcm, sample_rate=24000, channels=1)

    assert message["type"] == "output_audio"
    assert message["encoding"] == "pcm_s16le"
    assert message["sample_rate"] == 24000
    assert message["channels"] == 1
    assert message["data"] == base64.b64encode(pcm).decode("ascii")


def test_web_server_status_includes_audio_output_mode(tmp_path):
    server = WebServer(
        host="127.0.0.1",
        port=0,
        writer=object(),
        gemini_client=_FakeGeminiClient(),
        config={"audio": {"output_mode": "both"}},
        config_dir=tmp_path,
    )

    status = server._build_status_dict()

    assert status["audio_output_mode"] == "both"
    assert status["audio_output_subscribers"] == 0


def test_web_server_status_prefers_runtime_audio_output_mode(tmp_path):
    server = WebServer(
        host="127.0.0.1",
        port=0,
        writer=object(),
        gemini_client=_FakeGeminiClient(),
        config={"audio": {"output_mode": "client"}},
        config_dir=tmp_path,
        get_audio_output_mode=lambda: "server",
    )

    status = server._build_status_dict()

    assert status["audio_output_mode"] == "server"


def test_web_server_status_uses_gc_port_status_as_receiving_signal(tmp_path):
    server = WebServer(
        host="127.0.0.1",
        port=0,
        writer=object(),
        gemini_client=_FakeGeminiClient(),
        config={},
        config_dir=tmp_path,
        get_port_status=lambda: {
            "gc": {
                "active": 10003,
                "ports": [
                    {"port": 10003, "receiving": True},
                    {"port": 11003, "receiving": False},
                ],
            }
        },
    )

    status = server._build_status_dict()

    assert status["gc_receiving"] is True
    assert status["port_status"]["gc"]["ports"][0]["receiving"] is True
