# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""Hybrid Event Detector - combines GC GameEvents and Tracker heuristics."""

import hashlib
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from google.protobuf.descriptor import FieldDescriptor


@dataclass
class DetectedEvent:
    """A detected game event."""

    event_type: str  # "GOAL", "SHOT", "PASS", etc.
    position: Tuple[float, float]
    ball_speed: float
    confidence: float
    primary_robot: Optional[Dict] = None   # {"id": int, "team": str}
    secondary_robot: Optional[Dict] = None
    metadata: Dict = field(default_factory=dict)


# GC GameEvent type -> DetectedEvent type mapping.
# Keep every oneof field from proto/ssl_gc_game_event.proto represented here so
# valid GC events are never silently dropped.
_GC_EVENT_MAP = {
    # Goals
    "GOAL": "GOAL",
    "POSSIBLE_GOAL": "SHOT",
    "INVALID_GOAL": "INVALID_GOAL",
    "INDIRECT_GOAL": "INVALID_GOAL",
    "CHIPPED_GOAL": "INVALID_GOAL",
    # Ball out
    "BALL_LEFT_FIELD_TOUCH_LINE": "BALL_OUT",
    "BALL_LEFT_FIELD_GOAL_LINE": "BALL_OUT",
    "AIMLESS_KICK": "BALL_OUT",
    # Collisions
    "BOT_CRASH_UNIQUE": "COLLISION",
    "BOT_CRASH_DRAWN": "COLLISION",
    "BOT_PUSHED_BOT": "COLLISION",
    "BOT_CRASH_UNIQUE_SKIPPED": "COLLISION",
    "BOT_PUSHED_BOT_SKIPPED": "COLLISION",
    # Fast shot
    "BOT_KICKED_BALL_TOO_FAST": "FAST_SHOT",
    # Fouls
    "KEEPER_HELD_BALL": "FOUL",
    "BOUNDARY_CROSSING": "FOUL",
    "BOT_DRIBBLED_BALL_TOO_FAR": "FOUL",
    "ATTACKER_TOUCHED_BALL_IN_DEFENSE_AREA": "FOUL",
    "ATTACKER_TOUCHED_OPPONENT_IN_DEFENSE_AREA": "FOUL",
    "ATTACKER_TOUCHED_OPPONENT_IN_DEFENSE_AREA_SKIPPED": "FOUL",
    "ATTACKER_DOUBLE_TOUCHED_BALL": "FOUL",
    "ATTACKER_TOO_CLOSE_TO_DEFENSE_AREA": "FOUL",
    "DEFENDER_IN_DEFENSE_AREA": "FOUL",
    "DEFENDER_IN_DEFENSE_AREA_PARTIALLY": "FOUL",
    "BOT_TOO_FAST_IN_STOP": "FOUL",
    "DEFENDER_TOO_CLOSE_TO_KICK_POINT": "FOUL",
    "BOT_INTERFERED_PLACEMENT": "FOUL",
    "BOT_HELD_BALL_DELIBERATELY": "FOUL",
    "BOT_TIPPED_OVER": "FOUL",
    "MULTIPLE_CARDS": "FOUL",
    "MULTIPLE_FOULS": "FOUL",
    "TOO_MANY_ROBOTS": "FOUL",
    "UNSPORTING_BEHAVIOR_MINOR": "FOUL",
    "UNSPORTING_BEHAVIOR_MAJOR": "FOUL",
    "KICK_TIMEOUT": "FOUL",
    # Ball placement lifecycle
    "PLACEMENT_SUCCEEDED": "BALL_PLACEMENT_SUCCEEDED",
    "PLACEMENT_FAILED": "BALL_PLACEMENT_FAILED",
    "MULTIPLE_PLACEMENT_FAILURES": "BALL_PLACEMENT_FAILED",
    # Other referee/game events
    "PENALTY_KICK_FAILED": "PENALTY_KICK_FAILED",
    "NO_PROGRESS_IN_GAME": "NO_PROGRESS",
    "BOT_SUBSTITUTION": "BOT_SUBSTITUTION",
    "CHALLENGE_FLAG": "CHALLENGE_FLAG",
    "EMERGENCY_STOP": "EMERGENCY_STOP",
    "PREPARED": "PREPARED",
}

_TEAM_BY_ENUM = {
    1: "yellow",
    2: "blue",
}

_REFEREE_COMMAND_NAMES = {
    0: "HALT",
    1: "STOP",
    2: "NORMAL_START",
    3: "FORCE_START",
    4: "PREPARE_KICKOFF_YELLOW",
    5: "PREPARE_KICKOFF_BLUE",
    6: "PREPARE_PENALTY_YELLOW",
    7: "PREPARE_PENALTY_BLUE",
    8: "DIRECT_FREE_YELLOW",
    9: "DIRECT_FREE_BLUE",
    10: "INDIRECT_FREE_YELLOW",
    11: "INDIRECT_FREE_BLUE",
    12: "TIMEOUT_YELLOW",
    13: "TIMEOUT_BLUE",
    14: "GOAL_YELLOW",
    15: "GOAL_BLUE",
    16: "BALL_PLACEMENT_YELLOW",
    17: "BALL_PLACEMENT_BLUE",
}

# Referee command → (event_type, team, extra_metadata) mapping.
# Each command comes in a yellow/blue pair; team is encoded per entry.
_TEAM_COMMAND_MAP = {
    4:  ("KICKOFF",        "yellow", {}),
    5:  ("KICKOFF",        "blue",   {}),
    6:  ("PENALTY",        "yellow", {}),
    7:  ("PENALTY",        "blue",   {}),
    8:  ("FREE_KICK",      "yellow", {"indirect": False}),
    9:  ("FREE_KICK",      "blue",   {"indirect": False}),
    10: ("FREE_KICK",      "yellow", {"indirect": True}),
    11: ("FREE_KICK",      "blue",   {"indirect": True}),
    12: ("TIMEOUT",        "yellow", {}),
    13: ("TIMEOUT",        "blue",   {}),
    16: ("BALL_PLACEMENT", "yellow", {}),
    17: ("BALL_PLACEMENT", "blue",   {}),
}

_INPLAY_START_PRE_COMMANDS = {
    0, 1, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 16, 17,
}

# Thresholds for Tracker heuristics
_SHOT_SPEED_THRESHOLD = 6.0       # m/s
_PASS_SPEED_THRESHOLD = 1.0       # m/s
_BALL_CONTACT_DIST = 0.15         # m


class EventDetector:
    """
    Hybrid event detection combining GC GameEvents and Tracker heuristics.

    GC events provide ground-truth for goals, fouls, and ball-out.
    Tracker heuristics detect passes, shots, saves, and possession changes.
    """

    def __init__(self):
        # GC state tracking
        self._seen_gc_event_ids: Set[str] = set()
        self._last_gc_command: Optional[int] = None
        self._last_gc_command_counter: Optional[int] = None
        self._last_gc_stage: Optional[int] = None

        # Tracker heuristics state
        self._prev_ball_pos: Optional[Tuple[float, float]] = None
        self._prev_ball_speed: float = 0.0
        self._prev_possessor: Optional[Dict] = None  # {"id": int, "team": str}
        self._shot_in_progress: bool = False
        self._shot_start_time: float = 0.0
        self._last_ball_pos: Tuple[float, float] = (0.0, 0.0)

    def update_from_referee(self, referee: Any) -> List[DetectedEvent]:
        """Detect events from Referee protobuf message."""
        events: List[DetectedEvent] = []

        if not referee.game_events:
            self._seen_gc_event_ids.clear()

        # Process new game_events
        for index, ge in enumerate(referee.game_events):
            event_id = self._gc_event_id(ge, index)
            if event_id in self._seen_gc_event_ids:
                continue
            self._seen_gc_event_ids.add(event_id)

            detected = self._gc_game_event_to_detected(ge, referee)
            if detected:
                events.append(detected)

        # Detect command changes
        current_command = referee.command
        current_counter = getattr(referee, "command_counter", None)
        if self._last_gc_command is None:
            cmd_event = self._initial_command_to_event(current_command, referee)
            if cmd_event:
                events.append(cmd_event)
        elif (
            current_command != self._last_gc_command
            or (
                current_counter is not None
                and current_counter != self._last_gc_command_counter
            )
        ):
            cmd_event = self._command_change_to_event(
                self._last_gc_command, current_command, referee
            )
            if cmd_event:
                events.append(cmd_event)

        self._last_gc_command = current_command
        self._last_gc_command_counter = current_counter

        # Detect stage changes (half time, game end)
        current_stage = referee.stage
        if (
            self._last_gc_stage is not None
            and current_stage != self._last_gc_stage
        ):
            stage_event = self._stage_change_to_event(
                self._last_gc_stage, current_stage
            )
            if stage_event:
                events.append(stage_event)

        self._last_gc_stage = current_stage

        return events

    def update_from_tracker(self, frame: Any) -> List[DetectedEvent]:
        """Detect events from TrackedFrame protobuf message."""
        events: List[DetectedEvent] = []

        if not frame.balls:
            return events

        ball = frame.balls[0]
        ball_pos = (ball.pos.x, ball.pos.y)
        ball_speed = math.hypot(ball.vel.x, ball.vel.y)

        # Find nearest robot to ball for each team
        nearest_blue = self._find_nearest_robot(frame, ball_pos, "blue")
        nearest_yellow = self._find_nearest_robot(frame, ball_pos, "yellow")

        # Determine current possessor
        current_possessor = self._determine_possessor(
            ball_pos, nearest_blue, nearest_yellow
        )

        # Pass detection (same team, new robot near ball, ball moving)
        if (
            current_possessor is not None
            and self._prev_possessor is not None
            and current_possessor["team"] == self._prev_possessor["team"]
            and current_possessor["id"] != self._prev_possessor["id"]
            and ball_speed > _PASS_SPEED_THRESHOLD
        ):
            events.append(
                DetectedEvent(
                    event_type="PASS",
                    position=ball_pos,
                    ball_speed=ball_speed,
                    confidence=0.6,
                    primary_robot=self._prev_possessor,
                    secondary_robot=current_possessor,
                )
            )

        # Shot detection (fast ball toward goal)
        if (
            ball_speed > _SHOT_SPEED_THRESHOLD
            and not self._shot_in_progress
        ):
            if self._is_shot_direction(ball_pos, ball.vel.x, ball.vel.y):
                shooter = current_possessor or self._prev_possessor
                events.append(
                    DetectedEvent(
                        event_type="SHOT",
                        position=ball_pos,
                        ball_speed=ball_speed,
                        confidence=0.8,
                        primary_robot=shooter,
                        metadata={"speed_mps": round(ball_speed, 2)},
                    )
                )
                self._shot_in_progress = True
                self._shot_start_time = time.time()

        # Reset shot flag when ball slows down
        if ball_speed < 1.0 and self._shot_in_progress:
            self._shot_in_progress = False

        # Save detection (after shot, ball direction changes near goal)
        if (
            self._shot_in_progress
            and self._prev_ball_speed > _SHOT_SPEED_THRESHOLD
            and ball_speed < self._prev_ball_speed * 0.5
            and self._near_goal(ball_pos)
        ):
            shooting_team = self._prev_possessor.get("team") if self._prev_possessor else None
            gk = nearest_yellow if shooting_team == "blue" else nearest_blue
            events.append(
                DetectedEvent(
                    event_type="SAVE",
                    position=ball_pos,
                    ball_speed=ball_speed,
                    confidence=0.75,
                    primary_robot=gk,
                )
            )
            self._shot_in_progress = False

        # Update state
        self._prev_ball_pos = ball_pos
        self._prev_ball_speed = ball_speed
        if current_possessor is not None:
            self._prev_possessor = current_possessor

        self._last_ball_pos = ball_pos

        return events

    # ========== Helpers ==========

    def _gc_event_id(self, ge: Any, index: int) -> str:
        """Generate a unique ID for a GC game event to avoid duplicates."""
        event_type = (
            ge.WhichOneof("event") or self._gc_event_type_name(ge) or "unknown"
        )
        payload = ge.SerializePartialToString(deterministic=True)
        digest = hashlib.sha1(payload).hexdigest()[:16]
        return f"{index}:{event_type}:{digest}"

    def _gc_game_event_to_detected(
        self, ge: Any, referee: Any
    ) -> Optional[DetectedEvent]:
        """Convert a GC GameEvent to a DetectedEvent."""
        event_field = ge.WhichOneof("event")
        event_type_str = (
            event_field.upper() if event_field else self._gc_event_type_name(ge)
        )
        if not event_type_str:
            return None
        detected_type = _GC_EVENT_MAP.get(event_type_str, "GAME_EVENT")

        # Extract position and metadata from event
        event_data = getattr(ge, event_field, None) if event_field else None
        position = (0.0, 0.0)
        primary_robot = None
        secondary_robot = None
        metadata = {
            "gc_event_type": event_type_str,
            "gc_event_has_payload": bool(event_field),
        }
        if detected_type == "GAME_EVENT":
            metadata["log_only"] = True

        if event_data:
            event_metadata, positions = self._extract_gc_event_metadata(event_data)
            metadata.update(event_metadata)
            position = self._select_event_position(positions)
            primary_robot, secondary_robot = self._extract_event_robots(metadata)

        ball_speed = float(metadata.get("initial_ball_speed", 0.0))
        return DetectedEvent(
            event_type=detected_type,
            position=position,
            ball_speed=ball_speed,
            confidence=1.0,  # GC events are ground truth
            primary_robot=primary_robot,
            secondary_robot=secondary_robot,
            metadata=metadata,
        )

    @staticmethod
    def _gc_event_type_name(ge: Any) -> Optional[str]:
        try:
            if not ge.HasField("type"):
                return None
        except (AttributeError, ValueError):
            return None

        enum_type = ge.DESCRIPTOR.fields_by_name["type"].enum_type
        try:
            return enum_type.values_by_number[ge.type].name
        except KeyError:
            return None

    def _extract_gc_event_metadata(
        self, event_data: Any
    ) -> Tuple[Dict[str, Any], Dict[str, Tuple[float, float]]]:
        """Extract scalar fields and positions from a GC event payload."""
        metadata: Dict[str, Any] = {}
        positions: Dict[str, Tuple[float, float]] = {}

        for field in event_data.DESCRIPTOR.fields:
            name = field.name
            value = getattr(event_data, name)

            if getattr(field, "is_repeated", False):
                if field.type == FieldDescriptor.TYPE_MESSAGE:
                    metadata[f"{name}_count"] = len(value)
                else:
                    metadata[name] = list(value)
                continue

            if not self._has_proto_field(event_data, name):
                continue

            if field.type == FieldDescriptor.TYPE_MESSAGE:
                if hasattr(value, "x") and hasattr(value, "y"):
                    point = (float(value.x), float(value.y))
                    positions[name] = point
                    metadata[name] = {"x": point[0], "y": point[1]}
                continue

            if field.type == FieldDescriptor.TYPE_ENUM:
                metadata[name] = self._enum_value_to_metadata(field, value)
                continue

            metadata[name] = value

        return metadata, positions

    @staticmethod
    def _has_proto_field(message: Any, field_name: str) -> bool:
        try:
            return message.HasField(field_name)
        except (AttributeError, ValueError):
            return True

    @staticmethod
    def _enum_value_to_metadata(field: Any, value: int) -> str:
        if field.enum_type.full_name == "robocup_ssl.Team":
            return _TEAM_BY_ENUM.get(value, "unknown")
        try:
            return field.enum_type.values_by_number[value].name
        except KeyError:
            return str(value)

    @staticmethod
    def _select_event_position(
        positions: Dict[str, Tuple[float, float]]
    ) -> Tuple[float, float]:
        for key in ("location", "ball_location", "end", "start", "kick_location"):
            if key in positions:
                return positions[key]
        return (0.0, 0.0)

    def _extract_event_robots(
        self, metadata: Dict[str, Any]
    ) -> Tuple[Optional[Dict], Optional[Dict]]:
        team = self._metadata_team(metadata, "kicking_team")
        if "kicking_bot" in metadata:
            return {"id": metadata["kicking_bot"], "team": team}, None

        team = self._metadata_team(metadata, "by_team")
        if "by_bot" in metadata:
            primary = {"id": metadata["by_bot"], "team": team}
            secondary = None
            if "victim" in metadata:
                secondary = {
                    "id": metadata["victim"],
                    "team": self._opponent_team(team),
                }
            return primary, secondary

        if "violator" in metadata:
            primary = {"id": metadata["violator"], "team": team}
            secondary = None
            if "victim" in metadata:
                secondary = {
                    "id": metadata["victim"],
                    "team": self._opponent_team(team),
                }
            return primary, secondary

        if "bot_yellow" in metadata or "bot_blue" in metadata:
            primary = None
            secondary = None
            if "bot_yellow" in metadata:
                primary = {"id": metadata["bot_yellow"], "team": "yellow"}
            if "bot_blue" in metadata:
                secondary = {"id": metadata["bot_blue"], "team": "blue"}
            return primary, secondary

        return None, None

    @staticmethod
    def _metadata_team(metadata: Dict[str, Any], field_name: str) -> str:
        value = metadata.get(field_name)
        if value in ("blue", "yellow"):
            return value
        fallback = metadata.get("by_team")
        if fallback in ("blue", "yellow"):
            return fallback
        return "unknown"

    @staticmethod
    def _opponent_team(team: str) -> str:
        if team == "blue":
            return "yellow"
        if team == "yellow":
            return "blue"
        return "unknown"

    def _initial_command_to_event(
        self, current_cmd: int, referee: Any
    ) -> Optional[DetectedEvent]:
        """Emit important set-play commands already active at startup."""
        if current_cmd in _TEAM_COMMAND_MAP:
            return self._team_command_to_event(current_cmd, referee)
        return None

    def _command_change_to_event(
        self, old_cmd: int, new_cmd: int, referee: Any
    ) -> Optional[DetectedEvent]:
        """Detect play state changes from Referee command transitions."""
        if new_cmd == 0:
            return DetectedEvent(
                event_type="HALT",
                position=self._last_ball_pos,
                ball_speed=0.0,
                confidence=1.0,
            )
        if new_cmd == 1:
            return DetectedEvent(
                event_type="STOP",
                position=self._last_ball_pos,
                ball_speed=0.0,
                confidence=1.0,
            )
        # NORMAL_START/FORCE_START after any non-inplay state → INPLAY_START
        if new_cmd in (2, 3) and old_cmd in _INPLAY_START_PRE_COMMANDS:
            return DetectedEvent(
                event_type="INPLAY_START",
                position=self._last_ball_pos,
                ball_speed=0.0,
                confidence=1.0,
            )
        # Team-based commands (kickoff, penalty, free kick, timeout, ball placement)
        return self._team_command_to_event(new_cmd, referee)

    def _team_command_to_event(
        self, command: int, referee: Any
    ) -> Optional[DetectedEvent]:
        """Convert a team-scoped referee command into a detected event."""
        entry = _TEAM_COMMAND_MAP.get(command)
        if not entry:
            return None

        event_type, team, extra = entry
        metadata = {
            "team": team,
            "command": _REFEREE_COMMAND_NAMES.get(command, f"COMMAND_{command}"),
            **extra,
        }
        position = self._last_ball_pos

        if event_type == "BALL_PLACEMENT":
            target = self._get_designated_position(referee)
            if target is not None:
                position = target
                metadata["target_position"] = {"x": target[0], "y": target[1]}

        next_command = self._get_next_command_name(referee)
        if next_command:
            metadata["next_command"] = next_command

        return DetectedEvent(
            event_type=event_type,
            position=position,
            ball_speed=0.0,
            confidence=1.0,
            metadata=metadata,
        )

    @staticmethod
    def _get_designated_position(referee: Any) -> Optional[Tuple[float, float]]:
        if not EventDetector._has_proto_field(referee, "designated_position"):
            return None
        # Referee designated_position is in millimeters; use meters internally.
        point = referee.designated_position
        return (point.x / 1000.0, point.y / 1000.0)

    @staticmethod
    def _get_next_command_name(referee: Any) -> Optional[str]:
        if not EventDetector._has_proto_field(referee, "next_command"):
            return None
        value = referee.next_command
        return _REFEREE_COMMAND_NAMES.get(value, f"COMMAND_{value}")

    def _stage_change_to_event(
        self, old_stage: int, new_stage: int
    ) -> Optional[DetectedEvent]:
        """Detect half-time and game-end from Referee stage transitions."""
        # Stage 2 = NORMAL_HALF_TIME
        if new_stage == 2:
            return DetectedEvent(
                event_type="HALF_TIME",
                position=self._last_ball_pos,
                ball_speed=0.0,
                confidence=1.0,
            )
        # Stage 13 = POST_GAME
        if new_stage == 13:
            return DetectedEvent(
                event_type="GAME_END",
                position=self._last_ball_pos,
                ball_speed=0.0,
                confidence=1.0,
            )
        return None

    def _find_nearest_robot(
        self,
        frame: Any,
        ball_pos: Tuple[float, float],
        team: str,
    ) -> Optional[Dict]:
        """Find the robot nearest to the ball for a given team (blue/yellow)."""
        # Team proto enum: 1=YELLOW, 2=BLUE
        team_id = 2 if team == "blue" else 1
        min_dist = float("inf")
        nearest = None

        for robot in frame.robots:
            if robot.robot_id.team != team_id:
                continue
            if robot.visibility < 0.5:
                continue

            dist = math.hypot(robot.pos.x - ball_pos[0], robot.pos.y - ball_pos[1])
            if dist < min_dist:
                min_dist = dist
                nearest = {
                    "id": robot.robot_id.id,
                    "team": team,
                    "dist": dist,
                }

        return nearest

    def _determine_possessor(
        self,
        ball_pos: Tuple[float, float],
        nearest_blue: Optional[Dict],
        nearest_yellow: Optional[Dict],
    ) -> Optional[Dict]:
        """Determine which robot (if any) has possession."""
        blue_dist = nearest_blue["dist"] if nearest_blue else float("inf")
        yellow_dist = nearest_yellow["dist"] if nearest_yellow else float("inf")

        if blue_dist < _BALL_CONTACT_DIST:
            return {"id": nearest_blue["id"], "team": "blue"}
        if yellow_dist < _BALL_CONTACT_DIST:
            return {"id": nearest_yellow["id"], "team": "yellow"}
        return None

    def _is_shot_direction(
        self, ball_pos: Tuple[float, float], vx: float, vy: float
    ) -> bool:
        """Check if ball direction is toward either goal."""
        if abs(vx) < 0.1:
            return False

        # Check if ball is heading toward positive x goal (blue goal side)
        if vx > 0:
            goal_x, goal_y = 6.0, 0.0
        else:
            goal_x, goal_y = -6.0, 0.0

        dx = goal_x - ball_pos[0]
        dy = goal_y - ball_pos[1]
        goal_angle = math.atan2(dy, dx)
        ball_angle = math.atan2(vy, vx)

        angle_diff = abs(goal_angle - ball_angle)
        if angle_diff > math.pi:
            angle_diff = 2 * math.pi - angle_diff

        return angle_diff < math.radians(30)

    def _near_goal(self, ball_pos: Tuple[float, float]) -> bool:
        """Check if ball is near either goal."""
        for goal_x in (6.0, -6.0):
            dist = math.hypot(ball_pos[0] - goal_x, ball_pos[1])
            if dist < 2.0:
                return True
        return False
