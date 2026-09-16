# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""SSL Log File Reader - parses RoboCup SSL log files (.log and .log.gz)."""

import asyncio
import gzip
import logging
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, BinaryIO, Iterator, Optional, Union

logger = logging.getLogger(__name__)

MAGIC_HEADER = b"SSL_LOG_FILE"
SUPPORTED_VERSION = 1

# Message type constants from RoboCup SSL log tools
MSG_TYPE_BLANK = 0
MSG_TYPE_UNKNOWN = 1
MSG_TYPE_SSL_VISION_2010 = 2
MSG_TYPE_SSL_REFBOX_2013 = 3
MSG_TYPE_SSL_VISION_2014 = 4
MSG_TYPE_SSL_VISION_TRACKER_2020 = 5
MSG_TYPE_SSL_INDEX_2021 = 6


@dataclass
class LogPacket:
    """A single packet record from an SSL log file."""

    timestamp_ns: int
    message_type: int
    payload: bytes

    @property
    def timestamp_sec(self) -> float:
        """Timestamp in seconds (floating point)."""
        return self.timestamp_ns / 1e9

    def decode(self) -> Optional[object]:
        """
        Decode protobuf payload according to message_type.

        Returns:
            Referee for MSG_TYPE_SSL_REFBOX_2013 (3)
            SSL_WrapperPacket for MSG_TYPE_SSL_VISION_2014 (4)
            TrackerWrapperPacket for MSG_TYPE_SSL_VISION_TRACKER_2020 (5)
            None if message type is unsupported or decode fails.
        """
        if self.message_type == MSG_TYPE_SSL_REFBOX_2013:
            try:
                from ssl_auto_streamer.ssl import ssl_gc_referee_message_pb2

                ref = ssl_gc_referee_message_pb2.Referee()
                ref.ParseFromString(self.payload)
                return ref
            except Exception as e:
                logger.debug(f"Failed to decode Referee message: {e}")
                return None

        elif self.message_type == MSG_TYPE_SSL_VISION_2014:
            try:
                from ssl_auto_streamer.ssl import ssl_vision_wrapper_pb2

                wrapper = ssl_vision_wrapper_pb2.SSL_WrapperPacket()
                wrapper.ParseFromString(self.payload)
                return wrapper
            except Exception as e:
                logger.debug(f"Failed to decode Vision wrapper: {e}")
                return None

        elif self.message_type == MSG_TYPE_SSL_VISION_TRACKER_2020:
            try:
                from ssl_auto_streamer.ssl import ssl_vision_wrapper_tracked_pb2

                tracker = ssl_vision_wrapper_tracked_pb2.TrackerWrapperPacket()
                tracker.ParseFromString(self.payload)
                return tracker
            except Exception as e:
                logger.debug(f"Failed to decode Tracker wrapper: {e}")
                return None

        return None


class SSLLogReader:
    """
    Reader for RoboCup SSL binary log files (.log or .log.gz).

    Format Version 1:
        Header:
            12 bytes: "SSL_LOG_FILE"
             4 bytes: format version (int32 big-endian)
        Per message:
             8 bytes: timestamp (int64 big-endian, nanoseconds)
             4 bytes: message type (int32 big-endian)
             4 bytes: message size in bytes (int32 big-endian)
             N bytes: protobuf binary message
    """

    def __init__(self, path: Union[str, Path]):
        self._path = Path(path)
        self._file: Optional[BinaryIO] = None
        self._version: Optional[int] = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def version(self) -> Optional[int]:
        return self._version

    def open(self) -> "SSLLogReader":
        """Open the log file and read the header."""
        if not self._path.exists():
            raise FileNotFoundError(f"Log file not found: {self._path}")

        # Check if gzip
        if str(self._path).endswith(".gz"):
            self._file = gzip.open(self._path, "rb")
        else:
            self._file = open(self._path, "rb")

        magic = self._file.read(len(MAGIC_HEADER))
        if magic != MAGIC_HEADER:
            self.close()
            raise ValueError(
                f"Invalid log file format: magic={magic!r}, expected {MAGIC_HEADER!r}"
            )

        ver_bytes = self._file.read(4)
        if len(ver_bytes) < 4:
            self.close()
            raise ValueError("Truncated header in log file")

        self._version = struct.unpack(">i", ver_bytes)[0]
        if self._version != SUPPORTED_VERSION:
            logger.warning(
                f"Log file version {self._version} might not be fully compatible (expected {SUPPORTED_VERSION})"
            )

        return self

    def close(self) -> None:
        """Close the open file."""
        if self._file is not None:
            self._file.close()
            self._file = None

    def __enter__(self) -> "SSLLogReader":
        return self.open()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def read_packet(self) -> Optional[LogPacket]:
        """Read the next packet from the log file, or None if EOF."""
        if self._file is None:
            raise RuntimeError("SSLLogReader is not opened")

        header = self._file.read(16)
        if not header or len(header) < 16:
            return None

        ts, msg_type, size = struct.unpack(">qii", header)
        if size < 0:
            raise ValueError(f"Invalid packet size: {size}")

        payload = self._file.read(size)
        if len(payload) < size:
            logger.warning(
                f"Incomplete packet payload: expected {size} bytes, got {len(payload)}"
            )
            return None

        return LogPacket(timestamp_ns=ts, message_type=msg_type, payload=payload)

    def iter_packets(self, max_packets: Optional[int] = None) -> Iterator[LogPacket]:
        """Iterate through packets sequentially."""
        count = 0
        while True:
            if max_packets is not None and count >= max_packets:
                break
            pkt = self.read_packet()
            if pkt is None:
                break
            yield pkt
            count += 1

    async def iter_timed_packets(
        self,
        speed: float = 1.0,
        loop: bool = False,
        max_packets: Optional[int] = None,
    ) -> AsyncIterator[LogPacket]:
        """
        Asynchronously yield packets with timing aligned to the recorded timestamps.

        Args:
            speed: Playback speed multiplier (1.0 = real-time, 2.0 = 2x, 0 = no delay).
            loop: If True, rewind to the start when reaching EOF.
            max_packets: Maximum number of packets to yield before stopping.
        """
        packet_count = 0

        while True:
            prev_ts: Optional[int] = None
            last_yield_time = time.monotonic()

            for pkt in self.iter_packets():
                if max_packets is not None and packet_count >= max_packets:
                    return

                if prev_ts is not None and speed > 0:
                    dt_log = (pkt.timestamp_ns - prev_ts) / 1e9
                    # Prevent long delays if log contains unexpected gaps
                    dt_log = max(0.0, min(dt_log, 5.0))
                    target_delay = dt_log / speed

                    # Adjust for processing time
                    elapsed = time.monotonic() - last_yield_time
                    delay = target_delay - elapsed
                    if delay > 0.001:
                        await asyncio.sleep(delay)

                prev_ts = pkt.timestamp_ns
                last_yield_time = time.monotonic()
                packet_count += 1
                yield pkt

            if not loop:
                break

            # Rewind to start of file (after header)
            logger.info("SSLLogReader: Reached EOF, rewinding...")
            self.close()
            self.open()
