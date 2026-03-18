# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""World Model Writer - Maintains game narrative in background."""

import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime


@dataclass
class BallTrajectoryPoint:
    """Single point in ball trajectory history."""

    timestamp: float
    position: Tuple[float, float, float]  # x, y, z
    velocity: Tuple[float, float, float]  # vx, vy, vz


@dataclass
class RobotSnapshot:
    """Snapshot of a robot's state."""

    robot_id: int
    is_ours: bool
    position: Tuple[float, float, float]  # x, y, theta
    velocity: Tuple[float, float]  # linear_speed, angular_speed
    is_available: bool
    has_ball_contact: bool


@dataclass
class GameContext:
    """Current game context for commentary generation."""

    play_situation: int = 0
    our_score: int = 0
    their_score: int = 0
    elapsed_seconds: float = 0.0
    momentum: str = "NEUTRAL"
    last_possession_team: Optional[str] = None
    recent_events: List[str] = field(default_factory=list)


@dataclass
class HighlightEvent:
    """Important event for replay/analysis."""

    event_type: str
    timestamp: datetime
    score: float
    data: Dict[str, Any] = field(default_factory=dict)


# Referee command constants (from ssl_gc_referee_message.proto)
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
    16: "BALL_PLACEMENT_YELLOW",
    17: "BALL_PLACEMENT_BLUE",
}

# SSL Stage constants
_REFEREE_STAGE_NAMES = {
    0: "NORMAL_FIRST_HALF_PRE",
    1: "NORMAL_FIRST_HALF",
    2: "NORMAL_HALF_TIME",
    3: "NORMAL_SECOND_HALF_PRE",
    4: "NORMAL_SECOND_HALF",
    5: "EXTRA_TIME_BREAK",
    6: "EXTRA_FIRST_HALF_PRE",
    7: "EXTRA_FIRST_HALF",
    8: "EXTRA_HALF_TIME",
    9: "EXTRA_SECOND_HALF_PRE",
    10: "EXTRA_SECOND_HALF",
    11: "PENALTY_SHOOTOUT_BREAK",
    12: "PENALTY_SHOOTOUT",
    13: "POST_GAME",
}


class WorldModelWriter:
    """
    Statler Architecture - Writer Component.

    Maintains the game narrative and context in background.
    Updated from SSL Tracker and Game Controller data via UDP multicast.
    """

    def __init__(self):
        self._context = GameContext()
        self._highlights: List[HighlightEvent] = []
        self._lock = threading.Lock()

        self.max_highlights = 100
        self.max_recent_events = 10

        self._ball_trajectory: deque[BallTrajectoryPoint] = deque(maxlen=300)

        self._robot_snapshots_ours: Dict[int, RobotSnapshot] = {}
        self._robot_snapshots_theirs: Dict[int, RobotSnapshot] = {}

        self._play_situation_name: str = "UNKNOWN"
        self._our_goalie_id: int = 0
        self._their_goalie_id: int = 0
        self._ball_possession_team: Optional[str] = None

        self._current_ball_pos: Tuple[float, float, float] = (0.0, 0.0, 0.0)
        self._current_ball_vel: Tuple[float, float, float] = (0.0, 0.0, 0.0)

        # Match start time for elapsed_seconds calculation
        self._match_start_time: Optional[float] = None
        self._stage_time_left_us: int = 0  # microseconds from Referee

    def update(
        self,
        play_situation: int,
        our_score: int,
        their_score: int,
        elapsed_seconds: float,
    ) -> None:
        """Update game context (called periodically, ~1Hz)."""
        with self._lock:
            self._context.play_situation = play_situation
            self._context.our_score = our_score
            self._context.their_score = their_score
            self._context.elapsed_seconds = elapsed_seconds
            self._update_momentum()

    def update_from_tracker(self, frame: Any, our_team_is_blue: bool) -> None:
        """Update robot and ball state from TrackedFrame protobuf message."""
        with self._lock:
            current_time = time.time()

            # Update ball state
            if frame.balls:
                ball = frame.balls[0]
                pos = (ball.pos.x, ball.pos.y, ball.pos.z)
                vel = (ball.vel.x, ball.vel.y, ball.vel.z)
                self._current_ball_pos = pos
                self._current_ball_vel = vel
                self._ball_trajectory.append(
                    BallTrajectoryPoint(
                        timestamp=current_time,
                        position=pos,
                        velocity=vel,
                    )
                )

            # Update robots
            self._robot_snapshots_ours.clear()
            self._robot_snapshots_theirs.clear()

            for robot in frame.robots:
                robot_id = robot.robot_id.id
                # Team: 0=UNKNOWN, 1=YELLOW, 2=BLUE (from ssl_gc_common.proto Team enum)
                team_value = robot.robot_id.team
                # BLUE=2, YELLOW=1
                is_blue = (team_value == 2)
                is_ours = (is_blue == our_team_is_blue)

                snapshot = self._build_robot_snapshot_from_tracked(robot, is_ours)

                if is_ours:
                    self._robot_snapshots_ours[robot_id] = snapshot
                else:
                    self._robot_snapshots_theirs[robot_id] = snapshot

            self._ball_possession_team = self._determine_possession()
            self._update_momentum()

    def update_from_referee(self, referee: Any, our_team_is_blue: bool) -> None:
        """Update game state from Referee protobuf message."""
        with self._lock:
            # Update scores
            if our_team_is_blue:
                self._context.our_score = referee.blue.score
                self._context.their_score = referee.yellow.score
                self._our_goalie_id = referee.blue.goalkeeper
                self._their_goalie_id = referee.yellow.goalkeeper
            else:
                self._context.our_score = referee.yellow.score
                self._context.their_score = referee.blue.score
                self._our_goalie_id = referee.yellow.goalkeeper
                self._their_goalie_id = referee.blue.goalkeeper

            # Update play situation name from command
            command_value = referee.command
            self._play_situation_name = _REFEREE_COMMAND_NAMES.get(
                command_value, f"COMMAND_{command_value}"
            )

            # Update elapsed time from stage_time_left
            self._stage_time_left_us = referee.stage_time_left
            # Stage determines half duration (300s = 5 min per half)
            stage_value = referee.stage
            stage_duration_us = 300 * 1_000_000  # 5 min in microseconds
            elapsed_us = stage_duration_us - self._stage_time_left_us
            self._context.elapsed_seconds = max(0.0, elapsed_us / 1_000_000.0)

            self._update_momentum()

    def add_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """Add a new event to recent history."""
        with self._lock:
            self._context.recent_events.append(event_type)
            if len(self._context.recent_events) > self.max_recent_events:
                self._context.recent_events.pop(0)

            score = self._calculate_highlight_score(event_type, data)
            if score >= 50:
                highlight = HighlightEvent(
                    event_type=event_type,
                    timestamp=datetime.now(),
                    score=score,
                    data=data,
                )
                self._highlights.append(highlight)
                if len(self._highlights) > self.max_highlights:
                    self._highlights.sort(key=lambda h: h.score, reverse=True)
                    self._highlights.pop()

    def get_context(self) -> GameContext:
        """Get current game context."""
        with self._lock:
            return GameContext(
                play_situation=self._context.play_situation,
                our_score=self._context.our_score,
                their_score=self._context.their_score,
                elapsed_seconds=self._context.elapsed_seconds,
                momentum=self._context.momentum,
                last_possession_team=self._context.last_possession_team,
                recent_events=self._context.recent_events.copy(),
            )

    def get_pending_highlights(self) -> List[HighlightEvent]:
        """Get highlights from the last 30 seconds."""
        with self._lock:
            now = datetime.now()
            return [
                h for h in self._highlights
                if (now - h.timestamp).total_seconds() < 30
            ]

    def _update_momentum(self) -> None:
        if not self._context.recent_events:
            self._context.momentum = "NEUTRAL"
            return

        our_actions = sum(
            1 for e in self._context.recent_events[-5:]
            if e in ["SHOT", "FAST_SHOT", "GOAL"]
        )
        their_actions = sum(
            1 for e in self._context.recent_events[-5:]
            if e in ["SAVE", "INTERCEPTION"]
        )

        if our_actions > their_actions + 1:
            self._context.momentum = "OURS"
        elif their_actions > our_actions + 1:
            self._context.momentum = "THEIRS"
        else:
            self._context.momentum = "NEUTRAL"

    def _calculate_highlight_score(
        self, event_type: str, data: Dict[str, Any]
    ) -> float:
        scores = {
            "GOAL": 100,
            "FAST_SHOT": 80,
            "SAVE": 75,
            "SHOT": 60,
            "INTERCEPTION": 55,
            "PASS": 30,
            "POSSESSION_CHANGE": 20,
        }
        base_score = scores.get(event_type, 10)

        if event_type == "GOAL":
            if data.get("score_diff_after", 0) == 0:
                base_score += 10
            elif data.get("score_diff_after", 0) == 1:
                base_score += 5

        if event_type in ["SHOT", "FAST_SHOT"]:
            speed = data.get("ball_speed", 0)
            if speed > 8.0:
                base_score += 15
            elif speed > 6.0:
                base_score += 5

        return min(base_score, 100)

    # ========== Function Calling Data Providers ==========

    def get_game_state_data(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "score": {
                    "ours": self._context.our_score,
                    "theirs": self._context.their_score,
                },
                "elapsed_minutes": round(self._context.elapsed_seconds / 60.0, 1),
                "play_situation": self._play_situation_name,
                "play_situation_detail": self._get_play_situation_detail(),
                "momentum": self._context.momentum,
                "recent_events": self._context.recent_events[-5:],
                "highlights_count": len([h for h in self._highlights if h.score >= 70]),
            }

    def get_ball_trajectory_data(self, seconds: float = 3.0) -> Dict[str, Any]:
        seconds = min(seconds, 10.0)
        with self._lock:
            current_time = time.time()
            cutoff_time = current_time - seconds

            speed = math.hypot(self._current_ball_vel[0], self._current_ball_vel[1])
            current = {
                "position": {
                    "x": round(self._current_ball_pos[0], 2),
                    "y": round(self._current_ball_pos[1], 2),
                    "z": round(self._current_ball_pos[2], 2),
                },
                "velocity": {
                    "x": round(self._current_ball_vel[0], 2),
                    "y": round(self._current_ball_vel[1], 2),
                },
                "speed_mps": round(speed, 2),
                "state": self._determine_ball_state(speed),
            }

            trajectory = []
            for pt in self._ball_trajectory:
                if pt.timestamp >= cutoff_time:
                    trajectory.append(
                        {
                            "time_offset_sec": round(pt.timestamp - current_time, 2),
                            "position": {
                                "x": round(pt.position[0], 2),
                                "y": round(pt.position[1], 2),
                            },
                        }
                    )
            if len(trajectory) > 30:
                step = len(trajectory) // 30
                trajectory = trajectory[::step][:30]

            return {
                "current": current,
                "trajectory": trajectory,
                "summary": self._generate_ball_summary(),
            }

    def get_robot_status_data(self, robot_id: int, is_ours: bool) -> Dict[str, Any]:
        with self._lock:
            snapshots = (
                self._robot_snapshots_ours if is_ours else self._robot_snapshots_theirs
            )
            robot = snapshots.get(robot_id)

            if not robot:
                return {"error": f"Robot {robot_id} not found"}

            goalie_id = self._our_goalie_id if is_ours else self._their_goalie_id

            return {
                "robot_id": robot_id,
                "is_ours": is_ours,
                "position": {
                    "x": round(robot.position[0], 2),
                    "y": round(robot.position[1], 2),
                    "theta": round(robot.position[2], 2),
                },
                "velocity": {
                    "linear_mps": round(robot.velocity[0], 2),
                    "angular_rps": round(robot.velocity[1], 2),
                },
                "ball_contact": {"has_contact": robot.has_ball_contact},
                "is_goalkeeper": robot_id == goalie_id,
                "is_available": robot.is_available,
                "role_hint": self._infer_robot_role(robot_id, is_ours),
            }

    def get_all_robots_summary_data(self, team: str = "all") -> Dict[str, Any]:
        with self._lock:
            result: Dict[str, Any] = {}
            if team in ("ours", "all"):
                result["ours"] = self._build_team_summary(True)
            if team in ("theirs", "all"):
                result["theirs"] = self._build_team_summary(False)
            result["ball_possession"] = self._ball_possession_team
            return result

    def get_formation_analysis_data(self, focus: str = "both") -> Dict[str, Any]:
        with self._lock:
            result: Dict[str, Any] = {}
            if focus in ("offensive", "both"):
                result["ours"] = self._analyze_team_formation(True)
            if focus in ("defensive", "both"):
                result["theirs"] = self._analyze_team_formation(False)
            result["tactical_situation"] = self._analyze_tactical_situation()
            return result

    def get_highlight_details_data(
        self, highlight_type: str = "any", count: int = 1
    ) -> Dict[str, Any]:
        count = min(count, 5)
        with self._lock:
            type_mapping = {
                "goal": ["GOAL"],
                "shot": ["SHOT", "FAST_SHOT"],
                "save": ["SAVE"],
                "any": ["GOAL", "SHOT", "FAST_SHOT", "SAVE"],
            }
            allowed_types = type_mapping.get(highlight_type, type_mapping["any"])
            filtered = [h for h in self._highlights if h.event_type in allowed_types]
            filtered.sort(key=lambda h: h.timestamp, reverse=True)
            selected = filtered[:count]

            now = datetime.now()
            highlights_data = [
                self._build_highlight_detail(h, now) for h in selected
            ]
            return {"highlights": highlights_data, "total_available": len(filtered)}

    # ========== Helper Methods ==========

    def _build_robot_snapshot_from_tracked(
        self, robot: Any, is_ours: bool
    ) -> RobotSnapshot:
        """Build RobotSnapshot from TrackedRobot protobuf message."""
        robot_id = robot.robot_id.id
        pos = (robot.pos.x, robot.pos.y, robot.orientation)
        vel = (
            math.hypot(robot.vel.x, robot.vel.y),
            robot.vel_angular,
        )
        # Estimate ball contact by proximity
        ball_pos = self._current_ball_pos[:2]
        dist = math.hypot(robot.pos.x - ball_pos[0], robot.pos.y - ball_pos[1])
        has_ball_contact = dist < 0.15

        return RobotSnapshot(
            robot_id=robot_id,
            is_ours=is_ours,
            position=pos,
            velocity=vel,
            is_available=robot.visibility > 0.5,
            has_ball_contact=has_ball_contact,
        )

    def _get_play_situation_detail(self) -> str:
        details = {
            "HALT": "試合停止中",
            "STOP": "ボール停止待ち",
            "NORMAL_START": "試合開始",
            "FORCE_START": "強制開始",
            "PREPARE_KICKOFF_YELLOW": "黄チームキックオフ準備",
            "PREPARE_KICKOFF_BLUE": "青チームキックオフ準備",
            "PREPARE_PENALTY_YELLOW": "黄チームPK準備",
            "PREPARE_PENALTY_BLUE": "青チームPK準備",
            "DIRECT_FREE_YELLOW": "黄チームフリーキック",
            "DIRECT_FREE_BLUE": "青チームフリーキック",
            "BALL_PLACEMENT_YELLOW": "黄チームボールプレイスメント",
            "BALL_PLACEMENT_BLUE": "青チームボールプレイスメント",
        }
        return details.get(self._play_situation_name, self._play_situation_name)

    def _determine_ball_state(self, speed: float) -> str:
        if speed < 0.1:
            return "STOPPED"
        elif speed < 3.0:
            return "ROLLING_SLOW"
        elif speed < 6.0:
            return "ROLLING_FAST"
        else:
            return "FAST_MOVING"

    def _generate_ball_summary(self) -> str:
        if not self._ball_trajectory:
            return "ボール情報なし"

        speed = math.hypot(self._current_ball_vel[0], self._current_ball_vel[1])
        x, y = self._current_ball_pos[0], self._current_ball_pos[1]

        if x < -3.0:
            zone = "自陣深く"
        elif x < 0:
            zone = "自陣"
        elif x < 3.0:
            zone = "相手陣"
        else:
            zone = "相手陣深く"

        if y > 2.0:
            side = "左サイド"
        elif y < -2.0:
            side = "右サイド"
        else:
            side = "中央"

        if speed < 0.1:
            return f"ボールは{zone}{side}で静止"
        elif speed < 3.0:
            return f"ボールは{zone}{side}をゆっくり移動中"
        else:
            return f"ボールは{zone}{side}を高速で移動中（{speed:.1f}m/s）"

    def _determine_possession(self) -> Optional[str]:
        for robot in self._robot_snapshots_ours.values():
            if robot.has_ball_contact:
                return "ours"

        ball_pos = self._current_ball_pos[:2]
        min_our_dist = float("inf")
        min_their_dist = float("inf")

        for robot in self._robot_snapshots_ours.values():
            if robot.is_available:
                dist = math.hypot(
                    robot.position[0] - ball_pos[0],
                    robot.position[1] - ball_pos[1],
                )
                min_our_dist = min(min_our_dist, dist)

        for robot in self._robot_snapshots_theirs.values():
            if robot.is_available:
                dist = math.hypot(
                    robot.position[0] - ball_pos[0],
                    robot.position[1] - ball_pos[1],
                )
                min_their_dist = min(min_their_dist, dist)

        if min_our_dist < 0.3:
            return "ours"
        elif min_their_dist < 0.3:
            return "theirs"
        elif min_our_dist < min_their_dist - 0.5:
            return "ours"
        elif min_their_dist < min_our_dist - 0.5:
            return "theirs"
        return None

    def _infer_robot_role(self, robot_id: int, is_ours: bool) -> str:
        snapshots = (
            self._robot_snapshots_ours if is_ours else self._robot_snapshots_theirs
        )
        robot = snapshots.get(robot_id)
        if not robot:
            return "不明"

        goalie_id = self._our_goalie_id if is_ours else self._their_goalie_id
        if robot_id == goalie_id:
            return "ゴールキーパー"

        x = robot.position[0]
        if x < -4.0:
            return "守備（ゴール前）"
        elif x < -2.0:
            return "守備"
        elif x < 2.0:
            return "中盤"
        elif x < 4.0:
            return "攻撃"
        else:
            return "攻撃（ゴール前）"

    def _build_team_summary(self, is_ours: bool) -> Dict[str, Any]:
        snapshots = (
            self._robot_snapshots_ours if is_ours else self._robot_snapshots_theirs
        )
        goalie_id = self._our_goalie_id if is_ours else self._their_goalie_id

        active_robots = [r for r in snapshots.values() if r.is_available]
        robots_info = []

        for robot in active_robots:
            role = self._infer_robot_role(robot.robot_id, is_ours)
            zone = self._get_position_zone(robot.position[0])
            robots_info.append({"id": robot.robot_id, "role": role, "position_zone": zone})

        formation = self._determine_formation(active_robots, goalie_id)

        return {
            "active_count": len(active_robots),
            "goalkeeper_id": goalie_id,
            "robots": robots_info,
            "formation_summary": formation,
        }

    def _get_position_zone(self, x: float) -> str:
        if x < -4.0:
            return "goal_area"
        elif x < -2.0:
            return "defense"
        elif x < 2.0:
            return "midfield"
        elif x < 4.0:
            return "attack"
        else:
            return "opponent_goal_area"

    def _determine_formation(self, robots: List[RobotSnapshot], goalie_id: int) -> str:
        zones = {"defense": 0, "midfield": 0, "attack": 0}
        for robot in robots:
            if robot.robot_id == goalie_id:
                continue
            x = robot.position[0]
            if x < -2.0:
                zones["defense"] += 1
            elif x < 2.0:
                zones["midfield"] += 1
            else:
                zones["attack"] += 1

        d, m, a = zones["defense"], zones["midfield"], zones["attack"]
        if d >= 3:
            return f"{d}-{m}-{a}（守備的布陣）"
        elif a >= 3:
            return f"{d}-{m}-{a}（攻撃的布陣）"
        else:
            return f"{d}-{m}-{a}（バランス型）"

    def _analyze_team_formation(self, is_ours: bool) -> Dict[str, Any]:
        snapshots = (
            self._robot_snapshots_ours if is_ours else self._robot_snapshots_theirs
        )
        goalie_id = self._our_goalie_id if is_ours else self._their_goalie_id
        active_robots = [r for r in snapshots.values() if r.is_available]

        formation = self._determine_formation(active_robots, goalie_id)
        pattern = self._determine_pattern(active_robots)
        pressure_zone = self._determine_pressure_zone(active_robots, is_ours)
        robots_near_ball = self._count_robots_near_ball(active_robots, threshold=1.5)

        goalkeeper = snapshots.get(goalie_id)
        gk_info = {
            "x": round(goalkeeper.position[0], 2) if goalkeeper else 0.0,
            "y": round(goalkeeper.position[1], 2) if goalkeeper else 0.0,
            "advanced": goalkeeper.position[0] > -4.5 if (is_ours and goalkeeper) else False,
        }

        return {
            "formation": formation,
            "pattern": pattern,
            "pressure_zone": pressure_zone,
            "robots_near_ball": robots_near_ball,
            "goalkeeper_position": gk_info,
        }

    def _determine_pattern(self, robots: List[RobotSnapshot]) -> str:
        if len(robots) < 2:
            return "unknown"
        positions = [(r.position[0], r.position[1]) for r in robots]
        total_dist = 0.0
        count = 0
        for i, p1 in enumerate(positions):
            for p2 in positions[i + 1:]:
                dist = math.hypot(p1[0] - p2[0], p1[1] - p2[1])
                total_dist += dist
                count += 1
        avg_dist = total_dist / count if count > 0 else 0
        if avg_dist < 2.0:
            return "compact"
        elif avg_dist > 4.0:
            return "spread"
        else:
            return "balanced"

    def _determine_pressure_zone(
        self, robots: List[RobotSnapshot], is_ours: bool
    ) -> str:
        if not robots:
            return "unknown"
        avg_x = sum(r.position[0] for r in robots) / len(robots)
        if is_ours:
            if avg_x > 2.0:
                return "opponent_half"
            elif avg_x < -2.0:
                return "own_half"
            else:
                return "midfield"
        else:
            if avg_x < -2.0:
                return "opponent_half"
            elif avg_x > 2.0:
                return "own_half"
            else:
                return "midfield"

    def _count_robots_near_ball(
        self, robots: List[RobotSnapshot], threshold: float
    ) -> int:
        ball_pos = self._current_ball_pos[:2]
        count = 0
        for robot in robots:
            dist = math.hypot(
                robot.position[0] - ball_pos[0], robot.position[1] - ball_pos[1]
            )
            if dist < threshold:
                count += 1
        return count

    def _analyze_tactical_situation(self) -> Dict[str, Any]:
        ball_x, ball_y = self._current_ball_pos[0], self._current_ball_pos[1]

        if ball_x < -3.0:
            x_zone = "own_deep"
        elif ball_x < 0:
            x_zone = "own_half"
        elif ball_x < 3.0:
            x_zone = "opponent_half"
        else:
            x_zone = "opponent_deep"

        if ball_y > 1.5:
            y_zone = "left"
        elif ball_y < -1.5:
            y_zone = "right"
        else:
            y_zone = "center"

        ball_zone = f"{x_zone}_{y_zone}"

        our_near = self._count_robots_near_ball(
            list(self._robot_snapshots_ours.values()), 2.0
        )
        their_near = self._count_robots_near_ball(
            list(self._robot_snapshots_theirs.values()), 2.0
        )

        ball_speed = math.hypot(self._current_ball_vel[0], self._current_ball_vel[1])
        if ball_speed > 4.0:
            attack_style = "counter"
        elif self._ball_possession_team == "ours":
            attack_style = "possession"
        else:
            attack_style = "transition"

        return {
            "ball_zone": ball_zone,
            "numerical_advantage": {
                "zone": "ball_vicinity",
                "ours": our_near,
                "theirs": their_near,
            },
            "attack_style": attack_style,
            "defense_style": "zonal",
        }

    def _build_highlight_detail(
        self, highlight: HighlightEvent, now: datetime
    ) -> Dict[str, Any]:
        time_offset = -(now - highlight.timestamp).total_seconds()
        result = {
            "type": highlight.event_type.lower(),
            "timestamp_offset_sec": round(time_offset, 1),
            "importance_score": highlight.score,
        }

        data = highlight.data
        if "primary_robot" in data:
            robot_info = data["primary_robot"]
            result["shooter"] = {
                "robot_id": robot_info.get("id", -1),
                "is_ours": robot_info.get("is_ours", True),
                "position_at_shot": data.get("position", {"x": 0, "y": 0}),
                "distance_to_goal_m": self._calculate_distance_to_goal(
                    data.get("position", {"x": 0, "y": 0}),
                    robot_info.get("is_ours", True),
                ),
            }

        ball_speed = data.get("ball_speed", 0)
        result["shot_details"] = {
            "ball_speed_mps": round(ball_speed, 1),
            "shot_angle_deg": self._estimate_shot_angle(data),
            "target_zone": self._determine_target_zone(data),
            "shot_type": "direct",
        }

        if highlight.event_type == "SAVE" and "secondary_robot" in data:
            gk_info = data["secondary_robot"]
            result["goalkeeper_response"] = {
                "robot_id": gk_info.get("id", 0),
                "reaction_time_sec": 0.2,
                "dive_direction": self._estimate_dive_direction(data),
                "save_attempt": True,
            }
        elif highlight.event_type == "GOAL":
            result["goalkeeper_response"] = {
                "robot_id": self._their_goalie_id,
                "reaction_time_sec": 0.15,
                "dive_direction": "none",
                "save_attempt": False,
            }

        result["context"] = {
            "score_before": data.get("score_before", {"ours": 0, "theirs": 0}),
            "score_after": data.get("score_after", {"ours": 0, "theirs": 0}),
            "game_minute": round(self._context.elapsed_seconds / 60.0, 1),
            "significance": self._determine_goal_significance(data),
        }

        return result

    def _calculate_distance_to_goal(
        self, position: Dict[str, float], is_ours: bool
    ) -> float:
        goal_x = 6.0 if is_ours else -6.0
        return round(
            math.hypot(
                position.get("x", 0) - goal_x, position.get("y", 0)
            ),
            1,
        )

    def _estimate_shot_angle(self, data: Dict[str, Any]) -> float:
        pos = data.get("position", {"x": 0, "y": 0})
        dx = 6.0 - pos.get("x", 0)
        dy = 0 - pos.get("y", 0)
        return round(math.degrees(math.atan2(dy, dx)), 1)

    def _determine_target_zone(self, data: Dict[str, Any]) -> str:
        pos = data.get("position", {"x": 0, "y": 0})
        y = pos.get("y", 0)
        if y > 0.3:
            return "top_left"
        elif y < -0.3:
            return "top_right"
        else:
            return "center"

    def _estimate_dive_direction(self, data: Dict[str, Any]) -> str:
        pos = data.get("position", {"x": 0, "y": 0})
        y = pos.get("y", 0)
        if y > 0.2:
            return "left"
        elif y < -0.2:
            return "right"
        else:
            return "center"

    def _determine_goal_significance(self, data: Dict[str, Any]) -> str:
        before = data.get("score_before", {"ours": 0, "theirs": 0})
        after = data.get("score_after", {"ours": 0, "theirs": 0})
        our_diff = after.get("ours", 0) - before.get("ours", 0)

        if our_diff > 0:
            if before.get("ours", 0) < before.get("theirs", 0):
                if after.get("ours", 0) == after.get("theirs", 0):
                    return "equalizer"
                elif after.get("ours", 0) > after.get("theirs", 0):
                    return "comeback"
            elif before.get("ours", 0) == before.get("theirs", 0):
                return "go_ahead"
            else:
                return "insurance"
        return "regular"
