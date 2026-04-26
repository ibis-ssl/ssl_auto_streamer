import json

from ssl_auto_streamer.data import generate_initial_context


def _ssl_rules():
    return {
        "basic_info": {
            "field_size": "test field",
            "robots": "test robots",
            "match_duration": "test duration",
            "ball_speed_limit": 6.5,
            "robot_speed_in_stop": 1.5,
        },
        "fouls": {},
        "set_plays": {},
    }


def _team_profiles():
    return {
        "profiles": {
            "ibis": {
                "reading": "アイビス",
                "style": "勢い",
            }
        },
        "default_profile": {
            "reading": "不明なチーム",
            "style": "不明",
        },
    }


def test_generate_initial_context_includes_tournament_context_when_provided():
    tournament_context = {
        "tournament": {"name": "RoboCup Japan Open 2026"},
        "round_robin": {
            "ranking_policy": "掲載済み結果から順位計算しない",
            "results": [
                {
                    "team_a": "ibis",
                    "team_b": "ZUNOH Robotics",
                    "score_a": 6,
                    "score_b": 0,
                }
            ],
        },
    }

    context = json.loads(
        generate_initial_context(
            _ssl_rules(),
            _team_profiles(),
            blue_team_name="ibis",
            tournament_context=tournament_context,
        )
    )

    assert context["tournament_context"] == tournament_context
    assert context["tournament_context"]["round_robin"]["results"][0]["score_a"] == 6


def test_generate_initial_context_omits_tournament_context_when_not_provided():
    context = json.loads(generate_initial_context(_ssl_rules(), _team_profiles()))

    assert "tournament_context" not in context


def test_tournament_context_does_not_require_calculated_standings():
    tournament_context = {
        "round_robin": {
            "ranking_policy": "掲載済み結果から順位計算しない",
            "results": [],
        }
    }

    context = json.loads(
        generate_initial_context(
            _ssl_rules(),
            _team_profiles(),
            tournament_context=tournament_context,
        )
    )

    assert "standings" not in context["tournament_context"]
    assert "points" not in context["tournament_context"]["round_robin"]
