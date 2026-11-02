"""A single TLS link to a peer, including the v8 handshake.

We only ever accept TCP connections (the peer connects after receiving our UDP
announcement), which makes us the TLS *client* (roles are swapped in KDE
Connect). That's deliberate: Python's ssl module can't request a client
certificate without also validating it, so the TLS-server role can't pin an
unknown self-signed peer. As TLS client we always receive the peer's cert.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import ssl
import sys
import tempfile

from .certs import LocalIdentity, cert_common_name
from .const import (
    MAX_IDENTITY_PACKET_SIZE,
    MAX_PACKET_SIZE,
    PROTOCOL_VERSION,
    TYPE_IDENTITY,
)
from .packets import Identity, Packet, PacketError, is_valid_device_id

_LOGGER = logging.getLogger(__name__)

HANDSHAKE_TIMEOUT = 10.0


class HandshakeError(Exception):
    """The peer didn't complete a valid v8 handshake."""


def build_client_context(identity: LocalIdentity) -> ssl.SSLContext:
    """Build the TLS-client context. Blocking: run in an executor.

    ssl can only load key material from a path, so on Linux we hand it an
    anonymous memfd; elsewhere a 0600 temp file that's removed immediately.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # self-signed; pinned after the handshake
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    blob = (identity.cert_pem + "\n" + identity.key_pem).encode()

    if sys.platform.startswith("linux") and hasattr(os, "memfd_create"):
        fd = os.memfd_create("kdeconnect-identity", os.MFD_CLOEXEC)
        try:
            os.write(fd, blob)
            ctx.load_cert_chain(f"/proc/self/fd/{fd}")
        finally:
            os.close(fd)
    else:
        fd, path = tempfile.mkstemp(prefix="kdeconnect-")
        try:
            os.write(fd, blob)
            os.close(fd)
            ctx.load_cert_chain(path)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(path)
    return ctx


class LinkProtocol(asyncio.Protocol):
    """Newline framing over a transport that gets upgraded to TLS in place."""

    def __init__(self) -> None:
        self.transport: asyncio.Transport | None = None
        self._buffer = bytearray()
        self._limit = MAX_IDENTITY_PACKET_SIZE
        self._queue: asyncio.Queue[Packet | None] = asyncio.Queue()
        self.closed = asyncio.get_running_loop().create_future()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.Transport)
        self.transport = transport

    def data_received(self, data: bytes) -> None:
        self._buffer += data
        while (idx := self._buffer.find(b"\n")) != -1:
            line = bytes(self._buffer[:idx])
            del self._buffer[: idx + 1]
            if not line.strip():
                continue
            try:
                self._queue.put_nowait(Packet.parse(line))
            except PacketError as err:
                _LOGGER.debug("Dropping malformed packet: %s", err)
        if len(self._buffer) > self._limit:
            _LOGGER.warning("Peer sent an oversized packet, closing link")
            self._buffer.clear()
            if self.transport:
                self.transport.abort()

    def connection_lost(self, exc: Exception | None) -> None:
        self._queue.put_nowait(None)
        if not self.closed.done():
            self.closed.set_result(None)

    def set_limit(self, limit: int) -> None:
        self._limit = limit

    async def read(self, timeout: float | None = None) -> Packet | None:
        """Next packet, or None on EOF."""
        if timeout is None:
            return await self._queue.get()
        return await asyncio.wait_for(self._queue.get(), timeout)


class Link:
    """An established, authenticated-by-certificate link to one peer."""

    def __init__(
        self,
        protocol: LinkProtocol,
        identity: Identity,
        peer_cert_der: bytes,
        address: str,
    ) -> None:
        self._protocol = protocol
        self.identity = identity
        self.peer_cert_der = peer_cert_der
        self.address = address

    @property
    def device_id(self) -> str:
        return self.identity.device_id

    @property
    def is_closed(self) -> bool:
        t = self._protocol.transport
        return t is None or t.is_closing()

    def send(self, packet: Packet) -> bool:
        transport = self._protocol.transport
        if transport is None or transport.is_closing():
            return False
        _LOGGER.debug("-> %s %s", self.device_id, packet.type)
        transport.write(packet.serialize())
        return True

    async def read(self) -> Packet | None:
        return await self._protocol.read()

    def close(self) -> None:
        if self._protocol.transport:
            self._protocol.transport.close()

    async def wait_closed(self) -> None:
        await self._protocol.closed


def _configure_keepalive(transport: asyncio.BaseTransport) -> None:
    """Detect dead peers in ~60s, same as lanlinkprovider.cpp configureSocket."""
    sock: socket.socket | None = transport.get_extra_info("socket")
    if sock is None:
        return
    with contextlib.suppress(OSError, AttributeError):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, 60000)


async def accept_handshake(
    protocol: LinkProtocol,
    ssl_context: ssl.SSLContext,
    local_id: str,
    identity_packet: Packet,
) -> Link:
    """Run the v8 handshake on a freshly accepted TCP connection.

    1. Peer sends a plaintext identity (with targetDeviceId).
    2. We start TLS as the *client*.
    3. Both send their full identity inside TLS; that copy is authoritative.
    """
    loop = asyncio.get_running_loop()
    transport = protocol.transport
    assert transport is not None
    address = (transport.get_extra_info("peername") or ("?",))[0]
    _configure_keepalive(transport)

    first = await protocol.read(HANDSHAKE_TIMEOUT)
    if first is None or first.type != TYPE_IDENTITY:
        raise HandshakeError("no plaintext identity")
    device_id = first.body.get("deviceId")
    if not is_valid_device_id(device_id):
        raise HandshakeError("invalid deviceId")
    target = first.body.get("targetDeviceId")
    if target is not None and target != local_id:
        raise HandshakeError("connection is meant for another device")
    version = int(first.body.get("protocolVersion") or 0)
    if version < PROTOCOL_VERSION:
        raise HandshakeError(f"peer protocol version {version} is too old")

    tls_transport = await loop.start_tls(
        transport,
        protocol,
        ssl_context,
        server_side=False,
        ssl_handshake_timeout=HANDSHAKE_TIMEOUT,
    )
    if tls_transport is None:
        raise HandshakeError("TLS upgrade failed")
    protocol.transport = tls_transport  # type: ignore[assignment]

    ssl_obj: ssl.SSLObject = tls_transport.get_extra_info("ssl_object")
    peer_der = ssl_obj.getpeercert(binary_form=True)
    if not peer_der:
        raise HandshakeError("peer presented no certificate")

    tls_transport.write(identity_packet.serialize())

    secure = await protocol.read(HANDSHAKE_TIMEOUT)
    if secure is None:
        raise HandshakeError("peer closed before sending its encrypted identity")
    try:
        identity = Identity.from_packet(secure)
    except PacketError as err:
        raise HandshakeError(str(err)) from err
    if identity.device_id != device_id:
        raise HandshakeError("deviceId changed during the handshake")
    if identity.protocol_version != version:
        raise HandshakeError("protocolVersion changed during the handshake")
    if cert_common_name(peer_der) != device_id:
        raise HandshakeError("certificate CN doesn't match deviceId")

    protocol.set_limit(MAX_PACKET_SIZE)
    return Link(protocol, identity, peer_der, address)
