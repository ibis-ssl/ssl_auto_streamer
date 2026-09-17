from unittest.mock import MagicMock

from ssl_auto_streamer.web.server import WebServer


class _FakeGeminiClient:
    def is_connected(self):
        return False


def test_push_transcription_includes_turn_id(tmp_path):
    server = WebServer(
        host="127.0.0.1",
        port=0,
        writer=object(),
        gemini_client=_FakeGeminiClient(),
        config={},
        config_dir=tmp_path,
    )
    broadcast_messages = []

    async def fake_broadcast(msg):
        broadcast_messages.append(msg)

    server._broadcast = fake_broadcast
    server._fire_and_forget = MagicMock()

    server.push_transcription("ロボドラゴンズ対アイビス", turn_id="turn_123")
    assert server._fire_and_forget.call_count == 1
    coro = server._fire_and_forget.call_args[0][0]
    coro.close()

    server.push_transcription("後半開始")
    assert server._fire_and_forget.call_count == 2
    coro = server._fire_and_forget.call_args[0][0]
    coro.close()


def test_push_turn_complete(tmp_path):
    server = WebServer(
        host="127.0.0.1",
        port=0,
        writer=object(),
        gemini_client=_FakeGeminiClient(),
        config={},
        config_dir=tmp_path,
    )
    server._fire_and_forget = MagicMock()

    server.push_turn_complete(turn_id="turn_456")
    assert server._fire_and_forget.call_count == 1
    coro = server._fire_and_forget.call_args[0][0]
    coro.close()
