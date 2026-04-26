from ssl_auto_streamer.ssl import ssl_vision_detection_tracked_pb2 as tracked_pb
from ssl_auto_streamer.ssl import ssl_vision_geometry_pb2 as geometry_pb
from ssl_auto_streamer.ssl import ssl_gc_referee_message_pb2 as referee_pb
from ssl_auto_streamer.statler.world_model_writer import WorldModelWriter


def _tracked_frame(robot_visibility=None):
    frame = tracked_pb.TrackedFrame()
    frame.frame_number = 1
    frame.timestamp = 1.0

    ball = frame.balls.add()
    ball.pos.x = 1.2
    ball.pos.y = -0.4
    ball.pos.z = 0.0
    ball.vel.x = 0.0
    ball.vel.y = 0.0
    ball.vel.z = 0.0

    robot = frame.robots.add()
    robot.robot_id.id = 3
    robot.robot_id.team = 2
    robot.pos.x = 0.5
    robot.pos.y = 0.25
    robot.orientation = 0.1
    robot.vel.x = 0.0
    robot.vel.y = 0.0
    robot.vel_angular = 0.0
    if robot_visibility is not None:
        robot.visibility = robot_visibility

    return frame


def test_tracker_robot_without_visibility_is_rendered_on_field():
    writer = WorldModelWriter()

    writer.update_from_tracker(_tracked_frame())

    snapshot = writer.get_field_snapshot_data()
    assert len(snapshot["robots_blue"]) == 1
    assert snapshot["robots_blue"][0]["id"] == 3
    assert snapshot["robots_blue"][0]["x"] == 0.5
    assert snapshot["robots_blue"][0]["y"] == 0.25


def test_tracker_robot_with_explicit_low_visibility_is_hidden():
    writer = WorldModelWriter()

    writer.update_from_tracker(_tracked_frame(robot_visibility=0.0))

    snapshot = writer.get_field_snapshot_data()
    assert snapshot["robots_blue"] == []


def test_field_snapshot_includes_geometry_dimensions():
    writer = WorldModelWriter()
    geometry = geometry_pb.SSL_GeometryData()
    geometry.field.field_length = 9000
    geometry.field.field_width = 6000
    geometry.field.goal_width = 1000
    geometry.field.goal_depth = 180
    geometry.field.boundary_width = 300
    geometry.field.penalty_area_depth = 1000
    geometry.field.penalty_area_width = 2000

    writer.update_from_geometry(geometry)

    assert writer.get_field_snapshot_data()["field"] == {
        "length": 9.0,
        "width": 6.0,
        "goal_width": 1.0,
        "goal_depth": 0.18,
        "penalty_depth": 1.0,
        "penalty_width": 2.0,
    }


def _referee(command):
    referee = referee_pb.Referee()
    referee.packet_timestamp = 1
    referee.stage = referee_pb.Referee.NORMAL_FIRST_HALF
    referee.stage_time_left = 300_000_000
    referee.command = command
    referee.command_counter = 1
    referee.command_timestamp = 1

    for team, name in ((referee.yellow, "Yellow"), (referee.blue, "Blue")):
        team.name = name
        team.score = 0
        team.red_cards = 0
        team.yellow_cards = 0
        team.timeouts = 4
        team.timeout_time = 300_000_000
        team.goalkeeper = 0

    return referee


def test_game_state_includes_ball_placement_state():
    writer = WorldModelWriter()
    referee = _referee(referee_pb.Referee.BALL_PLACEMENT_YELLOW)
    referee.designated_position.x = 1234.0
    referee.designated_position.y = -567.0
    referee.next_command = referee_pb.Referee.INDIRECT_FREE_YELLOW
    referee.current_action_time_remaining = 12_500_000
    referee.yellow.can_place_ball = True
    referee.yellow.ball_placement_failures = 1
    referee.yellow.ball_placement_failures_reached = False
    referee.blue.can_place_ball = False
    referee.blue.ball_placement_failures = 3
    referee.blue.ball_placement_failures_reached = True

    writer.update_from_referee(referee)

    state = writer.get_game_state_data()["ball_placement"]
    assert state["active"] is True
    assert state["team"] == "yellow"
    assert state["target_position"] == {"x": 1.234, "y": -0.567}
    assert state["next_command"] == "INDIRECT_FREE_YELLOW"
    assert state["time_remaining_sec"] == 12.5
    assert state["teams"]["yellow"] == {
        "can_place_ball": True,
        "failures": 1,
        "failures_reached": False,
    }
    assert state["teams"]["blue"] == {
        "can_place_ball": False,
        "failures": 3,
        "failures_reached": True,
    }
