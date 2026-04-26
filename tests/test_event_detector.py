import pytest

from ssl_auto_streamer.event_detector import EventDetector, _GC_EVENT_MAP
from ssl_auto_streamer.ssl import ssl_gc_common_pb2 as common_pb
from ssl_auto_streamer.ssl import ssl_gc_game_event_pb2 as game_event_pb
from ssl_auto_streamer.ssl import ssl_gc_referee_message_pb2 as referee_pb


def _referee(command=None, counter=1):
    referee = referee_pb.Referee()
    referee.packet_timestamp = 1
    referee.stage = referee_pb.Referee.NORMAL_FIRST_HALF
    referee.stage_time_left = 300_000_000
    referee.command = (
        referee_pb.Referee.STOP if command is None else command
    )
    referee.command_counter = counter
    referee.command_timestamp = counter

    for team, name in ((referee.yellow, "Yellow"), (referee.blue, "Blue")):
        team.name = name
        team.score = 0
        team.red_cards = 0
        team.yellow_cards = 0
        team.timeouts = 4
        team.timeout_time = 300_000_000
        team.goalkeeper = 0

    return referee


def test_all_gc_game_event_oneof_fields_are_mapped():
    oneof = game_event_pb.GameEvent.DESCRIPTOR.oneofs_by_name["event"]
    event_fields = {field.name.upper() for field in oneof.fields}

    assert event_fields <= set(_GC_EVENT_MAP)


def test_gc_game_event_without_created_timestamp_is_converted_once():
    detector = EventDetector()
    referee = _referee()
    placement = referee.game_events.add().placement_succeeded
    placement.by_team = common_pb.YELLOW
    placement.time_taken = 1.2
    placement.precision = 0.03
    placement.distance = 2.4

    events = detector.update_from_referee(referee)
    duplicate_events = detector.update_from_referee(referee)

    assert len(events) == 1
    assert duplicate_events == []
    event = events[0]
    assert event.event_type == "BALL_PLACEMENT_SUCCEEDED"
    assert event.metadata["by_team"] == "yellow"
    assert event.metadata["time_taken"] == pytest.approx(1.2)
    assert event.metadata["precision"] == pytest.approx(0.03)
    assert event.metadata["distance"] == pytest.approx(2.4)


def test_type_only_gc_placement_event_is_converted():
    detector = EventDetector()
    referee = _referee()
    event = referee.game_events.add()
    event.type = game_event_pb.GameEvent.PLACEMENT_FAILED

    events = detector.update_from_referee(referee)

    assert len(events) == 1
    assert events[0].event_type == "BALL_PLACEMENT_FAILED"
    assert events[0].metadata == {
        "gc_event_type": "PLACEMENT_FAILED",
        "gc_event_has_payload": False,
    }


def test_unmapped_gc_game_event_is_logged_as_generic_game_event():
    detector = EventDetector()
    referee = _referee()
    event = referee.game_events.add()
    event.type = game_event_pb.GameEvent.UNKNOWN_GAME_EVENT_TYPE

    events = detector.update_from_referee(referee)

    assert len(events) == 1
    assert events[0].event_type == "GAME_EVENT"
    assert events[0].metadata == {
        "gc_event_type": "UNKNOWN_GAME_EVENT_TYPE",
        "gc_event_has_payload": False,
        "log_only": True,
    }


def test_identical_gc_game_events_at_different_indices_are_logged_separately():
    detector = EventDetector()
    referee = _referee()
    for _ in range(2):
        placement = referee.game_events.add().placement_failed
        placement.by_team = common_pb.BLUE
        placement.remaining_distance = 0.2

    first_events = detector.update_from_referee(referee)
    duplicate_packet_events = detector.update_from_referee(referee)

    assert [event.event_type for event in first_events] == [
        "BALL_PLACEMENT_FAILED",
        "BALL_PLACEMENT_FAILED",
    ]
    assert duplicate_packet_events == []


def test_gc_game_event_is_not_repeated_when_command_counter_changes():
    detector = EventDetector()
    first = _referee()
    placement = first.game_events.add().placement_succeeded
    placement.by_team = common_pb.YELLOW
    placement.time_taken = 1.2

    second = _referee(referee_pb.Referee.BALL_PLACEMENT_YELLOW, counter=2)
    second.designated_position.x = 1000.0
    second.designated_position.y = 500.0
    second.game_events.add().CopyFrom(first.game_events[0])

    first_events = detector.update_from_referee(first)
    second_events = detector.update_from_referee(second)

    assert [event.event_type for event in first_events] == [
        "BALL_PLACEMENT_SUCCEEDED"
    ]
    assert [event.event_type for event in second_events] == ["BALL_PLACEMENT"]


def test_initial_ball_placement_command_emits_event_with_target_position():
    detector = EventDetector()
    referee = _referee(referee_pb.Referee.BALL_PLACEMENT_BLUE)
    referee.designated_position.x = 1500.0
    referee.designated_position.y = -750.0
    referee.next_command = referee_pb.Referee.INDIRECT_FREE_BLUE

    events = detector.update_from_referee(referee)

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "BALL_PLACEMENT"
    assert event.position == (1.5, -0.75)
    assert event.metadata["team"] == "blue"
    assert event.metadata["target_position"] == {"x": 1.5, "y": -0.75}
    assert event.metadata["next_command"] == "INDIRECT_FREE_BLUE"


def test_same_command_with_new_counter_emits_new_set_play_event():
    detector = EventDetector()
    first = _referee(referee_pb.Referee.BALL_PLACEMENT_YELLOW, counter=1)
    first.designated_position.x = 1000.0
    first.designated_position.y = 0.0
    detector.update_from_referee(first)

    second = _referee(referee_pb.Referee.BALL_PLACEMENT_YELLOW, counter=2)
    second.designated_position.x = 2500.0
    second.designated_position.y = 500.0

    events = detector.update_from_referee(second)

    assert len(events) == 1
    assert events[0].event_type == "BALL_PLACEMENT"
    assert events[0].position == (2.5, 0.5)


def test_defender_in_defense_area_maps_to_foul_with_robot_and_metadata():
    detector = EventDetector()
    referee = _referee()
    foul = referee.game_events.add().defender_in_defense_area
    foul.by_team = common_pb.BLUE
    foul.by_bot = 4
    foul.location.x = -1.0
    foul.location.y = 0.25
    foul.distance = 0.18

    events = detector.update_from_referee(referee)

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "FOUL"
    assert event.position == (-1.0, 0.25)
    assert event.primary_robot == {"id": 4, "team": "blue"}
    assert event.metadata["gc_event_type"] == "DEFENDER_IN_DEFENSE_AREA"
    assert event.metadata["distance"] == pytest.approx(0.18)
