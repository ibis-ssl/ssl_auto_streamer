# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""SSL Vision Client - receives SSL_WrapperPacket via UDP multicast."""

import asyncio
import logging
from typing import Callable, List, Optional

from .dual_port_receiver import DualPortReceiver

logger = logging.getLogger(__name__)

DEFAULT_VISION_ADDR = "224.5.23.1"
DEFAULT_VISION_PORTS = [10006, 10020]


class VisionClient:
    """
    Receives SSL Vision packets (SSL_WrapperPacket) via UDP multicast.

    Parses protobuf and delivers:
      - SSL_GeometryData to geometry_callback when a geometry packet is received.
    Detection frames are not forwarded (handled by TrackerClient instead).

    Falls back gracefully if protobuf is not compiled.
    """

    def __init__(
        self,
        addr: str = DEFAULT_VISION_ADDR,
        ports: Optional[List[int]] = None,
    ):
        self._addr = addr
        self._ports = ports if ports is not None else list(DEFAULT_VISION_PORTS)
        self._geometry_callback: Optional[Callable] = None
        self._receiver = DualPortReceiver(addr, self._ports)
        self._receiver.set_callback(self._on_data)
        self._proto_available = False
        self._try_import_proto()

    def _try_import_proto(self) -> None:
        try:
            from ssl_auto_streamer.ssl import ssl_vision_wrapper_pb2
            self._wrapper_pb2 = ssl_vision_wrapper_pb2
            self._proto_available = True
            logger.info("Vision protobuf available")
        except ImportError:
            logger.warning(
                "Vision protobuf not compiled. Run 'make proto' to enable parsing."
            )

    def set_geometry_callback(self, callback: Callable) -> None:
        """Set callback invoked with SSL_GeometryData when geometry is received."""
        self._geometry_callback = callback

    async def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start receiving vision data."""
        await self._receiver.start(loop)
        logger.info(f"VisionClient started: {self._addr} ports={self._ports}")

    def stop(self) -> None:
        """Stop receiving vision data."""
        self._receiver.stop()

    @property
    def active_port(self) -> int:
        return self._receiver.active_port

    def switch_port(self, port: int) -> bool:
        return self._receiver.switch_port(port)

    def get_port_status(self) -> dict:
        return self._receiver.get_port_status()

    def _on_data(self, data: bytes) -> None:
        if not self._proto_available:
            return

        try:
            wrapper = self._wrapper_pb2.SSL_WrapperPacket()
            wrapper.ParseFromString(data)
            if wrapper.HasField("geometry") and self._geometry_callback is not None:
                self._geometry_callback(wrapper.geometry)
        except Exception as e:
            logger.debug(f"Vision parse error: {e}")
