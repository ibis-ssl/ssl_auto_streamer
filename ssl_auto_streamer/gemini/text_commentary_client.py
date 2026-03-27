# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""Text Commentary Client - REST API によるストリーミングテキスト実況生成。

GeminiLiveApiClient の text モード代替。
gemini-3.1-flash-lite 等の通常モデルに streamGenerateContent (SSE) で接続し、
テキスト応答をストリーミングで受け取る。
"""

import asyncio
import json
import logging
import os
import inspect
from typing import Any, Callable, Dict, List, Optional

try:
    import aiohttp

    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

logger = logging.getLogger(__name__)

_REST_BASE = "https://generativelanguage.googleapis.com/v1beta"


class TextCommentaryClient:
    """
    REST API (streamGenerateContent) を使ったテキスト実況クライアント。

    GeminiLiveApiClient と同じコールバックインターフェースを提供する:
      - set_text_callback(fn)          テキストチャンク受信時
      - set_turn_complete_callback(fn) ターン完了時
      - set_function_call_handler(fn)  関数呼び出し処理
      - set_disconnect_callback(fn)    (スタブ: REST なので常時接続ではない)
      - set_transcription_callback(fn) (スタブ: text モードでは不使用)
      - set_audio_callback(fn)         (スタブ: text モードでは不使用)

    send_text(text) でテキストを送信し、SSE ストリーミングでテキストを受け取る。
    会話履歴をセッション内で保持し、文脈のある応答を生成する。
    """

    def __init__(
        self,
        api_key: str = "",
        model: str = "gemini-3.1-flash-lite",
        system_instruction: str = "",
        tools_config: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 1.0,
        max_output_tokens: int = 1024,
    ):
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self._model = model
        self._system_instruction = system_instruction
        self._tools_config = tools_config or []
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens

        self._text_callback: Optional[Callable[[str], None]] = None
        self._turn_complete_callback: Optional[Callable[[], None]] = None
        self._function_call_handler: Optional[Callable[..., Any]] = None
        self._on_disconnect_callback: Optional[Callable[[], None]] = None

        self._session: Optional[Any] = None  # aiohttp.ClientSession
        self._connected = False
        self._is_generating = False
        self._conversation: List[Dict[str, Any]] = []  # 会話履歴
        self._pending_tasks: set = set()

    # ===== コールバック登録 (GeminiLiveApiClient 互換) =====

    def set_text_callback(self, callback: Callable[[str], None]) -> None:
        self._text_callback = callback

    def set_turn_complete_callback(self, callback: Callable[[], None]) -> None:
        self._turn_complete_callback = callback

    def set_function_call_handler(self, handler: Callable[..., Any]) -> None:
        self._function_call_handler = handler

    def set_disconnect_callback(self, callback: Callable[[], None]) -> None:
        self._on_disconnect_callback = callback

    def set_transcription_callback(self, callback: Callable[[str], None]) -> None:
        pass  # text モードでは使用しない

    def set_audio_callback(self, callback: Callable[[bytes], None]) -> None:
        pass  # text モードでは使用しない

    # ===== 接続管理 =====

    async def connect(self) -> bool:
        if not AIOHTTP_AVAILABLE:
            logger.error("aiohttp is required for TextCommentaryClient")
            return False
        if not self._api_key:
            logger.error("GEMINI_API_KEY not set")
            return False

        if self._session and not self._session.closed:
            await self._session.close()

        self._session = aiohttp.ClientSession()
        self._connected = True
        self._conversation = []
        logger.info(f"TextCommentaryClient connected (model={self._model})")
        return True

    async def disconnect(self) -> None:
        self._connected = False
        if self._session:
            await self._session.close()
            self._session = None
        logger.info("TextCommentaryClient disconnected")

    def is_connected(self) -> bool:
        return self._connected

    @property
    def is_generating(self) -> bool:
        return self._is_generating

    @property
    def session_age(self) -> float:
        return 0.0  # REST は常時接続ではないのでリフレッシュ不要

    # ===== 送信 =====

    async def send_text(self, text: str) -> None:
        if not self._connected:
            logger.warning("TextCommentaryClient not connected")
            return
        task = asyncio.create_task(self._stream_request(text))
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def send_audio(self, audio_b64: str) -> None:
        pass  # text モードでは使用しない

    async def set_thinking_level(self, level: str) -> None:
        pass  # text モードでは thinking_config を使用しない

    # ===== ストリーミングリクエスト =====

    async def _stream_request(self, text: str) -> None:
        """streamGenerateContent SSE でテキストを送信し、応答をストリーミング受信する。"""
        if not self._session:
            return

        self._is_generating = True
        self._conversation.append({"role": "user", "parts": [{"text": text}]})

        url = (
            f"{_REST_BASE}/models/{self._model}:streamGenerateContent"
            f"?key={self._api_key}&alt=sse"
        )

        payload: Dict[str, Any] = {
            "contents": self._conversation,
            "generationConfig": {
                "temperature": self._temperature,
                "maxOutputTokens": self._max_output_tokens,
            },
            "systemInstruction": {
                "parts": [{"text": self._system_instruction}]
            },
        }
        if self._tools_config:
            payload["tools"] = [{"function_declarations": self._tools_config}]

        try:
            await self._do_stream(url, payload)
        except Exception as e:
            logger.error(f"TextCommentaryClient request error: {e}")
        finally:
            self._is_generating = False

    async def _do_stream(self, url: str, payload: Dict[str, Any]) -> None:
        """SSE ストリームを読み取りテキストチャンクを callback に渡す。Function Calling もサポート。"""
        timeout = aiohttp.ClientTimeout(total=30)
        accumulated_text = ""

        for _iteration in range(4):  # function calling ループ (最大3回)
            async with self._session.post(url, json=payload, timeout=timeout) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"TextCommentaryClient HTTP {resp.status}: {body[:200]}")
                    return

                function_calls = []
                response_parts = []

                async for line in resp.content:
                    line = line.decode("utf-8").rstrip()
                    if not line.startswith("data: "):
                        continue
                    raw = line[6:]
                    if raw == "[DONE]":
                        break
                    try:
                        chunk = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    candidates = chunk.get("candidates", [])
                    if not candidates:
                        continue
                    parts = candidates[0].get("content", {}).get("parts", [])

                    for part in parts:
                        if "text" in part:
                            t = part["text"]
                            accumulated_text += t
                            if t and self._text_callback:
                                self._text_callback(t)
                            response_parts.append(part)
                        elif "functionCall" in part:
                            function_calls.append(part)
                            response_parts.append(part)

            if not function_calls:
                break

            # function calling ループ
            self._conversation.append({"role": "model", "parts": response_parts})
            function_response_parts = []
            for fc_part in function_calls:
                fc = fc_part["functionCall"]
                fc_name = fc.get("name", "")
                fc_args = fc.get("args", {})
                logger.debug(f"TextCommentaryClient tool call: {fc_name}({fc_args})")
                result = await self._execute_function(fc_name, fc_args)
                function_response_parts.append({
                    "functionResponse": {"name": fc_name, "response": result}
                })

            self._conversation.append({"role": "user", "parts": function_response_parts})
            payload["contents"] = self._conversation
            # 次のリクエストでは systemInstruction は不要 (会話履歴に含まれる)

        if accumulated_text:
            self._conversation.append({
                "role": "model",
                "parts": [{"text": accumulated_text}],
            })

        if self._turn_complete_callback:
            self._turn_complete_callback()

    async def _execute_function(self, fc_name: str, fc_args: Dict[str, Any]) -> Dict[str, Any]:
        if not self._function_call_handler:
            return {"error": "No function handler"}
        try:
            result = self._function_call_handler(fc_name, fc_args)
            if inspect.isawaitable(result):
                result = await result
            return result
        except Exception as e:
            logger.error(f"Function call error: {fc_name} -> {e}")
            return {"error": str(e)}
