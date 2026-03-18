# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""Hybrid Event Detector - combines GC GameEvents and Tracker heuristics."""

import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple


@dataclass
class DetectedEvent:
    """A detected game event."""

    event_type: str  # "GOAL", "SHOT", "PASS", etc.
    position: Tuple[float, float]
    ball_speed: float
    confidence: float
    primary_robot: Optional[Dict] = None   # {"id": int, "is_ours": bool}
    secondary_robot: Optional[Dict] = None
    metadata: Dict = field(default_factory=dict)


# GC GameEvent type → DetectedEvent type mapping
_GC_EVENT_MAP = {
    # Goals
    "GOAL": "GOAL",
    "POSSIBLE_GOAL": "SHOT",
    # Ball out
    "BALL_LEFT_FIELD_TOUCH_LINE": "BALL_OUT",
    "BALL_LEFT_FIELD_GOAL_LINE": "BALL_OUT",
    "AIMLESS_KICK": "BALL_OUT",
    # Collisions
    "BOT_CRASH_UNIQUE": "COLLISION",
    "BOT_CRASH_DRAWN": "COLLISION",
    "BOT_PUSHED_BOT": "COLLISION",
    # Fast shot
    "BOT_KICKED_BALL_TOO_FAST": "FAST_SHOT",
    # Fouls
    "KEEPER_HELD_BALL": "FOUL",
    "BOUNDARY_CROSSING": "FOUL",
    "BOT_DRIBBLED_BALL_TOO_FAR": "FOUL",
    "ATTACKER_TOUCHED_BALL_IN_DEFENSE_AREA": "FOUL",
    "ATTACKER_TOO_CLOSE_TO_DEFENSE_AREA": "FOUL",
    "BOT_TOO_FAST_IN_STOP": "FOUL",
    "DEFENDER_TOO_CLOSE_TO_KICK_POINT": "FOUL",
    "BOT_INTERFERED_PLACEMENT": "FOUL",
    "BOT_HELD_BALL_DELIBERATELY": "FOUL",
    "BOT_TIPPED_OVER": "FOUL",
}

# Thresholds for Tracker heuristics
_SHOT_SPEED_THRESHOLD = 6.0       # m/s
_PASS_SPEED_THRESHOLD = 1.0       # m/s
_BALL_CONTACT_DIST = 0.15         # m
_POSSESSION_CHANGE_MIN_DIST = 0.5  # m from ball to new possessor


class EventDetector:
    """
    Hybrid event detection combining GC GameEvents and Tracker heuristics.

    GC events provide ground-truth for goals, fouls, and ball-out.
    Tracker heuristics detect passes, shots, saves, and possession changes.
    """

    def __init__(self, our_team_is_blue: bool):
        self._our_team_is_blue = our_team_is_blue

        # GC state tracking
        self._seen_gc_event_ids: Set[str] = set()
        self._last_gc_command: Optional[int] = None
        self._last_gc_stage: Optional[int] = None

        # Tracker heuristics state
        self._prev_ball_pos: Optional[Tuple[float, float]] = None
        self._prev_ball_speed: float = 0.0
        self._prev_possessor: Optional[Dict] = None  # {"id": int, "is_ours": bool}
        self._shot_in_progress: bool = False
        self._shot_start_time: float = 0.0
        self._last_ball_pos: Tuple[float, float] = (0.0, 0.0)

    def update_from_referee(self, referee: Any) -> List[DetectedEvent]:
        """Detect events from Referee protobuf message."""
        events: List[DetectedEvent] = []

        # Process new game_events
        for ge in referee.game_events:
            event_id = self._gc_event_id(ge)
            if event_id in self._seen_gc_event_ids:
                continue
            self._seen_gc_event_ids.add(event_id)

            detected = self._gc_game_event_to_detected(ge, referee)
            if detected:
                events.append(detected)

        # Detect command changes
        current_command = referee.command
        if (
            self._last_gc_command is not None
            and current_command != self._last_gc_command
        ):
            cmd_event = self._command_change_to_event(
                self._last_gc_command, current_command
            )
            if cmd_event:
                events.append(cmd_event)

        self._last_gc_command = current_command

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
        nearest_ours = self._find_nearest_robot(frame, ball_pos, is_ours=True)
        nearest_theirs = self._find_nearest_robot(frame, ball_pos, is_ours=False)

        # Determine current possessor
        current_possessor = self._determine_possessor(
            ball_pos, nearest_ours, nearest_theirs
        )

        # Possession change detection
        if (
            current_possessor is not None
            and self._prev_possessor is not None
            and current_possessor["is_ours"] != self._prev_possessor["is_ours"]
            and ball_speed > 0.3
        ):
            events.append(
                DetectedEvent(
                    event_type="POSSESSION_CHANGE",
                    position=ball_pos,
                    ball_speed=ball_speed,
                    confidence=0.7,
                    primary_robot=current_possessor,
                    secondary_robot=self._prev_possessor,
                )
            )

        # Pass detection (same team, new robot near ball, ball moving)
        if (
            current_possessor is not None
            and self._prev_possessor is not None
            and current_possessor["is_ours"] == self._prev_possessor["is_ours"]
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
            gk = nearest_theirs if (
                self._prev_possessor and self._prev_possessor.get("is_ours")
            ) else nearest_ours
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

    def _gc_event_id(self, ge: Any) -> str:
        """Generate a unique ID for a GC game event to avoid duplicates."""
        event_type = ge.WhichOneof("event") or "unknown"
        return f"{event_type}_{ge.created_timestamp}"

    def _gc_game_event_to_detected(
        self, ge: Any, referee: Any
    ) -> Optional[DetectedEvent]:
        """Convert a GC GameEvent to a DetectedEvent."""
        event_field = ge.WhichOneof("event")
        if not event_field:
            return None

        event_type_str = event_field.upper()
        detected_type = _GC_EVENT_MAP.get(event_type_str)
        if not detected_type:
            return None

        # Extract position and metadata from event
        event_data = getattr(ge, event_field, None)
        position = (0.0, 0.0)
        primary_robot = None
        metadata = {"gc_event_type": event_type_str}

        if event_data:
            if hasattr(event_data, "location"):
                loc = event_data.location
                position = (loc.x, loc.y)
            if hasattr(event_data, "by_team"):
                by_team_value = event_data.by_team
                # Team: 0=UNKNOWN, 1=YELLOW, 2=BLUE
                is_blue = (by_team_value == 2)
                is_ours = (is_blue == self._our_team_is_blue)
                metadata["by_team_is_ours"] = is_ours
            if hasattr(event_data, "by_bot"):
                robot_id = event_data.by_bot
                is_ours = metadata.get("by_team_is_ours", True)
                primary_robot = {"id": robot_id, "is_ours": is_ours}
            if hasattr(event_data, "initial_ball_speed"):
                metadata["ball_speed"] = event_data.initial_ball_speed

        ball_speed = metadata.get("ball_speed", 0.0)
        return DetectedEvent(
            event_type=detected_type,
            position=position,
            ball_speed=ball_speed,
            confidence=1.0,  # GC events are ground truth
            primary_robot=primary_robot,
            metadata=metadata,
        )

    def _command_change_to_event(
        self, old_cmd: int, new_cmd: int
    ) -> Optional[DetectedEvent]:
        """Detect play state changes from Referee command transitions."""
        # HALT (0)
        if new_cmd == 0:
            return DetectedEvent(
                event_type="HALT",
                position=self._last_ball_pos,
                ball_speed=0.0,
                confidence=1.0,
            )
        # STOP (1)
        if new_cmd == 1:
            return DetectedEvent(
                event_type="STOP",
                position=self._last_ball_pos,
                ball_speed=0.0,
                confidence=1.0,
            )
        # NORMAL_START/FORCE_START after STOP/HALT → INPLAY_START
        if new_cmd in (2, 3) and old_cmd in (0, 1, 4, 5, 6, 7, 8, 9, 10, 11):
            return DetectedEvent(
                event_type="INPLAY_START",
                position=self._last_ball_pos,
                ball_speed=0.0,
                confidence=1.0,
            )
        # Timeout
        if new_cmd in (12, 13):
            return DetectedEvent(
                event_type="TIMEOUT",
                position=self._last_ball_pos,
                ball_speed=0.0,
                confidence=1.0,
            )
        return None

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
        is_ours: bool,
    ) -> Optional[Dict]:
        """Find the robot nearest to the ball for a given team."""
        min_dist = float("inf")
        nearest = None

        for robot in frame.robots:
            # Team: 1=YELLOW, 2=BLUE
            is_blue = (robot.robot_id.team == 2)
            robot_is_ours = (is_blue == self._our_team_is_blue)

            if robot_is_ours != is_ours:
                continue
            if robot.visibility < 0.5:
                continue

            dist = math.hypot(robot.pos.x - ball_pos[0], robot.pos.y - ball_pos[1])
            if dist < min_dist:
                min_dist = dist
                nearest = {
                    "id": robot.robot_id.id,
                    "is_ours": robot_is_ours,
                    "dist": dist,
                }

        return nearest

    def _determine_possessor(
        self,
        ball_pos: Tuple[float, float],
        nearest_ours: Optional[Dict],
        nearest_theirs: Optional[Dict],
    ) -> Optional[Dict]:
        """Determine which robot (if any) has possession."""
        our_dist = nearest_ours["dist"] if nearest_ours else float("inf")
        their_dist = nearest_theirs["dist"] if nearest_theirs else float("inf")

        if our_dist < _BALL_CONTACT_DIST:
            return {"id": nearest_ours["id"], "is_ours": True}
        if their_dist < _BALL_CONTACT_DIST:
            return {"id": nearest_theirs["id"], "is_ours": False}
        if our_dist < _POSSESSION_CHANGE_MIN_DIST and our_dist < their_dist:
            return {"id": nearest_ours["id"], "is_ours": True}
        if their_dist < _POSSESSION_CHANGE_MIN_DIST and their_dist < our_dist:
            return {"id": nearest_theirs["id"], "is_ours": False}
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
