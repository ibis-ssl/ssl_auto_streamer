# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""SSL Vision Tracker Client - receives TrackedFrame via UDP multicast."""

import asyncio
import logging
from typing import Callable, Optional

from .multicast_receiver import MulticastReceiver

logger = logging.getLogger(__name__)

DEFAULT_TRACKER_ADDR = "224.5.23.2"
DEFAULT_TRACKER_PORT = 10010


class TrackerClient:
    """
    Receives SSL Vision tracked data (TrackerWrapperPacket) via UDP multicast.

    Parses protobuf and delivers TrackedFrame objects to a callback.
    Falls back to raw bytes callback if protobuf is not compiled.
    """

    def __init__(
        self,
        addr: str = DEFAULT_TRACKER_ADDR,
        port: int = DEFAULT_TRACKER_PORT,
    ):
        self._addr = addr
        self._port = port
        self._callback: Optional[Callable] = None
        self._receiver = MulticastReceiver(addr, port)
        self._receiver.set_callback(self._on_data)
        self._proto_available = False
        self._try_import_proto()

    def _try_import_proto(self) -> None:
        """Try to import compiled protobuf classes."""
        try:
            from ssl_auto_streamer.ssl import ssl_vision_wrapper_tracked_pb2
            self._wrapper_pb2 = ssl_vision_wrapper_tracked_pb2
            self._proto_available = True
            logger.info("Tracker protobuf available")
        except ImportError:
            logger.warning(
                "Tracker protobuf not compiled. Run 'make proto' to enable parsing. "
                "Raw bytes will be delivered."
            )

    def set_callback(self, callback: Callable) -> None:
        """Set callback invoked with TrackedFrame (or raw bytes if proto unavailable)."""
        self._callback = callback

    async def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start receiving tracker data."""
        await self._receiver.start(loop)
        logger.info(f"TrackerClient started: {self._addr}:{self._port}")

    def stop(self) -> None:
        """Stop receiving tracker data."""
        self._receiver.stop()

    def _on_data(self, data: bytes) -> None:
        """Handle raw UDP data."""
        if self._callback is None:
            return

        if not self._proto_available:
            self._callback(data)
            return

        try:
            wrapper = self._wrapper_pb2.TrackerWrapperPacket()
            wrapper.ParseFromString(data)
            if wrapper.HasField("tracked_frame"):
                self._callback(wrapper.tracked_frame)
        except Exception as e:
            logger.debug(f"Tracker parse error: {e}")
