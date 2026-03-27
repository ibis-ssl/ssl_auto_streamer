# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""Initial Context Generator for Gemini Live API."""

import json
from typing import Optional, Dict, Any

from ssl_auto_streamer.statler.world_model_writer import DEFAULT_BLUE_TEAM_NAME, DEFAULT_YELLOW_TEAM_NAME


def get_team_profile_from_data(
    team_name: str, team_profiles: Dict[str, Any]
) -> Dict[str, Any]:
    """Get team profile by name with fuzzy matching from provided data."""
    profiles = team_profiles.get("profiles", {})
    default_profile = team_profiles.get("default_profile", {})

    # Exact match
    if team_name in profiles:
        return profiles[team_name]

    # Case-insensitive partial match
    name_lower = team_name.lower()
    for key, profile in profiles.items():
        if key.lower() in name_lower or name_lower in key.lower():
            return profile

    return default_profile


def get_team_reading_from_data(team_key: str, team_profiles: Dict[str, Any]) -> str:
    """Get team name reading from provided data."""
    profile = get_team_profile_from_data(team_key, team_profiles)
    return profile.get("reading", team_key)


def generate_initial_context(
    ssl_rules: Dict[str, Any],
    team_profiles: Dict[str, Any],
    blue_team_name: Optional[str] = None,
    yellow_team_name: Optional[str] = None,
) -> str:
    """Generate initial context JSON for commentary session."""
    context = {
        "type": "initial_context",
        "ssl_rules": {
            "summary": "RoboCup Small Size League の基本ルール",
            "field": ssl_rules["basic_info"]["field_size"],
            "robots": ssl_rules["basic_info"]["robots"],
            "match_duration": ssl_rules["basic_info"]["match_duration"],
            "speed_limits": {
                "ball_speed_limit_mps": ssl_rules["basic_info"]["ball_speed_limit"],
                "robot_speed_in_stop_mps": ssl_rules["basic_info"][
                    "robot_speed_in_stop"
                ],
            },
            "key_fouls": [
                {"name": name, **details}
                for name, details in ssl_rules["fouls"].items()
            ],
            "set_plays": ssl_rules["set_plays"],
        },
    }

    if blue_team_name:
        context["blue_team"] = {
            "name": get_team_reading_from_data(blue_team_name, team_profiles),
            "key": blue_team_name,
            **get_team_profile_from_data(blue_team_name, team_profiles),
        }
    else:
        context["blue_team"] = {"name": DEFAULT_BLUE_TEAM_NAME, "note": "チーム情報はGCから取得されます"}

    if yellow_team_name:
        context["yellow_team"] = {
            "name": get_team_reading_from_data(yellow_team_name, team_profiles),
            "key": yellow_team_name,
            **get_team_profile_from_data(yellow_team_name, team_profiles),
        }
    else:
        context["yellow_team"] = {"name": DEFAULT_YELLOW_TEAM_NAME, "note": "チーム情報はGCから取得されます"}

    blue_profile = get_team_profile_from_data(blue_team_name, team_profiles) if blue_team_name else {}
    yellow_profile = get_team_profile_from_data(yellow_team_name, team_profiles) if yellow_team_name else {}
    blue_style = blue_profile.get("style", "不明")
    yellow_style = yellow_profile.get("style", "不明")

    if blue_team_name and yellow_team_name and blue_style != "不明" and yellow_style != "不明":
        narrative = f"{blue_style} vs {yellow_style}の対戦。スタイルの違いに注目"
    elif blue_team_name and yellow_team_name:
        narrative = f"{get_team_reading_from_data(blue_team_name, team_profiles)} vs {get_team_reading_from_data(yellow_team_name, team_profiles)}の対戦"
    else:
        narrative = "試合開始を待っています。両チームのGC情報を受信次第、詳細情報を更新します"

    context["matchup"] = {
        "blue_style": blue_style,
        "yellow_style": yellow_style,
        "narrative_angle": narrative,
    }

    return json.dumps(context, ensure_ascii=False, indent=2)
