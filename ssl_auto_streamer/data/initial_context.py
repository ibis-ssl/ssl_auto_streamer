# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""Initial Context Generator for Gemini Live API."""

import json
from typing import Optional, Dict, Any


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
    our_team_name: str = "ibis",
    their_team_name: Optional[str] = None,
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
        "our_team": {
            "name": get_team_reading_from_data(our_team_name, team_profiles),
            "key": our_team_name,
            **get_team_profile_from_data(our_team_name, team_profiles),
        },
    }

    if their_team_name:
        context["their_team"] = {
            "name": get_team_reading_from_data(their_team_name, team_profiles),
            "key": their_team_name,
            **get_team_profile_from_data(their_team_name, team_profiles),
        }
    else:
        context["their_team"] = {
            "name": "未定",
            "note": "相手チーム情報は試合開始時に更新されます",
        }

    our_profile = get_team_profile_from_data(our_team_name, team_profiles)
    their_profile = (
        get_team_profile_from_data(their_team_name, team_profiles)
        if their_team_name
        else {}
    )
    our_style = our_profile.get("style", "不明")
    their_style = their_profile.get("style", "不明") if their_team_name else "未定"
    if their_team_name and our_style != "不明" and their_style != "不明":
        narrative = f"{our_style} vs {their_style}の対戦。スタイルの違いに注目"
    else:
        narrative = f"{get_team_reading_from_data(our_team_name, team_profiles)}の特徴を活かした戦い方に注目"
    context["matchup"] = {
        "our_style": our_style,
        "their_style": their_style,
        "narrative_angle": narrative,
    }

    return json.dumps(context, ensure_ascii=False, indent=2)
