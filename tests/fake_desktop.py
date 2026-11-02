"""A minimal in-process KDE Connect desktop for tests.

It behaves like kdeconnectd's LanLinkProvider on the receiving end: on a UDP
announcement it dials our tcpPort, sends a plaintext identity, takes the TLS
*server* role, and exchanges full identities inside TLS.
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import tempfile

from custom_components.kdeconnect.protocol.certs import generate_identity
from custom_components.kdeconnect.protocol.link import LinkProtocol
from custom_components.kdeconnect.protocol.packets import Packet, full_identity_packet


def _server_context(identity) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.verify_mode = ssl.CERT_NONE
    fd, path = tempfile.mkstemp()
    try:
        os.write(fd, (identity.cert_pem + identity.key_pem).encode())
        os.close(fd)
        ctx.load_cert_chain(path)
    finally:
        os.unlink(path)
    return ctx


class _Udp(asyncio.DatagramProtocol):
    def __init__(self, desktop: FakeDesktop) -> None:
        self.desktop = desktop

    def datagram_received(self, data: bytes, addr) -> None:
        packet = Packet.parse(data)
        self.desktop.announcements.append(packet)
        asyncio.get_running_loop().create_task(self.desktop._connect(addr[0], packet))


class FakeDesktop:
    def __init__(self, name: str = "Fake Desktop", accept_pair: bool = True) -> None:
        self.identity = generate_identity()
        self.name = name
        self.accept_pair = accept_pair
        self.announcements: list[Packet] = []
        self.received: list[Packet] = []
        self.pair_requests: list[Packet] = []
        self.locked = False
        self.connected = asyncio.Event()
        self.udp_port = 0
        self._protocol: LinkProtocol | None = None
        self._udp: asyncio.DatagramTransport | None = None
        self._tasks: set[asyncio.Task] = set()
        self._ctx = _server_context(self.identity)

    @property
    def device_id(self) -> str:
        return self.identity.device_id

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._udp, _ = await loop.create_datagram_endpoint(lambda: _Udp(self), local_addr=("127.0.0.1", 0))
        self.udp_port = self._udp.get_extra_info("sockname")[1]

    async def stop(self) -> None:
        if self._udp:
            self._udp.close()
        self.disconnect()
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def disconnect(self) -> None:
        if self._protocol and self._protocol.transport:
            self._protocol.transport.close()
        self.connected.clear()

    def send(self, type_: str, body: dict) -> None:
        assert self._protocol and self._protocol.transport
        self._protocol.transport.write(Packet(type_, body).serialize())

    async def _connect(self, host: str, announce: Packet) -> None:
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            await self._run_link(host, announce)
        finally:
            self._tasks.discard(task)

    async def _run_link(self, host: str, announce: Packet) -> None:
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_connection(LinkProtocol, host, announce.body["tcpPort"])
        transport.write(
            Packet(
                "kdeconnect.identity",
                {
                    "deviceId": self.device_id,
                    "deviceName": self.name,
                    "protocolVersion": 8,
                    "targetDeviceId": announce.body["deviceId"],
                    "targetProtocolVersion": 8,
                },
            ).serialize()
        )
        tls = await loop.start_tls(transport, protocol, self._ctx, server_side=True)
        protocol.transport = tls
        tls.write(full_identity_packet(self.device_id, self.name, "desktop").serialize())
        peer = await protocol.read(5)
        assert peer is not None and peer.type == "kdeconnect.identity"
        protocol.set_limit(1 << 20)
        self.disconnect()
        self._protocol = protocol
        self.connected.set()
        while (packet := await protocol.read()) is not None:
            self.received.append(packet)
            self._respond(packet)
        if self._protocol is protocol:
            self.connected.clear()

    def _respond(self, p: Packet) -> None:
        b = p.body
        if p.type == "kdeconnect.pair":
            if b.get("pair") and "timestamp" in b:
                self.pair_requests.append(p)
                self.send("kdeconnect.pair", {"pair": self.accept_pair})
        elif p.type == "kdeconnect.mpris.request":
            if b.get("requestPlayerList"):
                self.send("kdeconnect.mpris", {"playerList": ["VLC"], "supportAlbumArtPayload": True})
            if b.get("requestNowPlaying"):
                self.send(
                    "kdeconnect.mpris",
                    {
                        "player": b["player"],
                        "title": "Song",
                        "artist": "Artist",
                        "album": "Album",
                        "albumArtUrl": "file:///tmp/art.png",
                        "isPlaying": True,
                        "pos": 12000,
                        "length": 180000,
                        "volume": 40,
                        "canPlay": True,
                        "canPause": True,
                        "canGoNext": True,
                        "canGoPrevious": True,
                        "canSeek": True,
                    },
                )
        elif p.type == "kdeconnect.runcommand.request" and b.get("requestCommandList"):
            self.send(
                "kdeconnect.runcommand",
                {"commandList": json.dumps({"k1": {"name": "Say hi", "command": "echo hi"}}), "canAddCommand": True},
            )
        elif p.type == "kdeconnect.lock.request":
            if "setLocked" in b:
                self.locked = bool(b["setLocked"])
            self.send("kdeconnect.lock", {"isLocked": self.locked})
