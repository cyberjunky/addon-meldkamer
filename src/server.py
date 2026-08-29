"""TCP server for integration communication."""

import asyncio
import contextlib
import json
import logging

from .message import P2000Message

logger = logging.getLogger(__name__)


class P2000Server:
    """TCP server that broadcasts P2000 messages to connected clients."""

    def __init__(self, host: str = "0.0.0.0", port: int = 5000):
        self.host = host
        self.port = port
        self._server: asyncio.Server = None
        self._clients: set[asyncio.StreamWriter] = set()
        self._running = False
        # Keep references to in-flight broadcast tasks so they aren't
        # garbage-collected mid-send; each removes itself once done.
        self._background_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        """Start the TCP server."""
        self._running = True
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)

        addr = self._server.sockets[0].getsockname()
        logger.info(f"TCP server listening on {addr[0]}:{addr[1]}")

        async with self._server:
            await self._server.serve_forever()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handle a client connection."""
        addr = writer.get_extra_info("peername")
        logger.info(f"Client connected: {addr}")
        self._clients.add(writer)

        try:
            # Send welcome message
            welcome = {"type": "connected", "version": "2.0.0"}
            await self._send(writer, welcome)

            # Keep connection alive
            while self._running:
                try:
                    data = await asyncio.wait_for(reader.read(1024), timeout=30.0)
                    if not data:
                        break
                    # Handle any client commands here if needed
                except TimeoutError:
                    # Send ping to keep alive
                    await self._send(writer, {"type": "ping"})
        except ConnectionError:
            pass
        finally:
            self._clients.discard(writer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            logger.info(f"Client disconnected: {addr}")

    async def _send(self, writer: asyncio.StreamWriter, data: dict) -> None:
        """Send JSON data to a client."""
        try:
            message = json.dumps(data) + "\n"
            writer.write(message.encode())
            await writer.drain()
        except Exception as e:
            logger.debug(f"Send error: {e}")

    def broadcast_message(self, msg: P2000Message) -> None:
        """Broadcast a P2000 message to all connected clients."""
        if not self._clients:
            return

        data = {"type": "message", "data": msg.to_dict()}

        # Schedule broadcast on event loop
        task = asyncio.create_task(self._broadcast(data))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _broadcast(self, data: dict) -> None:
        """Send to all connected clients."""
        if not self._clients:
            return

        message = json.dumps(data) + "\n"
        encoded = message.encode()

        disconnected = set()
        for writer in list(self._clients):
            try:
                writer.write(encoded)
                # Bound the drain so a slow/dead client cannot block others
                await asyncio.wait_for(writer.drain(), timeout=5.0)
            except Exception:
                disconnected.add(writer)

        # Clean up disconnected (or too slow) clients
        for writer in disconnected:
            self._clients.discard(writer)
            writer.close()

    async def stop(self) -> None:
        """Stop the TCP server."""
        self._running = False

        # Close all clients
        for writer in list(self._clients):
            writer.close()

        if self._server:
            self._server.close()
            await self._server.wait_closed()

        logger.info("TCP server stopped")
