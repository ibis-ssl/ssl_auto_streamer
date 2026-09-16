# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""Synthetic SSL Log Generator - generates deterministic RoboCup SSL match logs."""

import argparse
import gzip
import logging
import math
import struct
from pathlib import Path
from typing import BinaryIO, Callable, Dict, List, Optional, Tuple

from ssl_auto_streamer.ssl import (
    ssl_gc_common_pb2 as common_pb,
    ssl_gc_game_event_pb2 as event_pb,
    ssl_gc_referee_message_pb2 as ref_pb,
    ssl_vision_wrapper_pb2 as wrapper_pb,
    ssl_vision_wrapper_tracked_pb2 as tracker_wrapper_pb,
)
from ssl_auto_streamer.ssl.log_reader import (
    MAGIC_HEADER,
    MSG_TYPE_SSL_REFBOX_2013,
    MSG_TYPE_SSL_VISION_2014,
    MSG_TYPE_SSL_VISION_TRACKER_2020,
)

logger = logging.getLogger(__name__)


class SSLLogWriter:
    """Helper to write packets in RoboCup SSL binary log format."""

    def __init__(self, file_handle: BinaryIO, version: int = 1):
        self.file = file_handle
        self.version = version
        self.packet_count = 0
        self._write_header()

    def _write_header(self) -> None:
        self.file.write(MAGIC_HEADER)
        self.file.write(struct.pack(">i", self.version))

    def write_packet(self, timestamp_ns: int, message_type: int, payload: bytes) -> None:
        header = struct.pack(">qii", timestamp_ns, message_type, len(payload))
        self.file.write(header)
        self.file.write(payload)
        self.packet_count += 1


def create_default_geometry() -> bytes:
    """Create standard SSL Division A geometry packet."""
    wrapper = wrapper_pb.SSL_WrapperPacket()
    geom = wrapper.geometry
    field = geom.field
    field.field_length = 12000
    field.field_width = 9000
    field.goal_width = 1800
    field.goal_depth = 180
    field.boundary_width = 300
    field.penalty_area_depth = 1800
    field.penalty_area_width = 3600
    return wrapper.SerializeToString()


def create_referee_message(
    timestamp_ns: int,
    command: int,
    command_counter: int = 1,
    stage: int = ref_pb.Referee.NORMAL_FIRST_HALF,
    stage_time_left_us: int = 300_000_000,
    blue_name: str = "RoboDragons",
    yellow_name: str = "ibis",
    blue_score: int = 0,
    yellow_score: int = 0,
    designated_position: Optional[Tuple[float, float]] = None,
    game_events: Optional[List[event_pb.GameEvent]] = None,
) -> bytes:
    """Build a Referee protobuf message."""
    ref = ref_pb.Referee()
    ref.packet_timestamp = timestamp_ns // 1000
    ref.stage = stage
    ref.stage_time_left = stage_time_left_us
    ref.command = command
    ref.command_counter = command_counter
    ref.command_timestamp = timestamp_ns // 1000

    ref.yellow.name = yellow_name
    ref.yellow.score = yellow_score
    ref.yellow.red_cards = 0
    ref.yellow.yellow_cards = 0
    ref.yellow.timeouts = 4
    ref.yellow.timeout_time = 300_000_000
    ref.yellow.goalkeeper = 0

    ref.blue.name = blue_name
    ref.blue.score = blue_score
    ref.blue.red_cards = 0
    ref.blue.yellow_cards = 0
    ref.blue.timeouts = 4
    ref.blue.timeout_time = 300_000_000
    ref.blue.goalkeeper = 0

    if designated_position is not None:
        ref.designated_position.x = designated_position[0]
        ref.designated_position.y = designated_position[1]

    if game_events:
        for ge in game_events:
            ref.game_events.append(ge)

    return ref.SerializeToString()


def create_tracker_frame(
    frame_number: int,
    timestamp_sec: float,
    ball_pos: Tuple[float, float],
    ball_vel: Tuple[float, float] = (0.0, 0.0),
    blue_robots: Optional[List[Tuple[int, float, float, float]]] = None,  # (id, x, y, theta)
    yellow_robots: Optional[List[Tuple[int, float, float, float]]] = None,
) -> bytes:
    """Build a TrackerWrapperPacket protobuf message."""
    wrapper = tracker_wrapper_pb.TrackerWrapperPacket()
    wrapper.uuid = "synthetic-generator"
    wrapper.source_name = "ssl-log-generator"

    frame = wrapper.tracked_frame
    frame.frame_number = frame_number
    frame.timestamp = timestamp_sec

    # Ball
    ball = frame.balls.add()
    ball.pos.x = ball_pos[0]
    ball.pos.y = ball_pos[1]
    ball.pos.z = 0.02
    ball.vel.x = ball_vel[0]
    ball.vel.y = ball_vel[1]
    ball.vel.z = 0.0
    ball.visibility = 1.0

    # Blue robots
    if blue_robots:
        for rid, x, y, theta in blue_robots:
            robot = frame.robots.add()
            robot.robot_id.id = rid
            robot.robot_id.team = common_pb.BLUE
            robot.pos.x = x
            robot.pos.y = y
            robot.orientation = theta
            robot.visibility = 1.0

    # Yellow robots
    if yellow_robots:
        for rid, x, y, theta in yellow_robots:
            robot = frame.robots.add()
            robot.robot_id.id = rid
            robot.robot_id.team = common_pb.YELLOW
            robot.pos.x = x
            robot.pos.y = y
            robot.orientation = theta
            robot.visibility = 1.0

    return wrapper.SerializeToString()


# ==============================================================================
# Scenario Generators
# ==============================================================================


def generate_scenario_1_goal(writer: SSLLogWriter, fps: int = 30) -> None:
    """
    Scenario 1: Shoot & Goal (~10 sec).
    Timeline:
      0.0s - 1.0s: Prepare Kickoff Yellow (HALT/STOP)
      1.0s - 3.0s: Kickoff Yellow -> In-play
      3.0s - 4.5s: Yellow #2 dribbles forward
      4.5s - 5.5s: Yellow #2 shoots toward blue goal (6.0, 0.0) with speed 6.5 m/s (SHOT)
      5.5s - 7.0s: Ball enters goal, Possible Goal event emitted (POSSIBLE_GOAL, HALT)
      7.0s - 10.0s: Referee confirms Goal (GOAL, score updated)
    """
    dt = 1.0 / fps
    total_frames = int(10.0 * fps)
    geom_bytes = create_default_geometry()
    writer.write_packet(0, MSG_TYPE_SSL_VISION_2014, geom_bytes)

    cmd_counter = 1
    for i in range(total_frames):
        t = i * dt
        ts_ns = int(t * 1e9)

        # Baseline robot setups
        # Blue keeper at (5.8, 0.0), defenders around (4.5, y)
        blue_bots = [
            (0, 5.8, 0.0, math.pi),
            (1, 4.5, 1.0, math.pi),
            (2, 4.5, -1.0, math.pi),
            (3, 2.0, 0.5, math.pi),
        ]
        # Yellow kicker #2, others support
        yellow_bots = [
            (0, -5.8, 0.0, 0.0),
            (1, -2.0, -1.0, 0.0),
            (3, -1.0, 2.0, 0.0),
        ]

        # Phases
        if t < 1.0:
            ball_pos = (0.0, 0.0)
            ball_vel = (0.0, 0.0)
            yellow_bots.append((2, -0.2, 0.0, 0.0))
            if i % fps == 0:
                writer.write_packet(
                    ts_ns,
                    MSG_TYPE_SSL_REFBOX_2013,
                    create_referee_message(ts_ns, ref_pb.Referee.PREPARE_KICKOFF_YELLOW, cmd_counter),
                )
        elif t < 3.0:
            ball_pos = (0.0, 0.0)
            ball_vel = (0.0, 0.0)
            yellow_bots.append((2, -0.1, 0.0, 0.0))
            if i == int(1.0 * fps):
                cmd_counter += 1
            if i % fps == 0:
                writer.write_packet(
                    ts_ns,
                    MSG_TYPE_SSL_REFBOX_2013,
                    create_referee_message(ts_ns, ref_pb.Referee.NORMAL_START, cmd_counter),
                )
        elif t < 4.5:
            # Yellow #2 approaches and moves forward with ball
            progress = (t - 3.0) / 1.5
            bx = progress * 1.5
            yellow_bots.append((2, bx - 0.15, 0.0, 0.0))
            ball_pos = (bx, 0.0)
            ball_vel = (1.0, 0.0)
        elif t < 5.5:
            # Shot! Ball flies at 6.5 m/s toward (6.0, 0.0)
            shot_t = t - 4.5
            bx = 1.5 + shot_t * 4.5
            ball_pos = (bx, 0.0)
            ball_vel = (6.5, 0.0)
            yellow_bots.append((2, 1.5, 0.0, 0.0))
        elif t < 7.0:
            # Ball inside goal
            ball_pos = (6.05, 0.0)
            ball_vel = (0.2, 0.0)
            yellow_bots.append((2, 1.8, 0.0, 0.0))
            if i == int(5.5 * fps):
                cmd_counter += 1
                ge = event_pb.GameEvent()
                ge.type = event_pb.GameEvent.POSSIBLE_GOAL
                ge.possible_goal.by_team = common_pb.YELLOW
                ge.possible_goal.kicking_team = common_pb.YELLOW
                ge.possible_goal.kicking_bot = 2
                ge.possible_goal.location.x = 6.05
                ge.possible_goal.location.y = 0.0
                writer.write_packet(
                    ts_ns,
                    MSG_TYPE_SSL_REFBOX_2013,
                    create_referee_message(
                        ts_ns, ref_pb.Referee.HALT, cmd_counter, game_events=[ge]
                    ),
                )
        else:
            # Goal confirmed
            ball_pos = (6.05, 0.0)
            ball_vel = (0.0, 0.0)
            yellow_bots.append((2, 2.0, 0.0, 0.0))
            if i == int(7.0 * fps):
                cmd_counter += 1
                ge = event_pb.GameEvent()
                ge.type = event_pb.GameEvent.GOAL
                ge.goal.by_team = common_pb.YELLOW
                ge.goal.kicking_team = common_pb.YELLOW
                ge.goal.kicking_bot = 2
                ge.goal.location.x = 6.05
                ge.goal.location.y = 0.0
                writer.write_packet(
                    ts_ns,
                    MSG_TYPE_SSL_REFBOX_2013,
                    create_referee_message(
                        ts_ns, ref_pb.Referee.HALT, cmd_counter, yellow_score=1, game_events=[ge]
                    ),
                )

        tracker_bytes = create_tracker_frame(
            frame_number=i,
            timestamp_sec=t,
            ball_pos=ball_pos,
            ball_vel=ball_vel,
            blue_robots=blue_bots,
            yellow_robots=yellow_bots,
        )
        writer.write_packet(ts_ns, MSG_TYPE_SSL_VISION_TRACKER_2020, tracker_bytes)


def generate_scenario_2_foul_card(writer: SSLLogWriter, fps: int = 30) -> None:
    """
    Scenario 2: Robot Collision & Yellow Card Foul (~8 sec).
    Timeline:
      0.0s - 2.0s: In-play, bots moving
      2.0s: Blue #3 crashes aggressively into Yellow #1 (COLLISION / BOT_CRASH_UNIQUE)
      2.5s: Referee calls STOP
      4.0s: Blue #3 awarded Yellow Card
      5.5s - 8.0s: Yellow awarded DIRECT_FREE_YELLOW
    """
    dt = 1.0 / fps
    total_frames = int(8.0 * fps)
    geom_bytes = create_default_geometry()
    writer.write_packet(0, MSG_TYPE_SSL_VISION_2014, geom_bytes)

    cmd_counter = 1
    for i in range(total_frames):
        t = i * dt
        ts_ns = int(t * 1e9)

        # Positions
        yellow_1_pos = (-1.0, 1.0)
        # Blue #3 charges towards Yellow #1
        if t < 2.0:
            b3_x = -1.0 + (2.0 - t) * 1.5
            b3_y = 1.0
        else:
            b3_x = -1.05
            b3_y = 1.0

        blue_bots = [
            (0, 5.8, 0.0, math.pi),
            (3, b3_x, b3_y, math.pi),
        ]
        yellow_bots = [
            (0, -5.8, 0.0, 0.0),
            (1, yellow_1_pos[0], yellow_1_pos[1], 0.0),
        ]
        ball_pos = (-0.9, 1.0)
        ball_vel = (0.1, 0.0)

        # Referee events
        if i == 0:
            writer.write_packet(
                ts_ns,
                MSG_TYPE_SSL_REFBOX_2013,
                create_referee_message(ts_ns, ref_pb.Referee.FORCE_START, cmd_counter),
            )
        elif i == int(2.0 * fps):
            # Collision event
            ge = event_pb.GameEvent()
            ge.type = event_pb.GameEvent.BOT_CRASH_UNIQUE
            crash = ge.bot_crash_unique
            crash.by_team = common_pb.BLUE
            crash.violator = 3
            crash.victim = 1
            crash.location.x = -1.0
            crash.location.y = 1.0
            crash.crash_speed = 3.2
            writer.write_packet(
                ts_ns,
                MSG_TYPE_SSL_REFBOX_2013,
                create_referee_message(
                    ts_ns, ref_pb.Referee.STOP, cmd_counter + 1, game_events=[ge]
                ),
            )
            cmd_counter += 1
        elif i == int(5.5 * fps):
            # Direct free kick yellow
            cmd_counter += 1
            writer.write_packet(
                ts_ns,
                MSG_TYPE_SSL_REFBOX_2013,
                create_referee_message(
                    ts_ns, ref_pb.Referee.DIRECT_FREE_YELLOW, cmd_counter
                ),
            )

        tracker_bytes = create_tracker_frame(
            frame_number=i,
            timestamp_sec=t,
            ball_pos=ball_pos,
            ball_vel=ball_vel,
            blue_robots=blue_bots,
            yellow_robots=yellow_bots,
        )
        writer.write_packet(ts_ns, MSG_TYPE_SSL_VISION_TRACKER_2020, tracker_bytes)


def generate_scenario_3_pass_chain(writer: SSLLogWriter, fps: int = 30) -> None:
    """
    Scenario 3: Pass Chain & Interception (~8 sec).
    Timeline:
      0.0s - 1.0s: Blue #1 possesses ball at (-2.0, -1.0)
      1.0s - 2.5s: Blue #1 passes to Blue #2 at (0.0, -1.0) (speed 2.0 m/s) -> PASS
      2.5s - 3.5s: Blue #2 controls ball
      3.5s - 5.0s: Blue #2 passes to Blue #3 at (2.0, 1.0) (speed 2.8 m/s) -> PASS
      5.0s - 8.0s: Yellow #1 takes ball and dribbles away
    """
    dt = 1.0 / fps
    total_frames = int(8.0 * fps)
    geom_bytes = create_default_geometry()
    writer.write_packet(0, MSG_TYPE_SSL_VISION_2014, geom_bytes)

    # In play
    writer.write_packet(
        0,
        MSG_TYPE_SSL_REFBOX_2013,
        create_referee_message(0, ref_pb.Referee.FORCE_START, 1),
    )

    p1 = (-2.0, -1.0)
    p2 = (0.0, -1.0)
    p3 = (2.0, 1.0)

    for i in range(total_frames):
        t = i * dt
        ts_ns = int(t * 1e9)

        # Blue robot positions
        blue_bots = [
            (1, p1[0], p1[1], 0.0),
            (2, p2[0], p2[1], 0.0),
            (3, p3[0], p3[1], 0.0),
        ]

        if t < 1.0:
            # Blue 1 holds ball within contact distance (< 0.15m)
            ball_pos = (p1[0] + 0.05, p1[1])
            ball_vel = (0.0, 0.0)
            yellow_bots = [(1, -1.0, 2.0, 0.0)]
        elif t < 2.0:
            # Ball moving from Blue 1 towards Blue 2 at 2.0 m/s
            alpha = (t - 1.0) / 1.0
            bx = p1[0] + 0.05 + alpha * (p2[0] - p1[0] - 0.05)
            ball_pos = (bx, p1[1])
            ball_vel = (2.0, 0.0)
            yellow_bots = [(1, -1.0, 2.0, 0.0)]
        elif t < 2.1:
            # Ball arrives at Blue 2 with speed 2.0 m/s -> Triggers PASS!
            ball_pos = (p2[0] + 0.05, p2[1])
            ball_vel = (2.0, 0.0)
            yellow_bots = [(1, -1.0, 2.0, 0.0)]
        elif t < 3.5:
            # Blue 2 controls ball, stopped
            ball_pos = (p2[0] + 0.05, p2[1])
            ball_vel = (0.0, 0.0)
            yellow_bots = [(1, -1.0, 2.0, 0.0)]
        elif t < 4.5:
            # Ball moving from Blue 2 towards Blue 3 at 2.8 m/s
            alpha = (t - 3.5) / 1.0
            bx = p2[0] + 0.05 + alpha * (p3[0] - p2[0] - 0.05)
            by = p2[1] + alpha * (p3[1] - p2[1])
            ball_pos = (bx, by)
            ball_vel = (2.0, 2.0)
            yellow_bots = [(1, 0.5, 3.0, 0.0)]
        elif t < 4.6:
            # Ball arrives at Blue 3 with speed 2.8 m/s -> Triggers PASS!
            ball_pos = (p3[0] + 0.05, p3[1])
            ball_vel = (2.0, 2.0)
            yellow_bots = [(1, 1.0, 2.5, 0.0)]
        elif t < 6.0:
            # Blue 3 controls ball
            ball_pos = (p3[0] + 0.05, p3[1])
            ball_vel = (0.0, 0.0)
            yellow_bots = [(1, 2.05, 1.05, -math.pi / 2)]
        else:
            # Yellow 1 takes ball away
            dx = (t - 6.0) * 0.8
            ball_pos = (2.0 - dx, 1.0)
            ball_vel = (-0.8, 0.0)
            yellow_bots = [(1, 2.0 - dx - 0.05, 1.0, math.pi)]

        tracker_bytes = create_tracker_frame(
            frame_number=i,
            timestamp_sec=t,
            ball_pos=ball_pos,
            ball_vel=ball_vel,
            blue_robots=blue_bots,
            yellow_robots=yellow_bots,
        )
        writer.write_packet(ts_ns, MSG_TYPE_SSL_VISION_TRACKER_2020, tracker_bytes)


def generate_scenario_4_penalty_save(writer: SSLLogWriter, fps: int = 30) -> None:
    """
    Scenario 4: Penalty Kick & GK Save (~8 sec).
    Timeline:
      0.0s - 2.0s: Prepare Penalty Yellow (PREPARE_PENALTY_YELLOW)
      2.0s - 3.5s: Normal Start
      3.5s - 4.5s: Yellow #1 shoots from penalty mark (4.0, 0.0) at 6.5 m/s toward goal (SHOT)
      4.5s - 5.5s: Blue GK #0 blocks ball at (5.75, 0.0), speed drops to 1.5 m/s (SAVE)
      5.5s - 8.0s: Ball stopped
    """
    dt = 1.0 / fps
    total_frames = int(8.0 * fps)
    geom_bytes = create_default_geometry()
    writer.write_packet(0, MSG_TYPE_SSL_VISION_2014, geom_bytes)

    cmd_counter = 1
    for i in range(total_frames):
        t = i * dt
        ts_ns = int(t * 1e9)

        # Yellow kicker close to ball (< 0.15m), Blue GK on goal line
        blue_bots = [(0, 5.8, 0.0, math.pi)]
        yellow_bots = [(1, 3.95, 0.0, 0.0)]

        if t < 2.0:
            ball_pos = (4.0, 0.0)
            ball_vel = (0.0, 0.0)
            if i % fps == 0:
                writer.write_packet(
                    ts_ns,
                    MSG_TYPE_SSL_REFBOX_2013,
                    create_referee_message(
                        ts_ns, ref_pb.Referee.PREPARE_PENALTY_YELLOW, cmd_counter
                    ),
                )
        elif t < 3.5:
            ball_pos = (4.0, 0.0)
            ball_vel = (0.0, 0.0)
            if i == int(2.0 * fps):
                cmd_counter += 1
            if i % fps == 0:
                writer.write_packet(
                    ts_ns,
                    MSG_TYPE_SSL_REFBOX_2013,
                    create_referee_message(
                        ts_ns, ref_pb.Referee.NORMAL_START, cmd_counter
                    ),
                )
        elif t < 4.5:
            # Shot toward blue goal: 6.5 m/s > 6.0 m/s threshold -> Triggers SHOT!
            st = t - 3.5
            bx = 4.0 + st * 1.7
            ball_pos = (bx, 0.0)
            ball_vel = (6.5, 0.0)
        elif t < 5.5:
            # Blocked by GK at (5.75, 0.0), speed drops to 1.5 m/s (< 6.5 * 0.5, >= 1.0) -> Triggers SAVE!
            ball_pos = (5.75, 0.0)
            ball_vel = (-1.5, 0.0)
        else:
            # Ball comes to rest
            ball_pos = (5.5, 0.0)
            ball_vel = (0.0, 0.0)

        tracker_bytes = create_tracker_frame(
            frame_number=i,
            timestamp_sec=t,
            ball_pos=ball_pos,
            ball_vel=ball_vel,
            blue_robots=blue_bots,
            yellow_robots=yellow_bots,
        )
        writer.write_packet(ts_ns, MSG_TYPE_SSL_VISION_TRACKER_2020, tracker_bytes)


def generate_scenario_5_ball_out(writer: SSLLogWriter, fps: int = 30) -> None:
    """
    Scenario 5: Ball Out of Bounds & Ball Placement (~10 sec).
    Timeline:
      0.0s - 2.0s: In play, ball crosses touch line (y > 4.5) -> BALL_OUT
      2.0s - 4.0s: Referee stops game, assigns BALL_PLACEMENT_YELLOW at designated position (1.0, 2.0)
      4.0s - 8.0s: Yellow #1 approaches ball and navigates it to (1.0, 2.0)
      8.0s - 10.0s: PLACEMENT_SUCCEEDED event emitted
    """
    dt = 1.0 / fps
    total_frames = int(10.0 * fps)
    geom_bytes = create_default_geometry()
    writer.write_packet(0, MSG_TYPE_SSL_VISION_2014, geom_bytes)

    cmd_counter = 1
    des_pos = (1.0, 2.0)

    for i in range(total_frames):
        t = i * dt
        ts_ns = int(t * 1e9)

        blue_bots = [(0, 5.8, 0.0, math.pi), (1, 3.0, 1.0, math.pi)]

        if t < 2.0:
            # Ball moving out of touch line
            by = 4.0 + (t / 2.0) * 0.7  # crosses 4.5 at ~1.43s
            ball_pos = (0.5, by)
            ball_vel = (0.0, 0.7)
            yellow_bots = [(0, -5.8, 0.0, 0.0), (1, 0.5, by - 0.4, math.pi / 2)]
            if i == 0:
                writer.write_packet(
                    ts_ns,
                    MSG_TYPE_SSL_REFBOX_2013,
                    create_referee_message(ts_ns, ref_pb.Referee.FORCE_START, cmd_counter),
                )
        elif t < 4.0:
            ball_pos = (0.5, 4.7)
            ball_vel = (0.0, 0.0)
            yellow_bots = [(0, -5.8, 0.0, 0.0), (1, 0.5, 4.3, math.pi / 2)]
            if i == int(2.0 * fps):
                # Ball out event
                cmd_counter += 1
                ge = event_pb.GameEvent()
                ge.type = event_pb.GameEvent.BALL_LEFT_FIELD_TOUCH_LINE
                ge.ball_left_field_touch_line.by_team = common_pb.BLUE
                ge.ball_left_field_touch_line.location.x = 0.5
                ge.ball_left_field_touch_line.location.y = 4.52
                writer.write_packet(
                    ts_ns,
                    MSG_TYPE_SSL_REFBOX_2013,
                    create_referee_message(
                        ts_ns,
                        ref_pb.Referee.STOP,
                        cmd_counter,
                        game_events=[ge],
                    ),
                )
            elif i == int(3.0 * fps):
                # Placement command
                cmd_counter += 1
                writer.write_packet(
                    ts_ns,
                    MSG_TYPE_SSL_REFBOX_2013,
                    create_referee_message(
                        ts_ns,
                        ref_pb.Referee.BALL_PLACEMENT_YELLOW,
                        cmd_counter,
                        designated_position=des_pos,
                    ),
                )
        elif t < 8.0:
            # Yellow #1 pushes ball from (0.5, 4.7) toward (1.0, 2.0)
            alpha = (t - 4.0) / 4.0
            bx = 0.5 + alpha * (des_pos[0] - 0.5)
            by = 4.7 + alpha * (des_pos[1] - 4.7)
            ball_pos = (bx, by)
            ball_vel = (0.12, -0.67)
            yellow_bots = [(0, -5.8, 0.0, 0.0), (1, bx, by + 0.15, -math.pi / 2)]
        else:
            ball_pos = des_pos
            ball_vel = (0.0, 0.0)
            yellow_bots = [(0, -5.8, 0.0, 0.0), (1, des_pos[0], des_pos[1] + 0.5, 0.0)]
            if i == int(8.0 * fps):
                cmd_counter += 1
                ge = event_pb.GameEvent()
                ge.type = event_pb.GameEvent.PLACEMENT_SUCCEEDED
                ge.placement_succeeded.by_team = common_pb.YELLOW
                ge.placement_succeeded.time_taken = 4.0
                ge.placement_succeeded.precision = 0.02
                ge.placement_succeeded.distance = 2.7
                writer.write_packet(
                    ts_ns,
                    MSG_TYPE_SSL_REFBOX_2013,
                    create_referee_message(
                        ts_ns,
                        ref_pb.Referee.STOP,
                        cmd_counter,
                        game_events=[ge],
                    ),
                )

        tracker_bytes = create_tracker_frame(
            frame_number=i,
            timestamp_sec=t,
            ball_pos=ball_pos,
            ball_vel=ball_vel,
            blue_robots=blue_bots,
            yellow_robots=yellow_bots,
        )
        writer.write_packet(ts_ns, MSG_TYPE_SSL_VISION_TRACKER_2020, tracker_bytes)


def generate_scenario_6_minimal_smoke(writer: SSLLogWriter) -> None:
    """
    Scenario 6: Ultra-lightweight Smoke Test log (~1.0 sec, 20 packets).
    Used for rapid CI and file parser validation.
    """
    geom_bytes = create_default_geometry()
    writer.write_packet(0, MSG_TYPE_SSL_VISION_2014, geom_bytes)

    # Referee message
    writer.write_packet(
        0,
        MSG_TYPE_SSL_REFBOX_2013,
        create_referee_message(0, ref_pb.Referee.HALT, command_counter=1),
    )

    # 15 frames
    for i in range(15):
        t = i * 0.066
        ts_ns = int(t * 1e9)
        tracker_bytes = create_tracker_frame(
            frame_number=i,
            timestamp_sec=t,
            ball_pos=(0.0, 0.0),
            ball_vel=(0.0, 0.0),
            blue_robots=[(0, 5.0, 0.0, 0.0), (1, 2.0, 1.0, 0.0)],
            yellow_robots=[(0, -5.0, 0.0, 0.0), (1, -2.0, -1.0, 0.0)],
        )
        writer.write_packet(ts_ns, MSG_TYPE_SSL_VISION_TRACKER_2020, tracker_bytes)


SCENARIOS: Dict[str, Tuple[Callable[[SSLLogWriter], None], str]] = {
    "scenario_1_goal": (generate_scenario_1_goal, "Shot & Goal sequence"),
    "scenario_2_foul_card": (generate_scenario_2_foul_card, "Robot collision & card foul"),
    "scenario_3_pass_chain": (generate_scenario_3_pass_chain, "Pass chain & interception"),
    "scenario_4_penalty_save": (generate_scenario_4_penalty_save, "Penalty kick & goalkeeper save"),
    "scenario_5_ball_out": (generate_scenario_5_ball_out, "Ball out of bounds & ball placement"),
    "scenario_6_minimal_smoke": (generate_scenario_6_minimal_smoke, "Minimal smoke test log"),
}


def generate_log_file(scenario_name: str, output_path: Path) -> int:
    """Generate a single scenario log file."""
    if scenario_name not in SCENARIOS:
        raise ValueError(f"Unknown scenario: {scenario_name}. Available: {list(SCENARIOS.keys())}")

    gen_func, desc = SCENARIOS[scenario_name]
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with gzip.open(output_path, "wb") as gz:
        writer = SSLLogWriter(gz)
        gen_func(writer)

    size_kb = output_path.stat().st_size / 1024
    logger.info(f"Generated {scenario_name} -> {output_path} ({writer.packet_count} pkts, {size_kb:.1f} KB)")
    return writer.packet_count


def generate_all(output_dir: Path) -> Dict[str, Path]:
    """Generate all available scenarios into the specified directory."""
    output_dir.mkdir(parents=True, exist_ok=True)
    results = {}
    for name in SCENARIOS:
        out_file = output_dir / f"{name}.log.gz"
        generate_log_file(name, out_file)
        results[name] = out_file
    return results


def main() -> None:
    """CLI entry point for ssl-log-generator."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Generate synthetic RoboCup SSL match logs for testing."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("tests/data/scenarios"),
        help="Target directory for generated log files (default: tests/data/scenarios)",
    )
    parser.add_argument(
        "--scenario",
        type=str,
        choices=list(SCENARIOS.keys()),
        help="Generate only the specified scenario",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        default=True,
        help="Generate all scenarios (default)",
    )

    args = parser.parse_args()

    if args.scenario:
        out_file = args.output_dir / f"{args.scenario}.log.gz"
        count = generate_log_file(args.scenario, out_file)
        print(f"Successfully generated {args.scenario}: {out_file} ({count} packets)")
    else:
        results = generate_all(args.output_dir)
        print(f"Successfully generated {len(results)} scenario logs in {args.output_dir}")


if __name__ == "__main__":
    main()
