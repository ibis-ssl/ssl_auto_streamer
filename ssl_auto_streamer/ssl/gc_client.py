# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""SSL Game Controller Client - receives Referee messages via UDP multicast."""

import asyncio
import logging
from typing import Callable, List, Optional

from .dual_port_receiver import DualPortReceiver

logger = logging.getLogger(__name__)

DEFAULT_GC_ADDR = "224.5.23.1"
DEFAULT_GC_PORTS = [10003, 11003]


class GCClient:
    """
    Receives SSL Game Controller Referee messages via UDP multicast.

    Parses protobuf and delivers Referee objects to a callback.
    Falls back to raw bytes callback if protobuf is not compiled.
    Listens on two ports simultaneously and auto-switches to the active one.
    """

    def __init__(
        self,
        addr: str = DEFAULT_GC_ADDR,
        ports: Optional[List[int]] = None,
    ):
        self._addr = addr
        self._ports = ports if ports is not None else list(DEFAULT_GC_PORTS)
        self._callback: Optional[Callable] = None
        self._receiver = DualPortReceiver(addr, self._ports)
        self._receiver.set_callback(self._on_data)
        self._proto_available = False
        self._try_import_proto()

    def _try_import_proto(self) -> None:
        """Try to import compiled protobuf classes."""
        try:
            from ssl_auto_streamer.ssl import ssl_gc_referee_message_pb2
            self._referee_pb2 = ssl_gc_referee_message_pb2
            self._proto_available = True
            logger.info("GC Referee protobuf available")
        except ImportError:
            logger.warning(
                "GC Referee protobuf not compiled. Run 'make proto' to enable parsing. "
                "Raw bytes will be delivered."
            )

    def set_callback(self, callback: Callable) -> None:
        """Set callback invoked with Referee message (or raw bytes if proto unavailable)."""
        self._callback = callback

    async def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start receiving GC data."""
        await self._receiver.start(loop)
        logger.info(f"GCClient started: {self._addr} ports={self._ports}")

    def stop(self) -> None:
        """Stop receiving GC data."""
        self._receiver.stop()

    @property
    def active_port(self) -> int:
        return self._receiver.active_port

    def switch_port(self, port: int) -> bool:
        return self._receiver.switch_port(port)

    def get_port_status(self) -> dict:
        return self._receiver.get_port_status()

    def _on_data(self, data: bytes) -> None:
        """Handle raw UDP data."""
        if self._callback is None:
            return

        if not self._proto_available:
            self._callback(data)
            return

        try:
            referee = self._referee_pb2.Referee()
            referee.ParseFromString(data)
            self._callback(referee)
        except Exception as e:
            logger.debug(f"GC Referee parse error: {e}")
