# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""Unit tests for team side, relative location, and reset logic."""

import pytest
from ssl_auto_streamer.event_detector import EventDetector
from ssl_auto_streamer.statler.world_model_writer import (
    DEFAULT_BLUE_TEAM_NAME,
    DEFAULT_YELLOW_TEAM_NAME,
    WorldModelWriter,
)


def test_world_model_writer_reset():
    writer = WorldModelWriter()
    # Change some internal state
    writer._blue_team_name = "TRAPS"
    writer._yellow_team_name = "ibis"
    writer._blue_team_on_positive_half = False
    writer._context.blue_score = 2
    writer._context.yellow_score = 3
    writer.add_event("GOAL", {"primary_robot": {"id": 1, "team": "yellow"}})

    assert writer.are_team_names_known()
    assert writer._context.blue_score == 2
    assert len(writer._context.recent_events) > 0

    # Call reset
    writer.reset()

    assert not writer.are_team_names_known()
    assert writer._blue_team_name == DEFAULT_BLUE_TEAM_NAME
    assert writer._yellow_team_name == DEFAULT_YELLOW_TEAM_NAME
    assert writer._blue_team_on_positive_half is None
    assert writer._context.blue_score == 0
    assert writer._context.yellow_score == 0
    assert len(writer._context.recent_events) == 0
    assert len(writer._highlights) == 0


def test_event_detector_reset():
    detector = EventDetector()
    detector._seen_gc_event_ids.add("evt_1")
    detector._last_gc_command = 2
    detector._shot_in_progress = True
    detector._blue_team_on_positive_half = False

    detector.reset()

    assert len(detector._seen_gc_event_ids) == 0
    assert detector._last_gc_command is None
    assert not detector._shot_in_progress
    assert detector._blue_team_on_positive_half is None


def test_side_aware_position_zone_and_role():
    writer = WorldModelWriter()
    # TRAPS (blue) on negative half (X < 0), ibis (yellow) on positive half (X > 0)
    writer._blue_team_on_positive_half = False

    # For ibis (yellow, defends X > 0):
    # Robot at X = -5.0 is in OPPONENT goal area (attack!)
    zone_yellow_front = writer._get_position_zone(-5.0, team="yellow")
    assert zone_yellow_front == "opponent_goal_area"

    # Robot at X = +5.0 is in OWN goal area (defense!)
    zone_yellow_back = writer._get_position_zone(5.0, team="yellow")
    assert zone_yellow_back == "goal_area"

    # For TRAPS (blue, defends X < 0):
    # Robot at X = -5.0 is in OWN goal area (defense!)
    zone_blue_back = writer._get_position_zone(-5.0, team="blue")
    assert zone_blue_back == "goal_area"

    # Robot at X = +5.0 is in OPPONENT goal area (attack!)
    zone_blue_front = writer._get_position_zone(5.0, team="blue")
    assert zone_blue_front == "opponent_goal_area"


def test_relative_location_description():
    writer = WorldModelWriter()
    # TRAPS defends X < 0, ibis defends X > 0
    writer._blue_team_on_positive_half = False

    # Near center
    desc_center = writer.get_relative_location_desc(0.1, 0.2, team="yellow")
    assert desc_center == "センターサークル付近"

    # X = -1.18, Y = -1.10 for yellow (ibis)
    # Since ibis defends X > 0, X = -1.18 is in opponent territory (敵陣)
    desc_ibis_attack = writer.get_relative_location_desc(-1.18, -1.10, team="yellow")
    assert "敵陣" in desc_ibis_attack
    assert "右" in desc_ibis_attack or "サイド" in desc_ibis_attack or "寄り" in desc_ibis_attack

    # Same coordinates for blue (TRAPS):
    # Since TRAPS defends X < 0, X = -1.18 is in own territory (自陣)
    desc_traps_defense = writer.get_relative_location_desc(-1.18, -1.10, team="blue")
    assert "自陣" in desc_traps_defense


def test_game_state_data_contains_sides_and_formatted_score():
    writer = WorldModelWriter()
    writer._blue_team_name = "TRAPS"
    writer._yellow_team_name = "ibis"
    writer._blue_team_on_positive_half = False
    writer._context.yellow_score = 1
    writer._context.blue_score = 0

    state = writer.get_game_state_data()
    assert state["score_formatted"] == "ibis 1 - 0 TRAPS（1対0）"
    assert "team_sides" in state
    assert state["team_sides"]["yellow"]["defending_side"] == "positive_half"
    assert state["team_sides"]["blue"]["defending_side"] == "negative_half"
