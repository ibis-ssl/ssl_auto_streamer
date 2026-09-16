# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""Log Player - re-broadcasts RoboCup SSL log files over UDP."""

import argparse
import asyncio
import logging
import socket
from pathlib import Path
from typing import Optional

from ssl_auto_streamer.ssl.log_reader import (
    MSG_TYPE_SSL_REFBOX_2013,
    MSG_TYPE_SSL_VISION_2014,
    MSG_TYPE_SSL_VISION_TRACKER_2020,
    SSLLogReader,
)

logger = logging.getLogger(__name__)

DEFAULT_GC_ADDR = "224.5.23.1"
DEFAULT_VISION_ADDR = "224.5.23.1"
DEFAULT_TRACKER_ADDR = "224.5.23.2"

DEFAULT_GC_PORT = 11003
DEFAULT_VISION_PORT = 10006
DEFAULT_TRACKER_PORT = 11010


class SSLLogPlayer:
    """Plays back SSL log files by sending UDP datagrams to configured destinations."""

    def __init__(
        self,
        log_path: Path,
        speed: float = 1.0,
        loop: bool = False,
        target_ip: Optional[str] = None,
        gc_port: int = DEFAULT_GC_PORT,
        vision_port: int = DEFAULT_VISION_PORT,
        tracker_port: int = DEFAULT_TRACKER_PORT,
        duration: Optional[float] = None,
    ):
        self._log_path = log_path
        self._speed = speed
        self._loop = loop
        self._target_ip = target_ip
        self._gc_port = gc_port
        self._vision_port = vision_port
        self._tracker_port = tracker_port
        self._duration = duration

        # Sockets
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        # Enable broadcast/multicast
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Set multicast TTL to 1 for local subnet
        self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        # Enable multicast loopback so local receivers on same machine can receive
        self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)

    def _get_destination(self, msg_type: int) -> Optional[tuple[str, int]]:
        if self._target_ip:
            # Unicast override (e.g. 127.0.0.1)
            ip = self._target_ip
            if msg_type == MSG_TYPE_SSL_REFBOX_2013:
                return (ip, self._gc_port)
            elif msg_type == MSG_TYPE_SSL_VISION_2014:
                return (ip, self._vision_port)
            elif msg_type == MSG_TYPE_SSL_VISION_TRACKER_2020:
                return (ip, self._tracker_port)
            return None

        # Multicast defaults
        if msg_type == MSG_TYPE_SSL_REFBOX_2013:
            return (DEFAULT_GC_ADDR, self._gc_port)
        elif msg_type == MSG_TYPE_SSL_VISION_2014:
            return (DEFAULT_VISION_ADDR, self._vision_port)
        elif msg_type == MSG_TYPE_SSL_VISION_TRACKER_2020:
            return (DEFAULT_TRACKER_ADDR, self._tracker_port)
        return None

    async def run(self) -> None:
        """Start playing back log file."""
        logger.info(f"Opening log file {self._log_path} (speed={self._speed}x, loop={self._loop})")

        with SSLLogReader(self._log_path) as reader:
            start_time = asyncio.get_event_loop().time()
            packet_count = 0

            async for pkt in reader.iter_timed_packets(speed=self._speed, loop=self._loop):
                dest = self._get_destination(pkt.message_type)
                if dest:
                    try:
                        self._sock.sendto(pkt.payload, dest)
                    except Exception as e:
                        logger.debug(f"Failed to send UDP packet to {dest}: {e}")

                packet_count += 1

                if self._duration is not None:
                    elapsed = asyncio.get_event_loop().time() - start_time
                    if elapsed >= self._duration:
                        logger.info(f"Reached duration limit: {self._duration}s")
                        break

            logger.info(f"Playback finished: sent {packet_count} packets.")

    def close(self) -> None:
        """Close UDP socket."""
        self._sock.close()


def main() -> None:
    """CLI entry point for ssl-log-player."""
    parser = argparse.ArgumentParser(
        description="Re-broadcast RoboCup SSL log file packets over UDP."
    )
    parser.add_argument("log_file", type=Path, help="Path to .log or .log.gz file")
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="Playback speed multiplier (1.0 = real-time, 2.0 = 2x, 0 = max speed)",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Loop playback indefinitely",
    )
    parser.add_argument(
        "--target-ip",
        type=str,
        default=None,
        help="Unicast IP target (e.g. 127.0.0.1). Defaults to standard multicast addresses.",
    )
    parser.add_argument(
        "--gc-port",
        type=int,
        default=DEFAULT_GC_PORT,
        help=f"GC port (default: {DEFAULT_GC_PORT})",
    )
    parser.add_argument(
        "--vision-port",
        type=int,
        default=DEFAULT_VISION_PORT,
        help=f"Vision port (default: {DEFAULT_VISION_PORT})",
    )
    parser.add_argument(
        "--tracker-port",
        type=int,
        default=DEFAULT_TRACKER_PORT,
        help=f"Tracker port (default: {DEFAULT_TRACKER_PORT})",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Maximum playback duration in seconds",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    player = SSLLogPlayer(
        log_path=args.log_file,
        speed=args.speed,
        loop=args.loop,
        target_ip=args.target_ip,
        gc_port=args.gc_port,
        vision_port=args.vision_port,
        tracker_port=args.tracker_port,
        duration=args.duration,
    )

    try:
        asyncio.run(player.run())
    except KeyboardInterrupt:
        logger.info("Playback cancelled by user.")
    finally:
        player.close()


if __name__ == "__main__":
    main()
