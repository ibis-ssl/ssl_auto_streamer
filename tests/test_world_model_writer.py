from ssl_auto_streamer.ssl import ssl_vision_detection_tracked_pb2 as tracked_pb
from ssl_auto_streamer.ssl import ssl_vision_geometry_pb2 as geometry_pb
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
