# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""FieldVisualStreamer - Renders SSL field snapshots as images for Gemini 3.8 Live visual input."""

import io
import math
import logging
from typing import Optional, Tuple

try:
    from PIL import Image, ImageDraw, ImageFont

    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    Image = None
    ImageDraw = None
    ImageFont = None

from ssl_auto_streamer.statler.world_model_writer import WorldModelWriter

logger = logging.getLogger(__name__)


class FieldVisualStreamer:
    """Renders 2D bird's-eye view snapshots of the SSL field for Gemini 3.8 Live API."""

    def __init__(
        self,
        writer: WorldModelWriter,
        width: int = 640,
        height: int = 480,
        jpeg_quality: int = 75,
    ):
        self._writer = writer
        self._width = width
        self._height = height
        self._quality = jpeg_quality
        self._last_frame_bytes: Optional[bytes] = None

    @property
    def is_available(self) -> bool:
        """Return True if Pillow is installed and visual streaming is possible."""
        return PIL_AVAILABLE

    def get_last_frame(self) -> Optional[bytes]:
        """Return the latest rendered JPEG frame bytes (useful for Web UI preview)."""
        return self._last_frame_bytes

    def render_frame_bytes(self) -> Optional[bytes]:
        """Render current field state as a JPEG byte buffer."""
        if not PIL_AVAILABLE:
            logger.debug("Pillow is not available; cannot render field image")
            return None

        try:
            snapshot = self._writer.get_field_snapshot_data()
            game_state = self._writer.get_game_state_data()
            blue_name, yellow_name = self._writer.get_team_names()

            field_cfg = snapshot.get("field", {})
            field_length = field_cfg.get("length", 9.0)
            field_width = field_cfg.get("width", 6.0)

            # Create RGB image with dark green pitch
            img = Image.new("RGB", (self._width, self._height), color=(30, 95, 45))
            draw = ImageDraw.Draw(img)

            # Margins (pixels)
            margin_x = 40
            margin_y = 40
            pitch_w = self._width - 2 * margin_x
            pitch_h = self._height - 2 * margin_y

            # Coordinate transformer: SSL (meters, center=0,0) -> Pixels
            def to_px(x: float, y: float) -> Tuple[float, float]:
                px = margin_x + (x + field_length / 2.0) * (pitch_w / field_length)
                # Note: SSL y points up/left, screen y points down
                py = margin_y + (field_width / 2.0 - y) * (pitch_h / field_width)
                return px, py

            # 1. Pitch boundary
            x0, y0 = to_px(-field_length / 2.0, field_width / 2.0)
            x1, y1 = to_px(field_length / 2.0, -field_width / 2.0)
            draw.rectangle([x0, y0, x1, y1], outline=(230, 230, 230), width=2)

            # 2. Halfway line & Center circle
            cx, cy0 = to_px(0.0, field_width / 2.0)
            _, cy1 = to_px(0.0, -field_width / 2.0)
            draw.line([cx, cy0, cx, cy1], fill=(230, 230, 230), width=2)

            # Center circle (radius ~0.5m)
            c_center_x, c_center_y = to_px(0.0, 0.0)
            r_px = 0.5 * (pitch_w / field_length)
            draw.ellipse(
                [c_center_x - r_px, c_center_y - r_px, c_center_x + r_px, c_center_y + r_px],
                outline=(230, 230, 230),
                width=2,
            )

            # 3. Penalty areas
            pen_depth = field_cfg.get("penalty_depth", 1.0)
            pen_width = field_cfg.get("penalty_width", 2.0)

            # Left (Blue goal side: -field_length/2)
            pl_x0, pl_y0 = to_px(-field_length / 2.0, pen_width / 2.0)
            pl_x1, pl_y1 = to_px(-field_length / 2.0 + pen_depth, -pen_width / 2.0)
            draw.rectangle([pl_x0, pl_y0, pl_x1, pl_y1], outline=(230, 230, 230), width=1)

            # Right (Yellow goal side: +field_length/2)
            pr_x0, pr_y0 = to_px(field_length / 2.0 - pen_depth, pen_width / 2.0)
            pr_x1, pr_y1 = to_px(field_length / 2.0, -pen_width / 2.0)
            draw.rectangle([pr_x0, pr_y0, pr_x1, pr_y1], outline=(230, 230, 230), width=1)

            # 4. Robots
            robot_r_px = max(6, int(0.09 * (pitch_w / field_length)))  # ~90mm SSL bot radius

            # Blue robots
            for r in snapshot.get("robots_blue", []):
                rx, ry = to_px(r["x"], r["y"])
                outline = (255, 255, 255) if r.get("has_ball") else (20, 40, 150)
                draw.ellipse(
                    [rx - robot_r_px, ry - robot_r_px, rx + robot_r_px, ry + robot_r_px],
                    fill=(40, 120, 240),
                    outline=outline,
                    width=2 if r.get("has_ball") else 1,
                )
                # Direction heading line
                theta = r.get("theta", 0.0)
                hx = rx + robot_r_px * 1.4 * math.cos(theta)
                hy = ry - robot_r_px * 1.4 * math.sin(theta)
                draw.line([rx, ry, hx, hy], fill=(220, 240, 255), width=2)
                # Label ID
                draw.text((rx - 3, ry - 4), str(r.get("id", "")), fill=(255, 255, 255))

            # Yellow robots
            for r in snapshot.get("robots_yellow", []):
                rx, ry = to_px(r["x"], r["y"])
                outline = (255, 255, 255) if r.get("has_ball") else (160, 120, 10)
                draw.ellipse(
                    [rx - robot_r_px, ry - robot_r_px, rx + robot_r_px, ry + robot_r_px],
                    fill=(240, 195, 30),
                    outline=outline,
                    width=2 if r.get("has_ball") else 1,
                )
                # Direction heading line
                theta = r.get("theta", 0.0)
                hx = rx + robot_r_px * 1.4 * math.cos(theta)
                hy = ry - robot_r_px * 1.4 * math.sin(theta)
                draw.line([rx, ry, hx, hy], fill=(50, 40, 0), width=2)
                # Label ID
                draw.text((rx - 3, ry - 4), str(r.get("id", "")), fill=(0, 0, 0))

            # 5. Ball (bright orange with white border)
            ball = snapshot.get("ball", {})
            bx, by = to_px(ball.get("x", 0.0), ball.get("y", 0.0))
            ball_r_px = max(4, int(0.043 * (pitch_w / field_length) * 1.5))
            draw.ellipse(
                [bx - ball_r_px, by - ball_r_px, bx + ball_r_px, by + ball_r_px],
                fill=(255, 80, 0),
                outline=(255, 255, 255),
                width=1,
            )

            # 6. Top overlay info (Score, Command, Teams)
            score_text = (
                f"[{blue_name}] {game_state.get('blue_score', 0)} - "
                f"{game_state.get('yellow_score', 0)} [{yellow_name}]  |  "
                f"STATE: {game_state.get('play_situation', 'UNKNOWN')}"
            )
            draw.text((margin_x, 12), score_text, fill=(240, 240, 240))

            # Output to JPEG byte stream
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=self._quality)
            frame_bytes = buf.getvalue()
            self._last_frame_bytes = frame_bytes
            return frame_bytes

        except Exception as e:
            logger.error(f"Error rendering visual frame: {e}")
            return None
