# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""Gemini Multimodal Live API Client for real-time audio commentary."""

import asyncio
import base64
import inspect
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional, Dict, Any, List

try:
    import websockets
    from websockets.client import WebSocketClientProtocol

    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    WebSocketClientProtocol = None

logger = logging.getLogger(__name__)


class ThinkingLevel(str, Enum):
    """Gemini Live API のthinkingLevel設定値。"""

    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class GeminiConfig:
    """Configuration for Gemini Live API."""

    api_key: str = ""
    model: str = "gemini-3.1-flash-live-preview"
    sample_rate: int = 24000
    voice: str = "Aoede"
    system_instruction: str = ""
    tools_config: List[Dict[str, Any]] = field(default_factory=list)
    thinking_level: str = "medium"  # minimal / low / medium / high
    output_transcription: bool = True


class GeminiLiveApiClient:
    """Client for Gemini Multimodal Live API."""

    def __init__(self, config: Optional[GeminiConfig] = None):
        if not WEBSOCKETS_AVAILABLE:
            raise ImportError(
                "websockets package is required. Install with: pip install websockets"
            )

        self._config = config or GeminiConfig()
        if not self._config.api_key:
            self._config.api_key = os.environ.get("GEMINI_API_KEY", "")

        self._ws: Optional[WebSocketClientProtocol] = None
        self._connected = False
        self._is_generating = False
        self._audio_callback: Optional[Callable[[bytes], None]] = None
        self._function_call_handler: Optional[
            Callable[[str, Dict[str, Any]], Dict[str, Any]]
        ] = None
        self._receive_task: Optional[asyncio.Task] = None
        self._on_disconnect_callback: Optional[Callable[[], None]] = None
        self._turn_complete_callback: Optional[Callable[[], None]] = None
        self._transcription_callback: Optional[Callable[[str], None]] = None
        self._session_start_time: float = 0.0

        self._ws_url = (
            f"wss://generativelanguage.googleapis.com/ws/"
            f"google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContent"
            f"?key={self._config.api_key}"
        )

    def set_disconnect_callback(self, callback: Callable[[], None]) -> None:
        self._on_disconnect_callback = callback

    def set_turn_complete_callback(self, callback: Callable[[], None]) -> None:
        self._turn_complete_callback = callback

    def set_transcription_callback(self, callback: Callable[[str], None]) -> None:
        self._transcription_callback = callback

    @property
    def session_age(self) -> float:
        """Returns the number of seconds since the current session was established."""
        if self._session_start_time == 0.0:
            return 0.0
        return time.time() - self._session_start_time

    async def connect(self) -> bool:
        """Establish WebSocket connection to Gemini API."""
        if self._connected:
            return True

        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
            self._receive_task = None

        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        if not self._config.api_key:
            logger.error("GEMINI_API_KEY not set")
            return False

        try:
            self._ws = await websockets.connect(self._ws_url)

            generation_config: Dict[str, Any] = {
                "response_modalities": ["AUDIO"],
                "speech_config": {
                    "voice_config": {
                        "prebuilt_voice_config": {
                            "voice_name": self._config.voice
                        }
                    }
                },
                "thinking_config": {
                    "thinkingLevel": self._config.thinking_level,
                },
            }

            setup_msg = {
                "setup": {
                    "model": f"models/{self._config.model}",
                    "generation_config": generation_config,
                    "system_instruction": {
                        "parts": [{"text": self._config.system_instruction}]
                    },
                }
            }

            if self._config.tools_config:
                setup_msg["setup"]["tools"] = [
                    {"function_declarations": self._config.tools_config}
                ]

            await self._ws.send(json.dumps(setup_msg))

            response = await self._ws.recv()
            response_data = json.loads(response)

            if "setupComplete" in response_data:
                self._connected = True
                self._is_generating = False
                self._session_start_time = time.time()
                logger.info("Connected to Gemini Live API")
                self._receive_task = asyncio.create_task(self._receive_loop())
                return True
            else:
                logger.error(f"Setup failed: {response_data}")
                return False

        except Exception as e:
            logger.error(f"Connection failed: {e}")
            return False

    async def disconnect(self) -> None:
        """Close WebSocket connection."""
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass

        if self._ws:
            await self._ws.close()

        self._connected = False
        self._is_generating = False
        self._session_start_time = 0.0
        logger.info("Disconnected from Gemini Live API")

    def is_connected(self) -> bool:
        return self._connected

    @property
    def is_generating(self) -> bool:
        """Gemini が現在音声を生成中かどうか（バージイン判定用）。"""
        return self._is_generating

    def set_audio_callback(self, callback: Callable[[bytes], None]) -> None:
        self._audio_callback = callback

    def set_function_call_handler(
        self, handler: Callable[..., Any]
    ) -> None:
        self._function_call_handler = handler

    async def set_thinking_level(self, level: str) -> None:
        """thinkingLevelを更新する。次回セッション接続時に反映される。"""
        if level == self._config.thinking_level:
            return
        logger.info(f"thinkingLevel queued: {self._config.thinking_level} -> {level} (applies on next session)")
        self._config.thinking_level = level

    async def send_audio(self, audio_b64: str) -> None:
        """Send audio via realtime_input (PCM 16-bit mono 16000Hz + trailing silence, base64-encoded)."""
        if not self._connected or not self._ws:
            logger.warning("Not connected to Gemini API")
            return

        message = {
            "realtime_input": {
                "media_chunks": [{
                    "mime_type": "audio/pcm;rate=16000",
                    "data": audio_b64,
                }]
            }
        }

        try:
            await self._ws.send(json.dumps(message))
            logger.debug(f"[send_audio] {len(audio_b64)} chars (b64)")
        except Exception as e:
            logger.error(f"Failed to send audio: {e}")
            self._connected = False

    async def send_text(self, text: str) -> None:
        """Send text input to generate audio commentary."""
        if not self._connected or not self._ws:
            logger.warning("Not connected to Gemini API")
            return

        message = {
            "realtime_input": {
                "text": text,
            }
        }

        try:
            await self._ws.send(json.dumps(message))
            logger.debug(f"Sent text: {text[:50]}...")
        except Exception as e:
            logger.error(f"Failed to send text: {e}")
            self._connected = False

    async def _receive_loop(self) -> None:
        """Background loop to receive audio data."""
        if not self._ws:
            return

        try:
            async for message in self._ws:
                try:
                    data = json.loads(message)
                    self._handle_response(data)
                except json.JSONDecodeError:
                    logger.warning("Received non-JSON message")
        except websockets.exceptions.ConnectionClosed as e:
            close_code = getattr(e.rcvd, "code", "unknown") if e.rcvd else "unknown"
            close_reason = getattr(e.rcvd, "reason", "") if e.rcvd else ""
            logger.info(f"WebSocket connection closed (code={close_code}, reason={close_reason!r})")
            self._connected = False
            if self._on_disconnect_callback:
                self._on_disconnect_callback()
        except Exception as e:
            logger.error(f"Receive loop error: {e}")
            self._connected = False
            if self._on_disconnect_callback:
                self._on_disconnect_callback()

    def _handle_response(self, data: dict) -> None:
        """Handle response from Gemini API."""
        if "serverContent" in data:
            server_content = data["serverContent"]

            if "modelTurn" in server_content:
                self._is_generating = True
                model_turn = server_content["modelTurn"]
                if "parts" in model_turn:
                    for part in model_turn["parts"]:
                        if "inlineData" in part:
                            inline_data = part["inlineData"]
                            if inline_data.get("mimeType", "").startswith("audio/"):
                                audio_b64 = inline_data.get("data", "")
                                if audio_b64 and self._audio_callback:
                                    audio_bytes = base64.b64decode(audio_b64)
                                    logger.debug(
                                        f"Received audio: {len(audio_bytes)} bytes"
                                    )
                                    self._audio_callback(audio_bytes)

            # 出力音声の文字起こし (gemini-3.1以降)
            if "outputTranscription" in server_content:
                transcription = server_content["outputTranscription"]
                text = transcription.get("text", "")
                if text and self._transcription_callback:
                    logger.debug(f"Transcription: {text}")
                    self._transcription_callback(text)

            if server_content.get("turnComplete"):
                logger.debug("Turn complete")
                self._is_generating = False
                if self._turn_complete_callback:
                    self._turn_complete_callback()

        if "toolCall" in data:
            tool_call = data["toolCall"]
            function_calls = tool_call.get("functionCalls", [])
            for fc in function_calls:
                self._handle_function_call(fc)

    def _handle_function_call(self, fc: dict) -> None:
        """Handle a single function call from Gemini."""
        fc_id = fc.get("id", "")
        fc_name = fc.get("name", "")
        fc_args = fc.get("args", {})

        logger.info(f"Function call: {fc_name}({fc_args})")

        if self._function_call_handler:
            asyncio.create_task(self._execute_function_call(fc_id, fc_name, fc_args))
        else:
            logger.warning(f"No handler for function call: {fc_name}")
            asyncio.create_task(
                self._send_function_response(
                    fc_id, fc_name, {"error": "No function handler registered"}
                )
            )

    async def _execute_function_call(
        self, fc_id: str, fc_name: str, fc_args: Dict[str, Any]
    ) -> None:
        """非同期でファンクションコールを実行して結果を返送する。"""
        try:
            result = self._function_call_handler(fc_name, fc_args)
            if inspect.isawaitable(result):
                result = await result
            await self._send_function_response(fc_id, fc_name, result)
        except Exception as e:
            logger.error(f"Function call error: {fc_name} -> {e}")
            await self._send_function_response(fc_id, fc_name, {"error": str(e)})

    async def _send_function_response(
        self, fc_id: str, fc_name: str, result: Dict[str, Any]
    ) -> None:
        """Send function response back to Gemini."""
        if not self._connected or not self._ws:
            return

        response_msg = {
            "tool_response": {
                "function_responses": [
                    {
                        "id": fc_id,
                        "name": fc_name,
                        "response": result,
                    }
                ]
            }
        }

        try:
            await self._ws.send(json.dumps(response_msg))
        except Exception as e:
            logger.error(f"Failed to send function response: {e}")
