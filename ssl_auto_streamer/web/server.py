# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""aiohttp-based Web Server for SSL Auto Streamer UI."""

import asyncio
import copy
import json
import logging
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Set

import aiohttp
import yaml
from aiohttp import web

from ssl_auto_streamer.gemini import ThinkingLevel

from ssl_auto_streamer.statler.world_model_writer import DEFAULT_BLUE_TEAM_NAME, DEFAULT_YELLOW_TEAM_NAME

logger = logging.getLogger(__name__)

_RECEIVER_TIMEOUT_SEC = 2.0


class WebServer:
    """
    Web UI server providing REST API, WebSocket, and static file serving.

    Broadcasts game state at 5Hz via WebSocket to connected clients.
    """

    def __init__(
        self,
        host: str,
        port: int,
        writer: Any,  # WorldModelWriter
        gemini_client: Any,  # GeminiLiveApiClient
        config: Dict[str, Any],
        config_dir: Path,
        on_config_update: Optional[Callable[[Dict[str, Any]], None]] = None,
        get_team_names: Optional[Callable[[], tuple]] = None,
        on_start_streaming: Optional[Callable[[], None]] = None,
        on_stop_streaming: Optional[Callable[[], None]] = None,
        get_streaming: Optional[Callable[[], bool]] = None,
        get_pipeline_snapshot: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
        on_switch_port: Optional[Callable[[str, int], bool]] = None,
        get_port_status: Optional[Callable[[], Dict[str, Any]]] = None,
    ):
        self._host = host
        self._port = port
        self._writer = writer
        self._gemini_client = gemini_client
        self._config = config
        self._config_dir = config_dir
        self._on_config_update = on_config_update
        self._get_team_names = get_team_names
        self._on_start_streaming = on_start_streaming
        self._on_stop_streaming = on_stop_streaming
        self._get_streaming = get_streaming
        self._get_pipeline_snapshot = get_pipeline_snapshot
        self._on_switch_port = on_switch_port
        self._get_port_status = get_port_status

        self._ws_clients: Set[web.WebSocketResponse] = set()
        self._commentary_history: Deque[Dict[str, Any]] = deque(maxlen=10)
        self._event_log: Deque[Dict[str, Any]] = deque(maxlen=20)
        self._pipeline_log: Deque[Dict[str, Any]] = deque(maxlen=30)
        self._tracker_last_seen: float = 0.0
        self._gc_last_seen: float = 0.0
        # Keep strong references to fire-and-forget tasks to prevent GC
        self._pending_tasks: Set[asyncio.Task] = set()

        self._static_dir = Path(__file__).parent / "static"
        self._app = web.Application()
        self._runner: Optional[web.AppRunner] = None
        self._broadcast_task: Optional[asyncio.Task] = None

        self._setup_routes()

    def _setup_routes(self) -> None:
        self._app.router.add_get("/", self._handle_index)
        self._app.router.add_get("/overlay", self._handle_overlay)
        self._app.router.add_get("/overlay-control", self._handle_overlay_control)
        self._app.router.add_get("/ws", self._handle_ws)
        self._app.router.add_get("/api/config", self._handle_get_config)
        self._app.router.add_post("/api/config", self._handle_post_config)
        self._app.router.add_get("/api/status", self._handle_get_status)
        self._app.router.add_get("/api/team-profiles", self._handle_get_team_profiles)
        self._app.router.add_post("/api/streaming/start", self._handle_streaming_start)
        self._app.router.add_post("/api/streaming/stop", self._handle_streaming_stop)
        self._app.router.add_post("/api/ssl/switch-port", self._handle_switch_port)
        if self._static_dir.exists():
            self._app.router.add_static("/static", self._static_dir)

    async def start(self) -> None:
        """Start the web server."""
        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        self._broadcast_task = asyncio.create_task(self._broadcast_loop())
        logger.info(f"Web UI started at http://{self._host}:{self._port}")

    async def stop(self) -> None:
        """Stop the web server."""
        if self._broadcast_task:
            self._broadcast_task.cancel()
        for ws in list(self._ws_clients):
            try:
                await ws.close()
            except Exception:
                pass
        if self._runner:
            await self._runner.cleanup()

    def push_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """Push a detected game event to connected WebSocket clients."""
        entry = {"event_type": event_type, "data": data, "timestamp": time.time()}
        self._event_log.append(entry)
        self._fire_and_forget(self._broadcast(
            json.dumps({"type": "event", **entry}, ensure_ascii=False)
        ))

    def push_commentary(self, text: str) -> None:
        """Push a commentary text to history and connected clients."""
        entry = {"text": text, "timestamp": time.time()}
        self._commentary_history.append(entry)
        self._fire_and_forget(self._broadcast(
            json.dumps({"type": "commentary", **entry}, ensure_ascii=False)
        ))

    def push_transcription(self, text: str) -> None:
        """Push output audio transcription to connected clients."""
        self._fire_and_forget(self._broadcast(
            json.dumps({"type": "transcription", "text": text, "timestamp": time.time()}, ensure_ascii=False)
        ))

    def push_pipeline_event(self, event: str, data: Dict[str, Any]) -> None:
        """Push a pipeline lifecycle event to connected WebSocket clients."""
        entry = {"event": event, "data": data, "timestamp": time.time()}
        self._pipeline_log.append(entry)
        self._fire_and_forget(self._broadcast(
            json.dumps({"type": "pipeline", **entry}, ensure_ascii=False)
        ))

    def update_tracker_seen(self) -> None:
        """Mark that a tracker frame was received."""
        self._tracker_last_seen = time.time()

    def update_gc_seen(self) -> None:
        """Mark that a GC message was received."""
        self._gc_last_seen = time.time()

    def _fire_and_forget(self, coro: Any) -> None:
        """Schedule a coroutine and keep a strong reference to prevent GC."""
        task = asyncio.create_task(coro)
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    # ========== Broadcast Loop ==========

    async def _broadcast_loop(self) -> None:
        """Broadcast full state to all clients at 5Hz."""
        while True:
            try:
                await asyncio.sleep(0.2)
                if not self._ws_clients:
                    continue
                state = self._build_state_message()
                await self._broadcast(json.dumps(state, ensure_ascii=False))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Broadcast loop error: {e}")

    def _build_status_dict(self) -> Dict[str, Any]:
        """Build system status dict (used by both broadcast and REST API)."""
        now = time.time()
        streaming = self._get_streaming() if self._get_streaming else False
        status = {
            "gemini_connected": self._gemini_client.is_connected(),
            "tracker_receiving": (now - self._tracker_last_seen) < _RECEIVER_TIMEOUT_SEC,
            "gc_receiving": (now - self._gc_last_seen) < _RECEIVER_TIMEOUT_SEC,
            "streaming": streaming,
        }
        if self._get_port_status:
            status["port_status"] = self._get_port_status()
        return status

    @staticmethod
    def _safe_call(method: Callable, default: Any = None) -> Any:
        """Call a method, returning default on failure."""
        try:
            return method()
        except Exception:
            return default if default is not None else {}

    def _build_state_message(self) -> Dict[str, Any]:
        """Build the full state snapshot for WebSocket broadcast."""
        game_state = self._safe_call(self._writer.get_game_state_data)
        ball = self._safe_call(self._writer.get_ball_trajectory_data)
        robots_summary = self._safe_call(self._writer.get_all_robots_summary_data)
        field_snapshot = self._safe_call(self._writer.get_field_snapshot_data)
        match_stats = self._safe_call(self._writer.get_match_stats_data)
        cards = self._safe_call(self._writer.get_team_cards_and_fouls_data)

        team_info = self._build_team_info()
        pipeline_snapshot = None
        if self._get_pipeline_snapshot:
            pipeline_snapshot = self._safe_call(self._get_pipeline_snapshot)
        return {
            "type": "state",
            "game_state": game_state,
            "ball": ball,
            "robots_summary": robots_summary,
            "field_snapshot": field_snapshot,
            "match_stats": match_stats,
            "cards": cards,
            "status": self._build_status_dict(),
            "commentary_history": list(self._commentary_history),
            "event_log": list(self._event_log),
            "team_info": team_info,
            "pipeline_snapshot": pipeline_snapshot,
            "pipeline_log": list(self._pipeline_log),
        }

    def _build_team_info(self) -> Dict[str, Any]:
        """Build team info dict from GC team names."""
        blue_name = DEFAULT_BLUE_TEAM_NAME
        yellow_name = DEFAULT_YELLOW_TEAM_NAME

        if self._get_team_names:
            try:
                blue_name, yellow_name = self._get_team_names()
            except Exception:
                pass

        return {
            "blue": {"name": blue_name, "color": "blue"},
            "yellow": {"name": yellow_name, "color": "yellow"},
        }

    async def _broadcast(self, msg: str) -> None:
        """Send a message to all connected WebSocket clients."""
        if not self._ws_clients:
            return
        dead: Set[web.WebSocketResponse] = set()
        for ws in list(self._ws_clients):
            try:
                await ws.send_str(msg)
            except Exception:
                dead.add(ws)
        self._ws_clients -= dead

    # ========== HTTP Handlers ==========

    async def _handle_index(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(self._static_dir / "index.html")

    async def _handle_overlay(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(self._static_dir / "overlay.html")

    async def _handle_overlay_control(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(self._static_dir / "overlay-control.html")

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws_clients.add(ws)
        logger.debug(f"WebSocket connected, total: {len(self._ws_clients)}")

        try:
            state = self._build_state_message()
            await ws.send_str(json.dumps(state, ensure_ascii=False))

            ptt_start: Optional[float] = None
            ptt_chunks: int = 0

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        msg_type = data.get("type")
                        if msg_type == "overlay_control":
                            await self._broadcast(msg.data)
                        elif msg_type == "user_text":
                            text = data.get("text", "").strip()
                            if text:
                                if self._gemini_client.is_connected():
                                    await self._gemini_client.set_thinking_level(ThinkingLevel.MEDIUM)
                                    await self._gemini_client.send_text(text)
                                    logger.info(f"[user_text] -> Gemini: 「{text}」")
                                else:
                                    logger.warning(f"[user_text] Gemini未接続のため送信スキップ: 「{text}」")
                                self.push_event("USER_TEXT", {"text": text})
                        elif msg_type == "audio_chunk":
                            audio_data = data.get("data", "")
                            if audio_data:
                                if self._gemini_client.is_connected():
                                    if ptt_start is None:
                                        ptt_start = time.time()
                                        ptt_chunks = 0
                                        logger.info("[PTT] 録音開始")
                                        await self._gemini_client.set_thinking_level(ThinkingLevel.MEDIUM)
                                    await self._gemini_client.send_audio(audio_data)
                                    ptt_chunks += 1
                                else:
                                    logger.warning("[audio_chunk] Gemini未接続")
                        elif msg_type == "audio_end":
                            if ptt_start is not None:
                                duration = time.time() - ptt_start
                                logger.info(f"[PTT] 完了 — {duration:.1f}秒 / {ptt_chunks}チャンク")
                                self.push_event("USER_AUDIO", {"duration": round(duration, 1)})
                                ptt_start = None
                                ptt_chunks = 0
                    except Exception:
                        pass
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    break
        finally:
            self._ws_clients.discard(ws)
            logger.debug(f"WebSocket disconnected, total: {len(self._ws_clients)}")

        return ws

    async def _handle_get_config(self, request: web.Request) -> web.Response:
        """Return current config with API key masked."""
        cfg = copy.deepcopy(self._config)
        if cfg.get("gemini", {}).get("api_key"):
            cfg["gemini"]["api_key"] = "***"
        cfg.pop("web", None)
        return web.json_response(cfg)

    async def _handle_post_config(self, request: web.Request) -> web.Response:
        """Update config at runtime and persist to YAML."""
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        restart_required: List[str] = []

        # SSL settings — runtime reflectable
        if "ssl" in data:
            self._config.setdefault("ssl", {})
            for key in ("tracker_addr", "tracker_port", "gc_addr", "gc_port"):
                if key in data["ssl"]:
                    self._config["ssl"][key] = data["ssl"][key]

        # Commentary settings — runtime reflectable
        if "commentary" in data:
            self._config.setdefault("commentary", {}).update(data["commentary"])

        # Gemini settings — require restart
        if "gemini" in data:
            gemini_data = data["gemini"]
            self._config.setdefault("gemini", {})
            if "model" in gemini_data:
                self._config["gemini"]["model"] = gemini_data["model"]
                restart_required.append("gemini.model")
            if "api_key" in gemini_data and gemini_data["api_key"] not in ("***", ""):
                self._config["gemini"]["api_key"] = gemini_data["api_key"]
                restart_required.append("gemini.api_key")

        # Audio settings — require restart
        if "audio" in data:
            self._config.setdefault("audio", {}).update(data["audio"])
            if "device" in data.get("audio", {}):
                restart_required.append("audio.device")

        if self._on_config_update:
            self._on_config_update(self._config)

        # Persist to YAML (exclude web section)
        config_path = self._config_dir / "config.yaml"
        try:
            save_cfg = copy.deepcopy(
                {k: v for k, v in self._config.items() if k != "web"}
            )
            if save_cfg.get("gemini", {}).get("api_key") == "***":
                save_cfg["gemini"]["api_key"] = ""
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(save_cfg, f, allow_unicode=True, default_flow_style=False)
        except Exception as e:
            logger.warning(f"Failed to save config: {e}")

        return web.json_response({"success": True, "restart_required": restart_required})

    async def _handle_streaming_start(self, request: web.Request) -> web.Response:
        """Start the commentary pipeline."""
        if self._on_start_streaming:
            self._on_start_streaming()
        return web.json_response({"success": True})

    async def _handle_streaming_stop(self, request: web.Request) -> web.Response:
        """Stop the commentary pipeline."""
        if self._on_stop_streaming:
            self._on_stop_streaming()
        return web.json_response({"success": True})

    async def _handle_switch_port(self, request: web.Request) -> web.Response:
        """Switch the active port for a given SSL data source."""
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        source = data.get("source")  # "tracker", "gc", or "vision"
        port = data.get("port")
        if source is None or port is None:
            return web.json_response({"error": "Missing 'source' or 'port'"}, status=400)

        if self._on_switch_port is None:
            return web.json_response({"error": "Port switching not available"}, status=503)

        success = self._on_switch_port(source, int(port))
        if not success:
            return web.json_response({"error": f"Invalid source or port: {source}:{port}"}, status=400)

        return web.json_response({"success": True})

    async def _handle_get_status(self, request: web.Request) -> web.Response:
        """Return system status."""
        game_state = self._safe_call(self._writer.get_game_state_data)
        return web.json_response({**self._build_status_dict(), "game_state": game_state})

    async def _handle_get_team_profiles(self, request: web.Request) -> web.Response:
        """Return team profiles."""
        profiles_path = self._config_dir / "team_profiles.yaml"
        try:
            with open(profiles_path, "r", encoding="utf-8") as f:
                profiles = yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning(f"Failed to load team_profiles.yaml: {e}")
            profiles = {}
        return web.json_response(profiles)
