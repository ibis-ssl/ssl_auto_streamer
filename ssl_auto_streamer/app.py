# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""CommentaryApp - asyncio-based SSL commentary application."""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from ssl_auto_streamer.data import (
    generate_initial_context,
    get_team_profile_from_data,
    get_team_reading_from_data,
)
from ssl_auto_streamer.statler import WorldModelWriter, WorldModelReader
from ssl_auto_streamer.statler.world_model_reader import CommentaryMode
from ssl_auto_streamer.gemini import GeminiLiveApiClient, FunctionHandler
from ssl_auto_streamer.gemini.live_api_client import GeminiConfig
from ssl_auto_streamer.audio import PcmAudioOutput
from ssl_auto_streamer.event_detector import EventDetector, DetectedEvent
from ssl_auto_streamer.ssl.tracker_client import TrackerClient
from ssl_auto_streamer.ssl.gc_client import GCClient
from ssl_auto_streamer.web.server import WebServer

logger = logging.getLogger(__name__)


class CommentaryApp:
    """
    SSL Auto Commentary Application.

    asyncio-based replacement for the ROS2 CommentaryNode.
    Receives data from SSL Vision Tracker and Game Controller via UDP multicast.
    """

    def __init__(self, config: Dict[str, Any]):
        self._config = config

        ssl_cfg = config.get("ssl", {})
        gemini_cfg = config.get("gemini", {})
        audio_cfg = config.get("audio", {})
        commentary_cfg = config.get("commentary", {})

        self._our_team_is_blue = ssl_cfg.get("our_team_color", "blue") == "blue"
        self._our_team_name: str = ssl_cfg.get("our_team_name", "ibis")
        self._their_team_name: Optional[str] = None
        self._initial_context_sent: bool = False

        # Config file directory
        self._config_dir = Path(__file__).parent.parent / "config"

        # Load config files
        self._ssl_rules: Dict = self._load_yaml("ssl_rules.yaml") or {}
        self._team_profiles: Dict = self._load_yaml("team_profiles.yaml") or {}

        # Load system instruction
        system_instruction = self._load_text("system_instruction.md") or ""

        # Load tools config
        tools_config = self._load_json("function_declarations.json") or []

        # Initialize Statler components
        self._writer = WorldModelWriter()
        self._reader = WorldModelReader(self._writer)
        self._function_handler = FunctionHandler(self._writer)

        # Initialize event detector
        self._event_detector = EventDetector(self._our_team_is_blue)

        # Gemini client
        api_key = gemini_cfg.get("api_key") or os.environ.get("GEMINI_API_KEY", "")
        gemini_config = GeminiConfig(
            api_key=api_key,
            model=gemini_cfg.get("model", "gemini-2.0-flash-exp"),
            sample_rate=gemini_cfg.get("sample_rate", 24000),
            system_instruction=system_instruction,
            tools_config=tools_config,
        )
        self._gemini_client = GeminiLiveApiClient(gemini_config)
        self._gemini_client.set_audio_callback(self._on_audio_received)
        self._gemini_client.set_function_call_handler(self._function_handler.handle)
        self._gemini_client.set_disconnect_callback(self._on_gemini_disconnected)
        self._gemini_client.set_turn_complete_callback(self._on_turn_complete)

        # Audio output
        sample_rate = gemini_cfg.get("sample_rate", 24000)
        audio_device = audio_cfg.get("device") or None
        self._audio_output = PcmAudioOutput(
            sample_rate=sample_rate,
            device=audio_device,
        )

        # SSL clients
        self._tracker_client = TrackerClient(
            addr=ssl_cfg.get("tracker_addr", "224.5.23.2"),
            port=ssl_cfg.get("tracker_port", 10010),
        )
        self._gc_client = GCClient(
            addr=ssl_cfg.get("gc_addr", "224.5.23.1"),
            port=ssl_cfg.get("gc_port", 10003),
        )
        self._tracker_client.set_callback(self._on_tracker_frame)
        self._gc_client.set_callback(self._on_referee_message)

        # Commentary settings
        self._analyst_threshold = commentary_cfg.get("analyst_silence_threshold", 5.0)
        self._writer_update_rate = commentary_cfg.get("writer_update_rate", 1.0)

        # State
        self._connected = False
        self._last_event_time: float = time.time()
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 10
        self._next_reconnect_time: float = 0.0
        self._running = False

        # Web server (optional)
        web_cfg = config.get("web", {})
        self._web_server: Optional[WebServer] = None
        if web_cfg.get("enabled", True):
            self._web_server = WebServer(
                host=web_cfg.get("host", "0.0.0.0"),
                port=web_cfg.get("port", 8080),
                writer=self._writer,
                gemini_client=self._gemini_client,
                config=config,
                config_dir=self._config_dir,
                on_config_update=self._on_web_config_update,
                get_team_names=lambda: (self._our_team_name, self._their_team_name),
            )

        # Event cooldowns
        self._last_commentary_time: Dict[str, float] = {}
        self._event_cooldowns = {
            "POSSESSION_CHANGE": 3.0,
            "SHOT": 2.0,
            "FAST_SHOT": 2.0,
            "GOAL": 5.0,
            "BALL_OUT": 3.0,
            "SET_PLAY": 5.0,
            "PASS": 2.0,
            "HALT": 3.0,
            "STOP": 3.0,
            "INPLAY_START": 2.0,
            "TIMEOUT": 10.0,
            "HALF_TIME": 10.0,
            "GAME_END": 10.0,
            "FOUL": 5.0,
            "COLLISION": 4.0,
        }

    def _load_yaml(self, filename: str) -> Optional[Dict]:
        import yaml
        path = self._config_dir / filename
        try:
            with open(path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
        except Exception as e:
            logger.warning(f"Failed to load {filename}: {e}")
            return None

    def _load_json(self, filename: str) -> Optional[Any]:
        path = self._config_dir / filename
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load {filename}: {e}")
            return None

    def _load_text(self, filename: str) -> Optional[str]:
        path = self._config_dir / filename
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            logger.warning(f"Failed to load {filename}: {e}")
            return None

    async def run(self) -> None:
        """Main application loop."""
        self._running = True
        logger.info("SSL Auto Streamer starting...")

        loop = asyncio.get_event_loop()

        # Start web server
        if self._web_server:
            try:
                await self._web_server.start()
            except Exception as e:
                logger.error(f"Failed to start web server: {e}")

        # Start SSL data receivers
        try:
            await self._tracker_client.start(loop)
            logger.info("Tracker client started")
        except Exception as e:
            logger.error(f"Failed to start tracker client: {e}")

        try:
            await self._gc_client.start(loop)
            logger.info("GC client started")
        except Exception as e:
            logger.error(f"Failed to start GC client: {e}")

        # Connect to Gemini
        success = await self._gemini_client.connect()
        if success:
            self._connected = True
            self._audio_output.start()
            logger.info("Connected to Gemini API, audio started")
            await self._send_initial_context()
            await self._gemini_client.send_text("実況システム起動。RoboCup SSL の実況を開始します。")
        else:
            logger.warning("Running without Gemini API connection")

        # Run main loop tasks
        tasks = [
            asyncio.create_task(self._analyst_check_loop()),
            asyncio.create_task(self._reconnect_loop()),
        ]

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Graceful shutdown."""
        self._running = False
        logger.info("Shutting down...")

        self._tracker_client.stop()
        self._gc_client.stop()

        if self._connected:
            await self._gemini_client.disconnect()

        self._audio_output.stop()

        if self._web_server:
            await self._web_server.stop()

        logger.info("Shutdown complete")

    def _on_tracker_frame(self, frame: Any) -> None:
        """Handle TrackedFrame from TrackerClient."""
        try:
            self._writer.update_from_tracker(frame, self._our_team_is_blue)
            if self._web_server:
                self._web_server.update_tracker_seen()
            events = self._event_detector.update_from_tracker(frame)
            for event in events:
                self._on_detected_event(event)
        except Exception as e:
            logger.debug(f"Tracker frame processing error: {e}")

    def _on_referee_message(self, referee: Any) -> None:
        """Handle Referee message from GCClient."""
        try:
            self._writer.update_from_referee(referee, self._our_team_is_blue)
            if self._web_server:
                self._web_server.update_gc_seen()
            self._update_team_names_from_referee(referee)
            events = self._event_detector.update_from_referee(referee)
            for event in events:
                self._on_detected_event(event)
        except Exception as e:
            logger.debug(f"Referee message processing error: {e}")

    def _update_team_names_from_referee(self, referee: Any) -> None:
        """Extract team names from Referee message and send initial context."""
        try:
            if self._our_team_is_blue:
                our_name = referee.blue.name
                their_name = referee.yellow.name
            else:
                our_name = referee.yellow.name
                their_name = referee.blue.name

            changed = False
            if our_name and our_name != self._our_team_name:
                self._our_team_name = our_name
                changed = True
            if their_name and their_name != self._their_team_name:
                self._their_team_name = their_name
                changed = True

            if not self._initial_context_sent and self._their_team_name and self._connected:
                asyncio.create_task(self._send_initial_context())
            elif self._initial_context_sent and changed and self._connected:
                asyncio.create_task(self._send_team_update())
        except Exception as e:
            logger.debug(f"Team name extraction error: {e}")

    def _on_detected_event(self, event: DetectedEvent) -> None:
        """Handle a detected game event."""
        self._last_event_time = time.time()

        event_data = {
            "position": {"x": event.position[0], "y": event.position[1]},
            "ball_speed": event.ball_speed,
            "confidence": event.confidence,
        }
        if event.primary_robot:
            event_data["primary_robot"] = event.primary_robot
        if event.secondary_robot:
            event_data["secondary_robot"] = event.secondary_robot
        if event.metadata:
            # Convert team name if present
            metadata = dict(event.metadata)
            if "team" in metadata:
                metadata["team"] = get_team_reading_from_data(
                    metadata["team"], self._team_profiles
                )
            event_data["metadata"] = metadata

        logger.info(f"Detected event: {event.event_type}")
        self._writer.add_event(event.event_type, event_data)

        if self._web_server:
            self._web_server.push_event(event.event_type, event_data)

        if not self._connected:
            return

        # Check cooldown
        current_time = time.time()
        cooldown = self._event_cooldowns.get(event.event_type, 1.0)
        last_time = self._last_commentary_time.get(event.event_type, 0.0)

        if current_time - last_time < cooldown:
            logger.info(f"Skipping {event.event_type} (cooldown)")
            return

        # Generate reflex commentary
        self._reader.set_mode(CommentaryMode.REFLEX)
        request = self._reader.generate_reflex(event.event_type, event_data)

        if request.priority >= 1:
            json_payload = self._reader.to_gemini_json(request)
            logger.info(f"Sending reflex commentary for {event.event_type}")
            asyncio.create_task(self._gemini_client.send_text(json_payload))
            self._last_commentary_time[event.event_type] = current_time
            if self._web_server:
                self._web_server.push_commentary(
                    f"[{event.event_type}] {event_data.get('metadata', {}).get('team', '')}"
                )

    async def _analyst_check_loop(self) -> None:
        """Periodic loop to check if analyst commentary should be triggered."""
        while self._running:
            await asyncio.sleep(1.0)

            if not self._connected:
                continue

            silence_duration = time.time() - self._last_event_time
            if silence_duration > self._analyst_threshold:
                if self._reader.get_mode() != CommentaryMode.ANALYST:
                    self._reader.set_mode(CommentaryMode.ANALYST)
                    logger.info("Switching to analyst mode")

                    request = self._reader.generate_analysis()
                    if request:
                        json_payload = self._reader.to_gemini_json(request)
                        await self._gemini_client.send_text(json_payload)
                        if self._web_server:
                            self._web_server.push_commentary("[アナリスト実況]")

    async def _reconnect_loop(self) -> None:
        """Periodic reconnection loop."""
        while self._running:
            await asyncio.sleep(5.0)

            if self._connected and not self._gemini_client.is_connected():
                logger.warning("Gemini connection lost")
                self._connected = False

            if self._connected:
                self._reconnect_attempts = 0
                continue

            if self._reconnect_attempts >= self._max_reconnect_attempts:
                logger.error("Max reconnect attempts reached. Giving up.")
                break

            current_time = time.time()
            if current_time < self._next_reconnect_time:
                continue

            backoff = min(5.0 * (2 ** self._reconnect_attempts), 300.0)
            self._reconnect_attempts += 1

            logger.info(
                f"Reconnect attempt {self._reconnect_attempts}/{self._max_reconnect_attempts}"
            )

            success = await self._gemini_client.connect()
            if success:
                self._connected = True
                self._reconnect_attempts = 0
                self._initial_context_sent = False
                self._audio_output.start()
                logger.info("Reconnected to Gemini API")
                await self._send_initial_context()
            else:
                self._next_reconnect_time = time.time() + backoff
                logger.warning(f"Reconnect failed, next attempt in {backoff:.0f}s")

    def _on_gemini_disconnected(self) -> None:
        """Called by GeminiLiveApiClient when WebSocket closes."""
        logger.warning("Gemini API disconnected")
        self._connected = False

    def _on_audio_received(self, pcm_data: bytes) -> None:
        """Handle received audio from Gemini."""
        self._audio_output.play(pcm_data)

    def _on_turn_complete(self) -> None:
        """Flush tail audio when Gemini signals end of turn."""
        self._audio_output.flush_buffer()

    async def _send_initial_context(self) -> None:
        """Send SSL rules and team info as initial context."""
        if self._initial_context_sent or not self._connected:
            return

        context = generate_initial_context(
            ssl_rules=self._ssl_rules,
            team_profiles=self._team_profiles,
            our_team_name=self._our_team_name,
            their_team_name=self._their_team_name,
        )
        logger.info("Sending initial context to Gemini")
        await self._gemini_client.send_text(f"[SYSTEM CONTEXT]\n{context}")
        self._initial_context_sent = True

    def _on_web_config_update(self, config: Dict[str, Any]) -> None:
        """Apply runtime-reflectable config changes from Web UI."""
        ssl_cfg = config.get("ssl", {})
        commentary_cfg = config.get("commentary", {})

        self._our_team_is_blue = ssl_cfg.get("our_team_color", "blue") == "blue"
        self._our_team_name = ssl_cfg.get("our_team_name", self._our_team_name)
        self._analyst_threshold = commentary_cfg.get(
            "analyst_silence_threshold", self._analyst_threshold
        )
        self._writer_update_rate = commentary_cfg.get(
            "writer_update_rate", self._writer_update_rate
        )
        logger.info("Config updated from Web UI")

    async def _send_team_update(self) -> None:
        """Send team information update to Gemini."""
        if not self._connected:
            return

        update = {
            "type": "team_update",
            "our_team": {
                "name": get_team_reading_from_data(
                    self._our_team_name, self._team_profiles
                ),
                "key": self._our_team_name,
                **get_team_profile_from_data(self._our_team_name, self._team_profiles),
            },
        }

        if self._their_team_name:
            update["their_team"] = {
                "name": get_team_reading_from_data(
                    self._their_team_name, self._team_profiles
                ),
                "key": self._their_team_name,
                **get_team_profile_from_data(
                    self._their_team_name, self._team_profiles
                ),
            }

        update_json = json.dumps(update, ensure_ascii=False, indent=2)
        await self._gemini_client.send_text(f"[TEAM UPDATE]\n{update_json}")
