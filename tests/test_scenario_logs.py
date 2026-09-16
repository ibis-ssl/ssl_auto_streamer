# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""Automated tests verifying synthetic scenario logs and fixture data."""

import collections
import json
from pathlib import Path

import pytest

from ssl_auto_streamer.event_detector import EventDetector
from ssl_auto_streamer.ssl import (
    ssl_gc_common_pb2 as common_pb,
    ssl_gc_game_event_pb2 as event_pb,
    ssl_vision_detection_tracked_pb2 as tracked_pb,
)
from ssl_auto_streamer.ssl.log_reader import (
    MSG_TYPE_SSL_REFBOX_2013,
    MSG_TYPE_SSL_VISION_2014,
    MSG_TYPE_SSL_VISION_TRACKER_2020,
    SSLLogReader,
)
from ssl_auto_streamer.statler import WorldModelWriter
from ssl_auto_streamer.tools.log_generator import SCENARIOS, generate_all

SCENARIOS_DIR = Path(__file__).parent / "data" / "scenarios"
FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session", autouse=True)
def ensure_scenario_logs_exist() -> None:
    """Ensure all scenario log files are generated before running tests."""
    missing = [name for name in SCENARIOS if not (SCENARIOS_DIR / f"{name}.log.gz").exists()]
    if missing:
        generate_all(SCENARIOS_DIR)


def _replay_scenario(log_path: Path):
    """Helper to parse a scenario log and feed it to WorldModelWriter and EventDetector."""
    writer = WorldModelWriter()
    detector = EventDetector()
    events = []

    with SSLLogReader(log_path) as reader:
        for pkt in reader.iter_packets():
            if pkt.message_type == MSG_TYPE_SSL_VISION_TRACKER_2020:
                tr = pkt.decode()
                if tr and tr.HasField("tracked_frame"):
                    writer.update_from_tracker(tr.tracked_frame)
                    evs = detector.update_from_tracker(tr.tracked_frame)
                    events.extend(evs)
            elif pkt.message_type == MSG_TYPE_SSL_REFBOX_2013:
                ref = pkt.decode()
                if ref:
                    writer.update_from_referee(ref)
                    evs = detector.update_from_referee(ref)
                    events.extend(evs)
            elif pkt.message_type == MSG_TYPE_SSL_VISION_2014:
                vis = pkt.decode()
                if vis and vis.HasField("geometry"):
                    writer.update_from_geometry(vis.geometry)

    event_counts = collections.Counter(e.event_type for e in events)
    return writer, detector, events, event_counts


def test_scenario_1_goal() -> None:
    """Scenario 1: Shoot & Goal sequence."""
    log_path = SCENARIOS_DIR / "scenario_1_goal.log.gz"
    writer, detector, events, counts = _replay_scenario(log_path)

    # Validate events
    assert "KICKOFF" in counts
    assert "INPLAY_START" in counts
    assert "SHOT" in counts
    assert "POSSIBLE_GOAL" in counts
    assert "GOAL" in counts

    # Check that score in WorldModelWriter is updated
    state = writer.get_game_state_data()
    assert state["score"]["yellow"] == 1
    assert state["score"]["blue"] == 0

    # Check goal event metadata
    goal_events = [e for e in events if e.event_type == "GOAL"]
    assert len(goal_events) >= 1
    assert goal_events[0].metadata["by_team"] == "yellow"
    assert goal_events[0].metadata["kicking_bot"] == 2


def test_scenario_2_foul_card() -> None:
    """Scenario 2: Robot collision and free kick foul."""
    log_path = SCENARIOS_DIR / "scenario_2_foul_card.log.gz"
    writer, detector, events, counts = _replay_scenario(log_path)

    assert "COLLISION" in counts
    assert "STOP" in counts
    assert "FREE_KICK" in counts

    collision_events = [e for e in events if e.event_type == "COLLISION"]
    assert len(collision_events) >= 1
    assert collision_events[0].metadata["by_team"] == "blue"
    assert collision_events[0].metadata["violator"] == 3
    assert collision_events[0].metadata["victim"] == 1


def test_scenario_3_pass_chain() -> None:
    """Scenario 3: Pass chain between multiple robots."""
    log_path = SCENARIOS_DIR / "scenario_3_pass_chain.log.gz"
    writer, detector, events, counts = _replay_scenario(log_path)

    assert counts.get("PASS", 0) >= 2

    pass_events = [e for e in events if e.event_type == "PASS"]
    # First pass: Blue #1 -> Blue #2
    assert pass_events[0].primary_robot["id"] == 1
    assert pass_events[0].secondary_robot["id"] == 2
    # Second pass: Blue #2 -> Blue #3
    assert pass_events[1].primary_robot["id"] == 2
    assert pass_events[1].secondary_robot["id"] == 3


def test_scenario_4_penalty_save() -> None:
    """Scenario 4: Penalty kick with goalkeeper save."""
    log_path = SCENARIOS_DIR / "scenario_4_penalty_save.log.gz"
    writer, detector, events, counts = _replay_scenario(log_path)

    assert "PENALTY" in counts
    assert "SHOT" in counts
    assert "SAVE" in counts

    save_events = [e for e in events if e.event_type == "SAVE"]
    assert len(save_events) >= 1
    assert save_events[0].primary_robot["team"] == "blue"
    assert save_events[0].primary_robot["id"] == 0  # Blue GK


def test_scenario_5_ball_out() -> None:
    """Scenario 5: Ball out of bounds and ball placement."""
    log_path = SCENARIOS_DIR / "scenario_5_ball_out.log.gz"
    writer, detector, events, counts = _replay_scenario(log_path)

    assert "BALL_OUT" in counts
    assert "BALL_PLACEMENT" in counts
    assert "BALL_PLACEMENT_SUCCEEDED" in counts

    placement_events = [e for e in events if e.event_type == "BALL_PLACEMENT_SUCCEEDED"]
    assert len(placement_events) >= 1
    assert placement_events[0].metadata["by_team"] == "yellow"


def test_scenario_6_minimal_smoke() -> None:
    """Scenario 6: Ultra-lightweight log parse and snapshot validation."""
    log_path = SCENARIOS_DIR / "scenario_6_minimal_smoke.log.gz"
    writer, detector, events, counts = _replay_scenario(log_path)

    snap = writer.get_field_snapshot_data()
    assert snap["ball"] is not None
    assert len(snap["robots_blue"]) == 2
    assert len(snap["robots_yellow"]) == 2


def test_gc_referee_events_fixture() -> None:
    """Test EventDetector against gc_referee_events.json fixtures."""
    fixture_path = FIXTURES_DIR / "gc_referee_events.json"
    assert fixture_path.exists()

    with open(fixture_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    detector = EventDetector()
    for item in data["events"]:
        ge = event_pb.GameEvent()
        event_name = item["event_type"].lower()

        # Set corresponding protobuf field
        if hasattr(ge, event_name):
            field = getattr(ge, event_name)
            for k, v in item["payload"].items():
                if k == "by_team" or k == "kicking_team":
                    setattr(field, k, common_pb.YELLOW if v == "yellow" else common_pb.BLUE)
                elif isinstance(v, dict) and hasattr(field, k):
                    sub = getattr(field, k)
                    for sub_k, sub_v in v.items():
                        setattr(sub, sub_k, sub_v)
                elif hasattr(field, k):
                    setattr(field, k, v)

        detected = detector._gc_game_event_to_detected(ge, referee=None)
        assert detected is not None, f"Failed to detect fixture event: {item['name']}"
        assert detected.event_type == item["expected_detected_type"]


def test_tracker_frames_edge_cases_fixture() -> None:
    """Test TrackedFrame behavior with edge case fixture data."""
    fixture_path = FIXTURES_DIR / "tracker_frames_edge_cases.json"
    assert fixture_path.exists()

    with open(fixture_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    detector = EventDetector()
    for case in data["cases"]:
        frame = tracked_pb.TrackedFrame()
        frame.frame_number = 1
        frame.timestamp = 0.0

        ball = frame.balls.add()
        ball.pos.x = case["ball"]["pos"]["x"]
        ball.pos.y = case["ball"]["pos"]["y"]
        ball.vel.x = case["ball"]["vel"]["x"]
        ball.vel.y = case["ball"]["vel"]["y"]
        ball.visibility = case["ball"]["visibility"]

        for r_data in case["robots"]:
            robot = frame.robots.add()
            robot.robot_id.id = r_data["id"]
            robot.robot_id.team = common_pb.BLUE if r_data["team"] == "blue" else common_pb.YELLOW
            robot.pos.x = r_data["pos"]["x"]
            robot.pos.y = r_data["pos"]["y"]
            robot.visibility = r_data["visibility"]

        # If robots are occluded (visibility < 0.5), neither should possess the ball
        if case["name"] == "occluded_robots_zero_visibility":
            n_blue = detector._find_nearest_robot(frame, (ball.pos.x, ball.pos.y), "blue")
            n_yellow = detector._find_nearest_robot(frame, (ball.pos.x, ball.pos.y), "yellow")
            assert n_blue is None
            assert n_yellow is None
