"""The local KDE Connect node: one per process, shared by all paired peers.

Connection model: we announce ourselves over UDP (unicast to known hosts,
broadcast while discovering) and the peer opens a TCP connection to our
tcpPort. See link.py for why we never dial out.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import ssl
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .certs import LocalIdentity, fingerprint
from .const import MAX_TCP_PORT, MIN_TCP_PORT, PROTOCOL_VERSION, UDP_PORT
from .device import RemoteDevice
from .link import HandshakeError, Link, LinkProtocol, accept_handshake, build_client_context
from .packets import Identity, Packet, PacketError, full_identity_packet, is_valid_device_id, udp_discovery_packet

_LOGGER = logging.getLogger(__name__)

RECONNECT_MIN = 5.0
RECONNECT_MAX = 120.0


@dataclass(slots=True, frozen=True)
class Candidate:
    """An unpaired peer that connected to us during discovery."""

    device_id: str
    name: str
    device_type: str
    host: str


class _UdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, node: KdeConnectNode) -> None:
        self._node = node

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._node._udp_received(data, addr[0])


class KdeConnectNode:
    def __init__(
        self,
        identity: LocalIdentity,
        name: str,
        device_type: str = "desktop",
        *,
        listen_udp: bool = True,
        broadcast_addresses: Iterable[str] = ("255.255.255.255",),
        udp_port: int = UDP_PORT,
    ) -> None:
        self.identity = identity
        self.name = name
        self.device_type = device_type
        self.listen_udp = listen_udp
        self.broadcast_addresses = list(broadcast_addresses)
        self.udp_port = udp_port
        self.tcp_port = 0
        self.devices: dict[str, RemoteDevice] = {}
        self.candidates: dict[str, RemoteDevice] = {}
        self._discovering = 0
        self._candidate_listeners: list[Callable[[Candidate], None]] = []
        self._ssl: ssl.SSLContext | None = None
        self._server: asyncio.Server | None = None
        self._udp_listener: asyncio.DatagramTransport | None = None
        self._udp_sender: asyncio.DatagramTransport | None = None
        self._tasks: set[asyncio.Task] = set()
        self._reconnect_task: asyncio.Task | None = None
        self._wake = asyncio.Event()

    @property
    def device_id(self) -> str:
        return self.identity.device_id

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._ssl = await loop.run_in_executor(None, build_client_context, self.identity)

        for port in range(MIN_TCP_PORT, MAX_TCP_PORT + 1):
            try:
                self._server = await loop.create_server(self._make_protocol, host=None, port=port, reuse_address=True)
            except OSError:
                continue
            self.tcp_port = port
            break
        else:
            raise OSError(f"no free TCP port in {MIN_TCP_PORT}-{MAX_TCP_PORT}")

        self._udp_sender, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, local_addr=("0.0.0.0", 0), allow_broadcast=True
        )
        if self.listen_udp:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("0.0.0.0", self.udp_port))
                self._udp_listener, _ = await loop.create_datagram_endpoint(lambda: _UdpProtocol(self), sock=sock)
            except OSError as err:
                _LOGGER.warning("Can't listen on UDP %s (%s); relying on outbound announcements only", self.udp_port, err)

        self._reconnect_task = loop.create_task(self._reconnect_loop())
        _LOGGER.debug("KDE Connect node %s listening on TCP %s", self.device_id, self.tcp_port)

    async def stop(self) -> None:
        if self._reconnect_task:
            self._reconnect_task.cancel()
        if self._server:
            self._server.close()
        for transport in (self._udp_listener, self._udp_sender):
            if transport:
                transport.close()
        for dev in [*self.devices.values(), *self.candidates.values()]:
            if dev.link:
                dev.link.close()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2)

    # -- registry ----------------------------------------------------------

    def add_device(self, device_id: str, name: str, cert_der: bytes, host: str | None) -> RemoteDevice:
        """Register a paired peer; we keep announcing to it until it connects."""
        dev = self.candidates.pop(device_id, None)
        if dev is None:
            dev = RemoteDevice(device_id, name, cert_der, host)
        else:
            dev.cert_der = cert_der
            if dev.link and dev.link.peer_cert_der != cert_der:
                dev.link.close()
            elif dev.connected:
                dev.request_state()  # link from pairing stays up; fetch state now
        self.devices[device_id] = dev
        self._wake.set()
        return dev

    def remove_device(self, device_id: str) -> None:
        dev = self.devices.pop(device_id, None)
        if dev and dev.link:
            dev.link.close()

    # -- discovery ---------------------------------------------------------

    def add_candidate_listener(self, cb: Callable[[Candidate], None]) -> Callable[[], None]:
        self._candidate_listeners.append(cb)
        return lambda: self._candidate_listeners.remove(cb)

    def begin_discovery(self) -> Callable[[], None]:
        """Keep unknown peers that connect as candidates until released."""
        self._discovering += 1
        released = False

        def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            self._discovering -= 1
            if not self._discovering:
                for dev_id, dev in list(self.candidates.items()):
                    if dev.link:
                        dev.link.close()
                    del self.candidates[dev_id]

        return release

    @contextlib.asynccontextmanager
    async def discovering(self):
        release = self.begin_discovery()
        try:
            yield
        finally:
            release()

    async def discover(self, timeout: float = 5.0, hosts: Iterable[str] = ()) -> list[Candidate]:
        """Broadcast (and unicast to hosts) and collect peers that connect back."""
        async with self.discovering():
            hosts = list(hosts)
            for _ in range(3):
                self.announce(*hosts, broadcast=True)
                await asyncio.sleep(timeout / 3)
            return [self._as_candidate(d) for d in self.candidates.values() if d.connected]

    async def wait_for_candidate(self, device_id: str, host: str | None, timeout: float = 10.0) -> RemoteDevice:
        """Announce to one host until a specific peer connects (manual setup)."""
        async with self.discovering():
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while loop.time() < deadline:
                dev = self.candidates.get(device_id) or self.devices.get(device_id)
                if dev and dev.connected:
                    return dev
                if host:
                    self.announce(host)
                else:
                    self.announce(broadcast=True)
                await asyncio.sleep(1.0)
        raise TimeoutError(device_id)

    @staticmethod
    def _as_candidate(dev: RemoteDevice) -> Candidate:
        assert dev.link is not None
        return Candidate(dev.device_id, dev.name, dev.link.identity.device_type, dev.link.address)

    def announce(self, *hosts: str, broadcast: bool = False) -> None:
        if self._udp_sender is None:
            return
        payload = udp_discovery_packet(self.device_id, self.name, self.tcp_port).serialize()
        targets = list(hosts) + (self.broadcast_addresses if broadcast else [])
        for host in targets:
            try:
                self._udp_sender.sendto(payload, (host, self.udp_port))
            except OSError as err:
                _LOGGER.debug("UDP announce to %s failed: %s", host, err)

    def _udp_received(self, data: bytes, sender: str) -> None:
        try:
            packet = Packet.parse(data)
        except PacketError:
            return
        device_id = packet.body.get("deviceId")
        if not is_valid_device_id(device_id) or device_id == self.device_id:
            return
        if int(packet.body.get("protocolVersion") or 0) < PROTOCOL_VERSION:
            return
        dev = self.devices.get(device_id)
        if (dev and not dev.connected) or (self._discovering and device_id not in self.candidates):
            _LOGGER.debug("Heard %s at %s, announcing back", device_id, sender)
            if dev:
                dev.host = sender
            self.announce(sender)

    # -- connections -------------------------------------------------------

    def _make_protocol(self) -> LinkProtocol:
        protocol = LinkProtocol()
        loop = asyncio.get_running_loop()
        # connection_made has run by the time this task starts
        task = loop.create_task(self._handle_inbound(protocol))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return protocol

    async def _handle_inbound(self, protocol: LinkProtocol) -> None:
        await asyncio.sleep(0)
        assert self._ssl is not None
        try:
            link = await accept_handshake(
                protocol,
                self._ssl,
                self.device_id,
                full_identity_packet(self.device_id, self.name, self.device_type),
            )
        except (HandshakeError, TimeoutError, ssl.SSLError, OSError, ConnectionError) as err:
            _LOGGER.debug("Inbound handshake failed: %s", err)
            if protocol.transport:
                protocol.transport.abort()
            return

        dev = self._route(link)
        if dev is None:
            link.close()
            return
        dev.attach(link)
        if dev.device_id in self.candidates:
            for cb in list(self._candidate_listeners):
                cb(self._as_candidate(dev))
        try:
            while (packet := await link.read()) is not None:
                dev.handle_packet(packet)
        finally:
            link.close()
            dev.detach(link)
            self._wake.set()
            _LOGGER.debug("Link to %s closed", link.device_id)

    def _route(self, link: Link) -> RemoteDevice | None:
        device_id = link.device_id
        dev = self.devices.get(device_id)
        if dev is not None:
            # cert_der is None after the peer unpaired us; accept so we can re-pair.
            if dev.cert_der is not None and dev.cert_der != link.peer_cert_der:
                _LOGGER.warning(
                    "Rejecting %s: certificate changed (expected %s, got %s); re-pair the device",
                    device_id,
                    fingerprint(dev.cert_der or b"")[:16],
                    fingerprint(link.peer_cert_der)[:16],
                )
                return None
            return dev
        if not self._discovering and device_id not in self.candidates:
            _LOGGER.debug("Ignoring unpaired device %s", device_id)
            return None
        dev = self.candidates.get(device_id)
        if dev is None:
            dev = self.candidates[device_id] = RemoteDevice(device_id, link.identity.name, None, link.address)
        return dev

    async def _reconnect_loop(self) -> None:
        """Announce to paired-but-disconnected peers with exponential backoff."""
        delay = RECONNECT_MIN
        while True:
            pending = [d for d in self.devices.values() if not d.connected]
            if pending:
                for dev in pending:
                    if dev.host:
                        self.announce(dev.host)
                if any(not d.host for d in pending):
                    self.announce(broadcast=True)
                delay = min(delay * 2, RECONNECT_MAX)
            else:
                delay = RECONNECT_MIN
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), delay if pending else None)
            if self._wake.is_set():
                delay = RECONNECT_MIN


__all__ = ["Candidate", "Identity", "KdeConnectNode", "RemoteDevice"]
