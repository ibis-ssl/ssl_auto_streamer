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
from typing import Any, Dict, List, Optional

from ssl_auto_streamer.data import (
    generate_initial_context,
    get_team_profile_from_data,
    get_team_reading_from_data,
)
from ssl_auto_streamer.statler import WorldModelWriter, WorldModelReader
from ssl_auto_streamer.statler.world_model_writer import DEFAULT_BLUE_TEAM_NAME, DEFAULT_YELLOW_TEAM_NAME
from ssl_auto_streamer.statler.world_model_reader import CommentaryMode
from ssl_auto_streamer.gemini import GeminiLiveApiClient, FunctionHandler, AnalysisAgent, ThinkingLevel
from ssl_auto_streamer.gemini.live_api_client import GeminiConfig
from ssl_auto_streamer.audio import PcmAudioOutput
from ssl_auto_streamer.audio_mode import (
    DEFAULT_AUDIO_OUTPUT_MODE,
    is_valid_audio_output_mode,
    normalize_audio_output_mode,
    uses_client_audio,
    uses_server_audio,
)
from ssl_auto_streamer.event_detector import EventDetector, DetectedEvent
from ssl_auto_streamer.ssl.tracker_client import TrackerClient
from ssl_auto_streamer.ssl.gc_client import GCClient
from ssl_auto_streamer.ssl.vision_client import VisionClient
from ssl_auto_streamer.logger import AiActivityLogger
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
        analysis_agent_cfg = config.get("analysis_agent", {})

        raw_audio_output_mode = audio_cfg.get(
            "output_mode", DEFAULT_AUDIO_OUTPUT_MODE
        )
        if not is_valid_audio_output_mode(raw_audio_output_mode):
            logger.warning(
                "Invalid audio.output_mode=%r; falling back to %s",
                raw_audio_output_mode,
                DEFAULT_AUDIO_OUTPUT_MODE,
            )
        self._audio_output_mode = normalize_audio_output_mode(
            raw_audio_output_mode
        )
        self._config.setdefault("audio", {})["output_mode"] = self._audio_output_mode

        self._initial_context_sent: bool = False

        # Config file directory
        self._config_dir = Path(__file__).parent.parent / "config"

        # Load config files
        self._ssl_rules: Dict = self._load_yaml("ssl_rules.yaml") or {}
        self._team_profiles: Dict = self._load_yaml("team_profiles.yaml") or {}
        self._tournament_context: Dict = self._load_yaml("tournament_context.yaml") or {}

        # Load system instruction
        system_instruction = self._load_text("system_instruction.md") or ""

        # Load tools config
        tools_config = self._load_json("function_declarations.json") or []

        # Initialize Statler components
        self._writer = WorldModelWriter()
        self._reader = WorldModelReader(self._writer)
        self._function_handler = FunctionHandler(
            self._writer,
            team_profiles=self._team_profiles,
            ssl_rules=self._ssl_rules,
        )

        # Initialize AnalysisAgent
        api_key_for_analysis = (
            analysis_agent_cfg.get("api_key")
            or gemini_cfg.get("api_key")
            or os.environ.get("GEMINI_API_KEY", "")
        )
        # request_analysis 自身を除外してループ再帰を防ぐ
        analysis_tool_declarations = [
            d for d in tools_config if d.get("name") != "request_analysis"
        ]
        self._analysis_agent = AnalysisAgent(
            writer=self._writer,
            config={**analysis_agent_cfg, "api_key": api_key_for_analysis},
            tool_declarations=analysis_tool_declarations,
            tool_executor=self._function_handler.handle,
            tournament_context=self._tournament_context,
        )
        self._function_handler.set_analysis_agent(self._analysis_agent)

        # Initialize event detector
        self._event_detector = EventDetector()

        # AI Activity Logger
        ai_logger_cfg = config.get("ai_logger", {})
        self._ai_logger = AiActivityLogger(ai_logger_cfg)
        self._current_turn_id: Optional[str] = None
        self._previous_turn_id: Optional[str] = None
        self._last_request_time: float = 0.0
        self._last_request_priority: int = 0

        # Gemini client (Live API / audio mode)
        api_key = gemini_cfg.get("api_key") or os.environ.get("GEMINI_API_KEY", "")
        gemini_config = GeminiConfig(
            api_key=api_key,
            model=gemini_cfg.get("model", "gemini-3.8-live"),
            sample_rate=gemini_cfg.get("sample_rate", 24000),
            system_instruction=system_instruction,
            tools_config=tools_config,
            thinking_level=gemini_cfg.get("thinking_level", "medium"),
            output_transcription=gemini_cfg.get("output_transcription", True),
            response_mode="audio",
        )
        self._gemini_client = GeminiLiveApiClient(gemini_config)
        self._gemini_client.set_audio_callback(self._on_audio_received)
        self._gemini_client.set_function_call_handler(self._function_handler.handle_async)
        self._gemini_client.set_disconnect_callback(self._on_gemini_disconnected)
        self._gemini_client.set_turn_complete_callback(self._on_turn_complete)
        self._gemini_client.set_interrupted_callback(self._on_gemini_interrupted)
        self._gemini_client.set_transcription_callback(self._on_transcription_received)
        self._gemini_client.set_thought_callback(self._on_thought_received)
        self._gemini_client.set_tool_call_callbacks(
            start_callback=self._on_tool_call_start,
            end_callback=self._on_tool_call_end,
        )

        # Audio output
        self._audio_sample_rate = gemini_cfg.get("sample_rate", 24000)
        audio_device = audio_cfg.get("device") or None
        self._audio_output = PcmAudioOutput(
            sample_rate=self._audio_sample_rate,
            device=audio_device,
        )

        # SSL clients
        tracker_ports = ssl_cfg.get("tracker_ports", [10010, 11010])
        gc_ports = ssl_cfg.get("gc_ports", [10003, 11003])
        vision_ports = ssl_cfg.get("vision_ports", [10006, 10020])
        self._tracker_client = TrackerClient(
            addr=ssl_cfg.get("tracker_addr", "224.5.23.2"),
            ports=tracker_ports,
        )
        self._gc_client = GCClient(
            addr=ssl_cfg.get("gc_addr", "224.5.23.1"),
            ports=gc_ports,
        )
        self._vision_client = VisionClient(
            addr=ssl_cfg.get("vision_addr", "224.5.23.1"),
            ports=vision_ports,
        )
        self._tracker_client.set_callback(self._on_tracker_frame)
        self._gc_client.set_callback(self._on_referee_message)
        self._vision_client.set_geometry_callback(self._on_vision_geometry)

        # Replay settings
        self._replay_log = ssl_cfg.get("replay_log")
        self._replay_loop = bool(ssl_cfg.get("replay_loop", False))
        self._replay_exit_on_finish = bool(ssl_cfg.get("replay_exit_on_finish", False))
        self._replay_task: Optional[asyncio.Task] = None

        # Commentary settings
        self._analyst_threshold = commentary_cfg.get("analyst_silence_threshold", 5.0)
        self._writer_update_rate = commentary_cfg.get("writer_update_rate", 1.0)
        self._interrupt_priority_threshold = commentary_cfg.get("interrupt_priority_threshold", 2)
        self._auto_start = bool(commentary_cfg.get("auto_start", False))

        # State
        self._connected = False
        self._streaming = False
        self._last_event_time: float = time.time()
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 10
        self._next_reconnect_time: float = 0.0
        # gemini-3.1 audio sessions expire at 15 min; refresh 2 min early to avoid mid-match drops
        self._session_refresh_threshold: float = 13 * 60
        self._running = False
        self._pending_tasks: set = set()
        self._last_callback_error_log: Dict[str, float] = {}

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
                get_team_names=lambda: self._writer.get_team_names(),
                on_start_streaming=self._on_web_start_streaming,
                on_stop_streaming=self._on_web_stop_streaming,
                get_streaming=lambda: self._streaming,
                get_audio_output_mode=lambda: self._audio_output_mode,
                on_switch_port=self._on_switch_port,
                get_port_status=self._get_port_status,
                on_start_replay=self.start_replay,
                on_stop_replay=self.stop_replay,
                get_replay_status=self.get_replay_status,
                get_replay_scenarios=self.get_available_scenarios,
                on_run_pytest=self.run_pytest_suite,
                ai_logger=self._ai_logger,
            )

        if uses_client_audio(self._audio_output_mode) and self._web_server is None:
            logger.warning(
                "audio.output_mode=%s requires the Web UI; no audio output "
                "client is available because web.enabled is false",
                self._audio_output_mode,
            )

        # Event cooldowns
        self._last_commentary_time: Dict[str, float] = {}
        self._event_cooldowns = {
            "SHOT": 2.0,
            "FAST_SHOT": 2.0,
            "POSSIBLE_GOAL": 5.0,
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
            "INVALID_GOAL": 5.0,
            "KICKOFF": 5.0,
            "PENALTY": 5.0,
            "FREE_KICK": 3.0,
            "BALL_PLACEMENT": 5.0,
            "BALL_PLACEMENT_SUCCEEDED": 3.0,
            "BALL_PLACEMENT_FAILED": 5.0,
            "PENALTY_KICK_FAILED": 5.0,
            "NO_PROGRESS": 8.0,
            "BOT_SUBSTITUTION": 5.0,
            "CHALLENGE_FLAG": 5.0,
            "EMERGENCY_STOP": 10.0,
            "PREPARED": 3.0,
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

        # Start AnalysisAgent
        await self._analysis_agent.start()

        # Start web server
        if self._web_server:
            try:
                await self._web_server.start()
            except Exception as e:
                logger.error(f"Failed to start web server: {e}")

        tasks = [
            asyncio.create_task(self._analyst_check_loop()),
            asyncio.create_task(self._reconnect_loop()),
        ]

        if self._auto_start:
            logger.info("Auto-start commentary enabled: starting streaming...")
            await self.start_streaming()

        if self._replay_log:
            logger.info(
                f"Replay mode enabled: reading from {self._replay_log} "
                f"(loop={self._replay_loop})"
            )
            self._replay_task = asyncio.create_task(self._run_replay_loop())
            tasks.append(self._replay_task)
        else:
            # Start SSL data receivers (UDP)
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

            try:
                await self._vision_client.start(loop)
                logger.info("Vision client started")
            except Exception as e:
                logger.error(f"Failed to start vision client: {e}")

        logger.info("Waiting for start command from dashboard...")

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    def _start_audio_output(self) -> None:
        """Start server-side audio output when the selected mode uses it."""
        if uses_server_audio(self._audio_output_mode):
            self._audio_output.start()

    def _stop_audio_output(self) -> None:
        """Stop server audio and clear client-side queued audio."""
        if uses_server_audio(self._audio_output_mode):
            self._audio_output.stop()
        if uses_client_audio(self._audio_output_mode) and self._web_server:
            self._web_server.push_audio_control("clear")

    def _clear_audio_output(self) -> int:
        """Clear queued audio for all active output targets and return discarded bytes."""
        discarded_bytes = 0
        if uses_server_audio(self._audio_output_mode):
            discarded_bytes = self._audio_output.clear_buffer()
        if uses_client_audio(self._audio_output_mode) and self._web_server:
            self._web_server.push_audio_control("clear")
        return discarded_bytes

    def _play_audio_output(self, pcm_data: bytes) -> None:
        """Route Gemini output PCM to the selected output target(s)."""
        if uses_server_audio(self._audio_output_mode):
            self._audio_output.play(pcm_data)
        if uses_client_audio(self._audio_output_mode) and self._web_server:
            self._web_server.push_audio_chunk(
                pcm_data,
                sample_rate=self._audio_sample_rate,
                channels=1,
            )

    def _flush_audio_output(self) -> None:
        """Flush server-side tail audio when the selected mode uses it."""
        if uses_server_audio(self._audio_output_mode):
            self._audio_output.flush_buffer()

    @property
    def _is_speaking(self) -> bool:
        """Return True if Gemini is generating or audio is currently playing."""
        if self._gemini_client.is_generating:
            return True
        if uses_server_audio(self._audio_output_mode) and self._audio_output.is_playing:
            return True
        return False

    def _set_audio_output_mode(self, raw_mode: object) -> None:
        """Apply audio output mode changes without restarting the app."""
        if not is_valid_audio_output_mode(raw_mode):
            logger.warning(
                "Invalid audio.output_mode=%r; keeping %s",
                raw_mode,
                self._audio_output_mode,
            )
            return

        new_mode = normalize_audio_output_mode(raw_mode)
        old_mode = self._audio_output_mode
        if new_mode == old_mode:
            return

        if self._streaming:
            if uses_server_audio(old_mode) and not uses_server_audio(new_mode):
                self._audio_output.stop()
            elif not uses_server_audio(old_mode) and uses_server_audio(new_mode):
                self._audio_output.start()

        if self._web_server:
            self._web_server.push_audio_control("clear")

        self._audio_output_mode = new_mode
        self._config.setdefault("audio", {})["output_mode"] = new_mode
        logger.info("Audio output mode updated: %s -> %s", old_mode, new_mode)

    async def start_streaming(self) -> bool:
        """Start commentary pipeline (connect Gemini, start audio)."""
        if self._streaming:
            logger.info("Already streaming")
            return True

        logger.info("Starting streaming...")
        success = await self._gemini_client.connect()
        if success:
            self._connected = True
            self._streaming = True
            self._ai_logger.log_session_start(
                model=self._gemini_client._config.model,
                voice=self._gemini_client._config.voice,
                sample_rate=self._audio_sample_rate,
                thinking_level=self._gemini_client._config.thinking_level,
            )
            self._start_audio_output()
            logger.info(
                "Connected to Gemini API, audio output mode=%s",
                self._audio_output_mode,
            )
            await self._send_initial_context()
            if self._writer.are_team_names_known():
                blue_name, yellow_name = self._writer.get_team_names()
                startup_dict = {
                    "mode": "startup",
                    "instruction": "試合前の挨拶として、対戦カード（両チーム名）と簡単な見どころを述べてください。「システム起動」などのメタ発言は禁止。",
                    "teams": {
                        "blue": get_team_reading_from_data(blue_name, self._team_profiles),
                        "yellow": get_team_reading_from_data(yellow_name, self._team_profiles),
                    },
                }
                startup_msg = json.dumps(startup_dict, ensure_ascii=False)
                turn_id = self._ai_logger.start_turn(
                    trigger_type="startup",
                    priority=1,
                    payload=startup_dict,
                )
                self._previous_turn_id = self._current_turn_id
                self._current_turn_id = turn_id
                await self._gemini_client.set_thinking_level(ThinkingLevel.MEDIUM)
                await self._gemini_client.send_text(startup_msg)
        else:
            logger.warning("Failed to connect to Gemini API")

        return success

    async def stop_streaming(self) -> None:
        """Stop commentary pipeline (disconnect Gemini, stop audio)."""
        if not self._streaming:
            logger.info("Already stopped")
            return

        logger.info("Stopping streaming...")
        self._streaming = False
        self._connected = False
        self._initial_context_sent = False
        self._reconnect_attempts = 0
        self._ai_logger.log_session_end(reason="stopped_by_user")
        if self._gemini_client.is_connected():
            await self._gemini_client.disconnect()
        self._stop_audio_output()
        logger.info("Streaming stopped")

    async def _run_replay_loop(self) -> None:
        """Replay packets from SSL log file and deliver directly to callbacks."""
        from ssl_auto_streamer.ssl.log_reader import (
            MSG_TYPE_SSL_REFBOX_2013,
            MSG_TYPE_SSL_VISION_2014,
            MSG_TYPE_SSL_VISION_TRACKER_2020,
            SSLLogReader,
        )

        try:
            with SSLLogReader(self._replay_log) as reader:
                async for pkt in reader.iter_timed_packets(
                    speed=1.0, loop=self._replay_loop
                ):
                    if not self._running:
                        break

                    if pkt.message_type == MSG_TYPE_SSL_VISION_TRACKER_2020:
                        tracker = pkt.decode()
                        if tracker and tracker.HasField("tracked_frame"):
                            self._on_tracker_frame(tracker.tracked_frame)
                    elif pkt.message_type == MSG_TYPE_SSL_REFBOX_2013:
                        referee = pkt.decode()
                        if referee:
                            self._on_referee_message(referee)
                    elif pkt.message_type == MSG_TYPE_SSL_VISION_2014:
                        wrapper = pkt.decode()
                        if wrapper and wrapper.HasField("geometry"):
                            self._on_vision_geometry(wrapper.geometry)

            logger.info("Log replay finished")
            if self._replay_exit_on_finish and not self._replay_loop:
                logger.info("Replay exit-on-finish requested; waiting 5s for active commentary then shutting down...")
                await asyncio.sleep(5.0)
                self._running = False
        except asyncio.CancelledError:
            logger.info("Log replay cancelled")
        except Exception as e:
            logger.error(f"Error during log replay: {e}", exc_info=True)

    def start_replay(
        self,
        log_path: Optional[str] = None,
        *args,
        loop: bool = False,
        **kwargs,
    ) -> bool:
        """Start log replay dynamically from UI or API (defaults to sample match log).

        Replay is fixed at real-time speed (1.0x). If legacy speed positional/keyword argument
        is provided, it is safely ignored.
        """
        # Handle legacy positional arguments: (log_path, loop) or (log_path, speed, loop)
        if args:
            if len(args) == 1:
                if isinstance(args[0], bool):
                    loop = args[0]
            elif len(args) >= 2:
                if isinstance(args[1], bool):
                    loop = args[1]

        if self._replay_task and not self._replay_task.done():
            logger.warning("Replay is already active")
            return False

        if not log_path:
            default_sample = Path(__file__).parent.parent / "tests" / "data" / "sample_match.log.gz"
            log_path = str(default_sample)

        path_obj = Path(log_path)
        if not path_obj.exists():
            logger.error(f"Replay log not found: {path_obj}")
            return False

        self._replay_log = str(path_obj)
        self._replay_loop = loop
        try:
            loop = asyncio.get_running_loop()
            self._replay_task = loop.create_task(self._run_replay_loop())
        except RuntimeError:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            self._replay_task = loop.create_task(self._run_replay_loop())
        logger.info(f"Started dynamic replay: {self._replay_log} (speed=1.0x)")
        return True

    def stop_replay(self) -> bool:
        """Stop dynamic log replay."""
        if self._replay_task and not self._replay_task.done():
            self._replay_task.cancel()
            self._replay_task = None
            logger.info("Dynamic replay stopped")
            return True
        return False

    def get_replay_status(self) -> Dict[str, Any]:
        """Return current replay status."""
        active = bool(self._replay_task and not self._replay_task.done())
        return {
            "active": active,
            "log_path": self._replay_log if active else None,
            "speed": 1.0,
            "loop": self._replay_loop,
        }

    def get_available_scenarios(self) -> List[Dict[str, Any]]:
        """Return list of available scenario logs for replay."""
        scenarios_dir = Path(__file__).parent.parent / "tests" / "data" / "scenarios"
        sample_log = Path(__file__).parent.parent / "tests" / "data" / "sample_match.log.gz"

        metadata_map = {
            "scenario_8_tactical_foul_and_card.log.gz": {
                "name": "シナリオ8: 危険な衝突・イエローカード・直接FK (40秒)",
                "category": "長尺マッチ",
                "description": "激しい衝突タックル、STOP、イエローカード提示、戦術解説、直接FK、ボールアウト、ボールプレイスメント",
                "recommended": True,
            },
            "scenario_7_counter_attack_goal.log.gz": {
                "name": "シナリオ7: カウンター速攻・リバウンドゴール・VAR (42秒)",
                "category": "長尺マッチ",
                "description": "キックオフ、パス連携、インターセプト、高速カウンター、シュート、セーブ、リバウンドゴール、判定審議、ゴール確定",
                "recommended": True,
            },
            "sample_match.log.gz": {
                "name": "サンプルマッチ: フルゲーム攻防 (65秒)",
                "category": "長尺マッチ",
                "description": "試合開始から複数回の攻防、シュート、STOP、解説モード遷移を含む総合マッチログ",
                "recommended": True,
            },
            "scenario_1_goal.log.gz": {
                "name": "シナリオ1: シュート・ゴール (10秒)",
                "category": "基本シナリオ",
                "description": "パスから高速シュート、ゴール判定まで",
                "recommended": False,
            },
            "scenario_2_foul_card.log.gz": {
                "name": "シナリオ2: 衝突ファール・STOP (8秒)",
                "category": "基本シナリオ",
                "description": "ロボット同士の衝突とSTOP判定",
                "recommended": False,
            },
            "scenario_3_pass_chain.log.gz": {
                "name": "シナリオ3: 連続パス展開 (8秒)",
                "category": "基本シナリオ",
                "description": "複数ロボット間でのパス連携",
                "recommended": False,
            },
            "scenario_4_penalty_save.log.gz": {
                "name": "シナリオ4: ペナルティキック・GKセーブ (8秒)",
                "category": "基本シナリオ",
                "description": "PK準備からキック、GKによるファインセーブ",
                "recommended": False,
            },
            "scenario_5_ball_out.log.gz": {
                "name": "シナリオ5: タッチラインボールアウト (10秒)",
                "category": "基本シナリオ",
                "description": "ドリブルからラインアウト、STOP判定",
                "recommended": False,
            },
            "scenario_6_minimal_smoke.log.gz": {
                "name": "シナリオ6: 最小スモークテスト (1秒)",
                "category": "基本シナリオ",
                "description": "1パケットのみの最小疎通テスト",
                "recommended": False,
            },
        }

        results: List[Dict[str, Any]] = []

        if sample_log.exists():
            meta = metadata_map.get(sample_log.name, {})
            results.append({
                "id": "sample_match",
                "filename": sample_log.name,
                "path": str(sample_log),
                "name": meta.get("name", sample_log.name),
                "category": meta.get("category", "長尺マッチ"),
                "description": meta.get("description", ""),
                "recommended": meta.get("recommended", True),
            })

        if scenarios_dir.exists():
            for p in sorted(scenarios_dir.glob("*.log.gz")):
                meta = metadata_map.get(p.name, {})
                results.append({
                    "id": p.stem.replace(".log", ""),
                    "filename": p.name,
                    "path": str(p),
                    "name": meta.get("name", p.name),
                    "category": meta.get("category", "基本シナリオ"),
                    "description": meta.get("description", ""),
                    "recommended": meta.get("recommended", False),
                })

        # Recommended first, then by category (長尺マッチ first), then by name
        results.sort(key=lambda x: (not x.get("recommended", False), x.get("category") != "長尺マッチ", x.get("name", "")))
        return results

    async def run_pytest_suite(self) -> Dict[str, Any]:
        """Run pytest test suite in background and return output summary."""
        import subprocess

        proc = await asyncio.create_subprocess_exec(
            "uv", "run", "pytest",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": ""},
        )
        stdout, stderr = await proc.communicate()
        success = (proc.returncode == 0)
        out_text = stdout.decode("utf-8", errors="replace") + stderr.decode("utf-8", errors="replace")
        return {
            "success": success,
            "returncode": proc.returncode,
            "output": out_text,
        }

    async def shutdown(self) -> None:
        """Graceful shutdown."""
        self._running = False
        self._streaming = False
        logger.info("Shutting down...")

        if self._replay_task and not self._replay_task.done():
            self._replay_task.cancel()

        self._tracker_client.stop()
        self._gc_client.stop()
        self._vision_client.stop()

        if self._connected:
            await self._gemini_client.disconnect()

        self._stop_audio_output()
        await self._analysis_agent.close()

        if self._web_server:
            await self._web_server.stop()

        self._ai_logger.close()
        logger.info("Shutdown complete")

    def _log_callback_error(self, source: str, message: str) -> None:
        """Rate-limited warning logger for source callback exceptions (5 s cooldown)."""
        now = time.time()
        if now - self._last_callback_error_log.get(source, 0.0) < 5.0:
            return
        self._last_callback_error_log[source] = now
        logger.warning(message, exc_info=True)

    def _on_tracker_frame(self, frame: Any) -> None:
        """Handle TrackedFrame from TrackerClient."""
        try:
            self._writer.update_from_tracker(frame)
            if self._web_server:
                self._web_server.update_tracker_seen()
            events = self._event_detector.update_from_tracker(frame)
            for event in events:
                self._on_detected_event(event)
        except Exception:
            self._log_callback_error("tracker", "Tracker frame processing error")

    def _on_switch_port(self, source: str, port: int) -> bool:
        """Switch the active port for a given SSL data source."""
        if source == "tracker":
            return self._tracker_client.switch_port(port)
        elif source == "gc":
            return self._gc_client.switch_port(port)
        elif source == "vision":
            return self._vision_client.switch_port(port)
        return False

    def _get_port_status(self) -> Dict[str, Any]:
        """Return port status for all SSL data sources."""
        return {
            "tracker": self._tracker_client.get_port_status(),
            "gc": self._gc_client.get_port_status(),
            "vision": self._vision_client.get_port_status(),
        }

    def _on_vision_geometry(self, geometry: Any) -> None:
        """Handle SSL_GeometryData from VisionClient."""
        try:
            self._writer.update_from_geometry(geometry)
        except Exception:
            self._log_callback_error("vision", "Vision geometry processing error")

    def _on_referee_message(self, referee: Any) -> None:
        """Handle Referee message from GCClient."""
        try:
            if self._web_server:
                self._web_server.update_gc_seen()
            self._writer.update_from_referee(referee)
            self._check_team_names_from_referee(referee)
            events = self._event_detector.update_from_referee(referee)
            for event in events:
                self._on_detected_event(event)
        except Exception:
            self._log_callback_error("referee", "Referee message processing error")

    def _check_team_names_from_referee(self, referee: Any) -> None:
        """Check if team names are available and send initial context / team update."""
        if not self._connected:
            return
        if not self._initial_context_sent and self._writer.are_team_names_known():
            asyncio.create_task(self._send_initial_context())
        elif self._initial_context_sent and self._writer.consume_team_names_changed():
            asyncio.create_task(self._send_team_update())

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
            # Convert team color keys to readable names if present.
            metadata = dict(event.metadata)
            for team_key_field, team_name_field in (
                ("by_team", "by_team_name"),
                ("team", "team_name"),
            ):
                if team_key_field not in metadata:
                    continue
                team_key = metadata[team_key_field]
                if team_key not in ("blue", "yellow"):
                    continue
                blue_name, yellow_name = self._writer.get_team_names()
                team_name = blue_name if team_key == "blue" else yellow_name
                metadata[team_name_field] = get_team_reading_from_data(
                    team_name, self._team_profiles
                )
            event_data["metadata"] = metadata

        logger.info(f"Detected event: {event.event_type}")
        self._writer.add_event(event.event_type, event_data)

        if self._web_server:
            self._web_server.push_event(event.event_type, event_data)

        if event_data.get("metadata", {}).get("log_only"):
            return

        if not self._connected or not self._streaming:
            return

        # Check cooldown
        current_time = time.time()
        cooldown = self._event_cooldowns.get(event.event_type, 1.0)
        last_time = self._last_commentary_time.get(event.event_type, 0.0)

        if current_time - last_time < cooldown:
            logger.info(f"Skipping {event.event_type} (cooldown)")
            return

        # Generate reflex commentary
        prev_mode = self._reader.get_mode()
        self._reader.set_mode(CommentaryMode.REFLEX)
        request = self._reader.generate_reflex(event.event_type, event_data)

        # 直前リクエストとの短時間競合ガード（0.8秒以内）: 新イベントの優先度が直前以下ならスキップ
        if (
            current_time - self._last_request_time < 0.8
            and request.priority <= self._last_request_priority
        ):
            logger.info(
                f"Skipping {event.event_type} (rapid succession: prio {request.priority} <= last prio {self._last_request_priority})"
            )
            return

        if request.priority >= 1:
            if self._is_speaking:
                if (
                    request.priority >= self._interrupt_priority_threshold
                    or prev_mode == CommentaryMode.ANALYST
                ):
                    logger.info(f"Barge-in triggered by {event.event_type} (priority={request.priority})")
                    discarded = self._clear_audio_output()
                    self._ai_logger.log_utterance_interrupted(
                        self._current_turn_id,
                        discarded,
                        reason=f"barge_in_by_{event.event_type}",
                    )
                else:
                    logger.info(
                        f"Skipping {event.event_type} (currently speaking, prio {request.priority} < threshold {self._interrupt_priority_threshold})"
                    )
                    return

            json_payload = self._reader.to_gemini_json(request)
            turn_id = self._ai_logger.start_turn(
                trigger_type="reflex",
                event_type=event.event_type,
                priority=request.priority,
                payload=json_payload,
            )
            self._previous_turn_id = self._current_turn_id
            self._current_turn_id = turn_id
            self._last_request_time = current_time
            self._last_request_priority = request.priority
            logger.info(f"Sending reflex commentary for {event.event_type} (turn={turn_id})")
            asyncio.create_task(self._send_reflex(json_payload, request.priority))
            self._last_commentary_time[event.event_type] = current_time
            if self._web_server:
                self._web_server.push_commentary(
                    f"[{event.event_type}] {event_data.get('metadata', {}).get('team', '')}"
                )

    async def _analyst_check_loop(self) -> None:
        """Periodic loop to check if analyst commentary should be triggered."""
        while self._running:
            await asyncio.sleep(1.0)

            if not self._connected or not self._streaming:
                continue

            if self._is_speaking:
                continue

            silence_duration = time.time() - self._last_event_time
            if silence_duration > self._analyst_threshold:
                if self._reader.get_mode() != CommentaryMode.ANALYST:
                    self._reader.set_mode(CommentaryMode.ANALYST)
                    logger.info("Switching to analyst mode")

                    request = self._reader.generate_analysis()
                    if request:
                        json_payload = self._reader.to_gemini_json(request)
                        turn_id = self._ai_logger.start_turn(
                            trigger_type="analyst",
                            priority=1,
                            payload=json_payload,
                        )
                        self._current_turn_id = turn_id
                        await self._gemini_client.set_thinking_level(ThinkingLevel.HIGH)
                        await self._gemini_client.send_text(json_payload)
                        if self._web_server:
                            self._web_server.push_commentary("[アナリスト実況]")

    async def _reconnect_loop(self) -> None:
        """Periodic reconnection loop."""
        while self._running:
            await asyncio.sleep(5.0)

            if not self._streaming:
                continue

            if self._connected and not self._gemini_client.is_connected():
                logger.warning("Gemini connection lost")
                self._connected = False

            if self._connected:
                self._reconnect_attempts = 0
                if self._gemini_client.session_age > self._session_refresh_threshold:
                    logger.info("Session approaching 15-min limit, refreshing connection...")
                    await self._gemini_client.disconnect()
                    self._connected = False
                    self._initial_context_sent = False
                continue

            if self._reconnect_attempts >= self._max_reconnect_attempts:
                logger.error("Max reconnect attempts reached. Giving up.")
                self._streaming = False
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
                self._ai_logger.log_session_start(
                    model=self._gemini_client._config.model,
                    voice=self._gemini_client._config.voice,
                    sample_rate=self._audio_sample_rate,
                    thinking_level=self._gemini_client._config.thinking_level,
                )
                self._start_audio_output()
                logger.info(
                    "Reconnected to Gemini API, audio output mode=%s",
                    self._audio_output_mode,
                )
                await self._send_initial_context()
            else:
                self._next_reconnect_time = time.time() + backoff
                logger.warning(f"Reconnect failed, next attempt in {backoff:.0f}s")

    def _on_gemini_disconnected(self) -> None:
        """Called by GeminiLiveApiClient when WebSocket closes."""
        logger.warning("Gemini API disconnected")
        self._connected = False
        self._ai_logger.log_session_end(reason="disconnected")

    def _on_gemini_interrupted(self) -> None:
        """Called when Gemini Live API signals output speech was interrupted."""
        logger.info("Gemini server interrupted output speech")
        discarded = self._clear_audio_output()
        target_turn_id = self._current_turn_id
        if self._current_turn_id:
            curr = self._ai_logger.get_turn(self._current_turn_id)
            if (
                curr
                and curr.first_audio_at is None
                and curr.audio_total_bytes == 0
                and self._previous_turn_id
            ):
                target_turn_id = self._previous_turn_id

        if target_turn_id:
            self._ai_logger.log_utterance_interrupted(
                target_turn_id,
                discarded,
                reason="server_interrupted",
            )

    def _on_tool_call_start(
        self, call_id: str, name: str, args: Dict[str, Any]
    ) -> None:
        """Called when Gemini Live API requests a function call."""
        self._ai_logger.log_tool_call_start(
            self._current_turn_id,
            call_id=call_id,
            name=name,
            args=args,
            source="live_api",
        )

    def _on_tool_call_end(
        self,
        call_id: str,
        name: str,
        result: Any,
        latency_ms: float,
        error: Optional[str] = None,
    ) -> None:
        """Called when a function call execution finishes."""
        self._ai_logger.log_tool_call_end(
            self._current_turn_id,
            call_id=call_id,
            name=name,
            result=result,
            latency_ms=latency_ms,
            error=error,
            source="live_api",
        )

    def _on_audio_received(self, pcm_data: bytes) -> None:
        """Handle received audio from Gemini (audio mode)."""
        self._ai_logger.log_audio_received(self._current_turn_id, len(pcm_data))
        self._play_audio_output(pcm_data)

    def _on_transcription_received(self, text: str) -> None:
        """Handle output audio transcription from Gemini."""
        self._ai_logger.log_transcription(self._current_turn_id, text)
        if self._web_server:
            self._web_server.push_transcription(text)

    def _on_thought_received(self, text: str) -> None:
        """Handle reasoning/thought stream from Gemini."""
        self._ai_logger.log_thought(self._current_turn_id, text)
        if self._web_server:
            self._web_server.push_thought(text)

    def _on_turn_complete(self) -> None:
        """Handle end of Gemini turn."""
        self._flush_audio_output()
        self._ai_logger.log_utterance_complete(self._current_turn_id)

    async def _send_initial_context(self) -> None:
        """Send SSL rules and team info as initial context."""
        if self._initial_context_sent or not self._connected:
            return

        blue_name, yellow_name = self._writer.get_team_names()
        context = generate_initial_context(
            ssl_rules=self._ssl_rules,
            team_profiles=self._team_profiles,
            blue_team_name=blue_name if blue_name != DEFAULT_BLUE_TEAM_NAME else None,
            yellow_team_name=yellow_name if yellow_name != DEFAULT_YELLOW_TEAM_NAME else None,
            tournament_context=self._tournament_context,
        )
        logger.info("Sending initial context to Gemini")
        payload = f"[SYSTEM CONTEXT]\n{context}"
        turn_id = self._ai_logger.start_turn(
            trigger_type="initial_context",
            priority=0,
            payload=payload,
        )
        self._current_turn_id = turn_id
        await self._gemini_client.set_thinking_level(ThinkingLevel.MINIMAL)
        await self._gemini_client.send_text(payload)
        self._initial_context_sent = True

    async def _send_reflex(self, payload: str, priority: int) -> None:
        level = ThinkingLevel.MINIMAL if priority == 0 else ThinkingLevel.LOW
        await self._gemini_client.set_thinking_level(level)
        await self._gemini_client.send_text(payload)

    def _fire_and_forget(self, coro: Any) -> None:
        """Schedule a coroutine and keep a strong reference to prevent GC."""
        task = asyncio.create_task(coro)
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    def _on_web_start_streaming(self) -> None:
        """Called from Web UI to start the commentary pipeline."""
        self._fire_and_forget(self.start_streaming())

    def _on_web_stop_streaming(self) -> None:
        """Called from Web UI to stop the commentary pipeline."""
        self._fire_and_forget(self.stop_streaming())

    def _on_web_config_update(self, config: Dict[str, Any]) -> None:
        """Apply runtime-reflectable config changes from Web UI."""
        commentary_cfg = config.get("commentary", {})
        audio_cfg = config.get("audio", {})

        self._analyst_threshold = commentary_cfg.get(
            "analyst_silence_threshold", self._analyst_threshold
        )
        self._writer_update_rate = commentary_cfg.get(
            "writer_update_rate", self._writer_update_rate
        )
        self._interrupt_priority_threshold = commentary_cfg.get(
            "interrupt_priority_threshold", self._interrupt_priority_threshold
        )
        self._set_audio_output_mode(
            audio_cfg.get("output_mode", self._audio_output_mode)
        )
        logger.info("Config updated from Web UI")

    async def _send_team_update(self) -> None:
        """Send team information update to Gemini."""
        if not self._connected:
            return

        blue_name, yellow_name = self._writer.get_team_names()
        update: Dict[str, Any] = {
            "type": "team_update",
            "instruction": "チーム情報が更新されました。以降の実況ではこのチーム名（reading）を使用してください。",
        }

        if blue_name and blue_name != DEFAULT_BLUE_TEAM_NAME:
            update["blue_team"] = {
                "name": get_team_reading_from_data(blue_name, self._team_profiles),
                "key": blue_name,
                **get_team_profile_from_data(blue_name, self._team_profiles),
            }
        if yellow_name and yellow_name != DEFAULT_YELLOW_TEAM_NAME:
            update["yellow_team"] = {
                "name": get_team_reading_from_data(yellow_name, self._team_profiles),
                "key": yellow_name,
                **get_team_profile_from_data(yellow_name, self._team_profiles),
            }

        update_json = json.dumps(update, ensure_ascii=False, indent=2)
        turn_id = self._ai_logger.start_turn(
            trigger_type="team_update",
            priority=0,
            payload=update,
        )
        self._current_turn_id = turn_id
        await self._gemini_client.set_thinking_level(ThinkingLevel.MINIMAL)
        await self._gemini_client.send_text(f"[TEAM UPDATE]\n{update_json}")
