# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""Generic UDP Multicast Receiver using asyncio."""

import asyncio
import logging
import socket
import struct
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class MulticastProtocol(asyncio.DatagramProtocol):
    """asyncio DatagramProtocol for multicast UDP reception."""

    def __init__(self, callback: Callable[[bytes], None]):
        self._callback = callback
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            self._callback(data)
        except Exception as e:
            logger.error(f"Error in multicast callback: {e}")

    def error_received(self, exc: Exception) -> None:
        logger.error(f"Multicast protocol error: {exc}")

    def connection_lost(self, exc: Optional[Exception]) -> None:
        if exc:
            logger.warning(f"Multicast connection lost: {exc}")


class MulticastReceiver:
    """
    Generic UDP Multicast Receiver.

    Joins a multicast group and delivers raw bytes to a callback.
    """

    def __init__(self, multicast_addr: str, port: int):
        self._multicast_addr = multicast_addr
        self._port = port
        self._callback: Optional[Callable[[bytes], None]] = None
        self._transport: Optional[asyncio.DatagramTransport] = None
        self._protocol: Optional[MulticastProtocol] = None

    def set_callback(self, callback: Callable[[bytes], None]) -> None:
        """Set callback invoked with raw UDP payload bytes."""
        self._callback = callback

    async def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Join multicast group and start receiving."""
        if self._callback is None:
            raise RuntimeError("Callback must be set before calling start()")

        # Create UDP socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except AttributeError:
            pass  # SO_REUSEPORT not available on all platforms

        sock.bind(("", self._port))

        # Join multicast group
        group = socket.inet_aton(self._multicast_addr)
        mreq = struct.pack("4sL", group, socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.setblocking(False)

        self._transport, self._protocol = await loop.create_datagram_endpoint(
            lambda: MulticastProtocol(self._callback),
            sock=sock,
        )

        logger.info(f"Multicast receiver started: {self._multicast_addr}:{self._port}")

    def stop(self) -> None:
        """Leave multicast group and stop receiving."""
        if self._transport:
            self._transport.close()
            self._transport = None
        logger.info(f"Multicast receiver stopped: {self._multicast_addr}:{self._port}")
