# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""Dual-port UDP multicast receiver with automatic port switching."""

import asyncio
import logging
import time
from typing import Callable, List, Optional

from .multicast_receiver import MulticastReceiver

logger = logging.getLogger(__name__)

# A port is considered inactive if no packet has been received within this duration
_PORT_TIMEOUT_SEC = 3.0


class DualPortReceiver:
    """
    Listens on two UDP multicast ports simultaneously.

    Auto-switches to the active port when only one port receives packets.
    When both ports are active, keeps the current selection.
    Manual switching is supported via switch_port().
    """

    def __init__(self, multicast_addr: str, ports: List[int]):
        if len(ports) != 2:
            raise ValueError("DualPortReceiver requires exactly 2 ports")
        self._addr = multicast_addr
        self._ports = ports
        self._callback: Optional[Callable[[bytes], None]] = None
        self._receivers = [
            MulticastReceiver(multicast_addr, ports[0]),
            MulticastReceiver(multicast_addr, ports[1]),
        ]
        self._last_seen = [0.0, 0.0]
        self._active_index = 0  # Which port index is currently active

        self._receivers[0].set_callback(lambda data: self._on_data(0, data))
        self._receivers[1].set_callback(lambda data: self._on_data(1, data))

    def set_callback(self, callback: Callable[[bytes], None]) -> None:
        """Set callback invoked with raw UDP payload from the active port."""
        self._callback = callback

    async def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start listening on both ports."""
        await self._receivers[0].start(loop)
        await self._receivers[1].start(loop)
        logger.info(
            f"DualPortReceiver started: {self._addr} ports={self._ports}, "
            f"active={self._ports[self._active_index]}"
        )

    def stop(self) -> None:
        """Stop listening on both ports."""
        for r in self._receivers:
            r.stop()

    @property
    def active_port(self) -> int:
        """The currently active port number."""
        return self._ports[self._active_index]

    def switch_port(self, port: int) -> bool:
        """
        Manually switch to the given port.

        Returns True if the port was valid and switched.
        """
        if port not in self._ports:
            return False
        idx = self._ports.index(port)
        if idx != self._active_index:
            self._active_index = idx
            logger.info(f"DualPortReceiver ({self._addr}): manually switched to port {port}")
        return True

    def get_port_status(self) -> dict:
        """Return status of each port and the currently active port."""
        now = time.time()
        return {
            "active": self._ports[self._active_index],
            "ports": [
                {
                    "port": self._ports[i],
                    "receiving": (now - self._last_seen[i]) < _PORT_TIMEOUT_SEC,
                }
                for i in range(2)
            ],
        }

    def _on_data(self, index: int, data: bytes) -> None:
        """Handle incoming data from port at the given index."""
        now = time.time()
        prev_seen = self._last_seen[index]
        self._last_seen[index] = now

        self._maybe_auto_switch(index, prev_seen, now)

        if index == self._active_index and self._callback is not None:
            self._callback(data)

    def _maybe_auto_switch(self, recv_index: int, prev_seen: float, now: float) -> None:
        """Auto-switch to a port if it's the only one receiving data."""
        other_index = 1 - recv_index
        other_active = (now - self._last_seen[other_index]) < _PORT_TIMEOUT_SEC

        if other_active:
            # Both ports are receiving — keep current selection
            return

        # Only this port is receiving — switch to it if not already active
        if self._active_index != recv_index:
            was_ever_seen = prev_seen > 0.0
            if not was_ever_seen:
                # First packet ever on this port — auto-select silently
                self._active_index = recv_index
                logger.info(
                    f"DualPortReceiver ({self._addr}): auto-selected port "
                    f"{self._ports[recv_index]} (initial)"
                )
            else:
                self._active_index = recv_index
                logger.info(
                    f"DualPortReceiver ({self._addr}): auto-switched to port "
                    f"{self._ports[recv_index]} (other port inactive)"
                )
