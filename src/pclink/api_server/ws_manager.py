# src/pclink/api_server/ws_manager.py
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025 AZHAR ZOUHIR / BYTEDz

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from fastapi import WebSocket

log = logging.getLogger(__name__)

# A single slow client must never stall the broadcaster, so every socket owns a
# bounded outbox drained by exactly one writer task.
SEND_TIMEOUT_SECONDS = 5.0
MAX_QUEUED_FRAMES = 128


@dataclass
class _Connection:
    """One accepted socket plus its outbox and dedicated writer task."""

    websocket: WebSocket
    device_id: Optional[str]
    loop: asyncio.AbstractEventLoop
    queue: "asyncio.Queue[Optional[Dict[str, Any]]]"
    writer: Optional[asyncio.Task] = None
    dropped: int = 0
    closed: bool = False


class ConnectionManager:
    def __init__(self):
        # Kept as plain containers for existing call sites that only test
        # membership or truthiness. The authoritative registry is _connections.
        self.active_connections: List[WebSocket] = []
        # Support multiple connections per device (Mobile app + Extensions)
        self.device_connections: Dict[str, List[WebSocket]] = {}

        self._connections: Dict[int, _Connection] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        self.dropped_frames = 0
        self.failed_writes = 0

    async def connect(self, websocket: WebSocket, device_id: Optional[str] = None):
        loop = asyncio.get_running_loop()
        self._loop = loop
        try:
            await websocket.accept()
        except RuntimeError:
            pass  # already accepted

        entry = _Connection(
            websocket=websocket,
            device_id=device_id,
            loop=loop,
            queue=asyncio.Queue(maxsize=MAX_QUEUED_FRAMES),
        )
        self._connections[id(websocket)] = entry
        self.active_connections.append(websocket)
        if device_id:
            self.device_connections.setdefault(device_id, []).append(websocket)

        entry.writer = loop.create_task(
            self._writer_loop(entry),
            name=f"pclink-ws-writer-{device_id or 'web-ui'}",
        )

    async def disconnect(self, websocket: WebSocket) -> None:
        """Stop the writer and drop all bookkeeping for a socket."""
        entry = self._connections.get(id(websocket))
        if entry is None:
            self._purge(websocket)
            return

        entry.closed = True
        writer = entry.writer
        if writer is not None and not writer.done():
            writer.cancel()
            # A cancelled writer must never propagate cancellation here.
            await asyncio.gather(writer, return_exceptions=True)
        self._detach(entry)

    async def disconnect_device(self, device_id: str) -> None:
        """Forcefully disconnect all paths for a specific device."""
        sockets = list(self.device_connections.get(device_id, []))
        entries = [self._connections.get(id(s)) for s in sockets]
        active_entries = [e for e in entries if e is not None]

        self.device_connections.pop(device_id, None)

        await asyncio.gather(
            *(
                self._close_entry(entry, code=4003, reason="Device revoked")
                for entry in active_entries
            ),
            return_exceptions=True,
        )

    async def send(self, websocket: WebSocket, message: Dict[str, Any]) -> bool:
        """Queue a frame for one specific socket.

        All writes must go through the outbox so the socket keeps exactly one
        writer; returns False when the socket is unknown or already closed.
        """
        entry = self._connections.get(id(websocket))
        if entry is None:
            return False
        self._enqueue(entry, message)
        return True

    async def send_to_device(self, device_id: str, message: Dict[str, Any]) -> int:
        """Queue a message for every socket of a device.

        Returns the number of sockets the frame was queued for. Delivery
        failures are handled by the owning writer (which logs and disconnects),
        so nothing is silently swallowed here.
        """
        sockets = self.device_connections.get(device_id)
        if not sockets:
            return 0

        queued = 0
        for socket in list(sockets):
            entry = self._connections.get(id(socket))
            if entry is None:
                continue
            self._enqueue(entry, message)
            queued += 1
        return queued

    async def broadcast(self, message: Dict[str, Any]) -> None:
        """Queue a message for every connected socket. Never blocks on I/O."""
        self._broadcast_nowait(message)

    def broadcast_threadsafe(self, message: Dict[str, Any]) -> None:
        """Dispatches broadcast safely from worker threads or sync contexts."""
        loop = self._loop
        if loop is None or loop.is_closed() or not self._connections:
            return
        try:
            loop.call_soon_threadsafe(self._broadcast_nowait, message)
        except RuntimeError:
            # Loop torn down between the check and the call.
            log.debug("Dropping WebSocket broadcast: event loop is closed")

    def stats(self) -> Dict[str, Any]:
        return {
            "connections": len(self._connections),
            "devices": len(self.device_connections),
            "dropped_frames": self.dropped_frames,
            "failed_writes": self.failed_writes,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _broadcast_nowait(self, message: Dict[str, Any]) -> None:
        for entry in list(self._connections.values()):
            self._enqueue(entry, message)

    def _enqueue(self, entry: _Connection, message: Dict[str, Any]) -> None:
        """Drop-oldest enqueue: a stalled client cannot grow memory unbounded."""
        if entry.closed:
            return
        try:
            entry.queue.put_nowait(message)
            return
        except asyncio.QueueFull:
            pass

        try:
            entry.queue.get_nowait()
            self.dropped_frames += 1
            entry.dropped += 1
        except asyncio.QueueEmpty:
            pass

        try:
            entry.queue.put_nowait(message)
        except asyncio.QueueFull:
            self.dropped_frames += 1

    async def _writer_loop(self, entry: _Connection) -> None:
        """The only task that ever writes to this socket."""
        try:
            while True:
                message = await entry.queue.get()
                if message is None:
                    return

                try:
                    await asyncio.wait_for(
                        entry.websocket.send_json(message),
                        timeout=SEND_TIMEOUT_SECONDS,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.failed_writes += 1
                    log.info(
                        "Closing WebSocket for %s after write failure: %s",
                        entry.device_id or "web-ui",
                        exc,
                    )
                    await self._abort(entry)
                    return
        finally:
            self._detach(entry)

    async def _abort(self, entry: _Connection) -> None:
        """Best-effort close so a half-open receive loop wakes up and unwinds."""
        entry.closed = True
        try:
            await asyncio.wait_for(
                entry.websocket.close(code=1011, reason="WRITE_FAILED"),
                timeout=1.0,
            )
        except Exception:
            pass

    async def _close_entry(
        self, entry: _Connection, code: int = 1000, reason: str = ""
    ) -> None:
        entry.closed = True
        writer = entry.writer
        if writer is not None and not writer.done():
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)

        try:
            await asyncio.wait_for(
                entry.websocket.close(code=code, reason=reason),
                timeout=SEND_TIMEOUT_SECONDS,
            )
        except Exception:
            pass

        self._detach(entry)

    def _detach(self, entry: _Connection) -> None:
        entry.closed = True
        self._connections.pop(id(entry.websocket), None)
        self._purge(entry.websocket)

    def _purge(self, websocket: WebSocket) -> None:
        try:
            self.active_connections.remove(websocket)
        except ValueError:
            pass

        for dev_id, sockets in list(self.device_connections.items()):
            if websocket in sockets:
                try:
                    sockets.remove(websocket)
                except ValueError:
                    pass
                if not sockets:
                    self.device_connections.pop(dev_id, None)
                break


# Global singleton managers
mobile_manager = ConnectionManager()
ui_manager = ConnectionManager()
