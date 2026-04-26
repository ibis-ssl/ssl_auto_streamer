# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""Audio output mode helpers."""

DEFAULT_AUDIO_OUTPUT_MODE = "server"
VALID_AUDIO_OUTPUT_MODES = ("server", "client", "both", "off")


def normalize_audio_output_mode(value: object) -> str:
    """Return a supported audio output mode, defaulting to server output."""
    mode = str(value or DEFAULT_AUDIO_OUTPUT_MODE).strip().lower()
    if mode in VALID_AUDIO_OUTPUT_MODES:
        return mode
    return DEFAULT_AUDIO_OUTPUT_MODE


def is_valid_audio_output_mode(value: object) -> bool:
    """Return whether value is one of the supported output modes."""
    return str(value or "").strip().lower() in VALID_AUDIO_OUTPUT_MODES


def uses_server_audio(mode: object) -> bool:
    """Return whether the mode should play audio through server PyAudio."""
    return normalize_audio_output_mode(mode) in ("server", "both")


def uses_client_audio(mode: object) -> bool:
    """Return whether the mode should stream output audio to web clients."""
    return normalize_audio_output_mode(mode) in ("client", "both")
