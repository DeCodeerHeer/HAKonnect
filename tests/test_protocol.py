"""Protocol tests against an in-process fake desktop (no Home Assistant)."""

from __future__ import annotations

import asyncio

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec

from custom_components.kdeconnect.protocol import node as node_mod
from custom_components.kdeconnect.protocol.certs import generate_identity, verification_key
from custom_components.kdeconnect.protocol.device import Event, PairingError
from custom_components.kdeconnect.protocol.node import KdeConnectNode
from custom_components.kdeconnect.protocol.packets import is_valid_device_id

from .fake_desktop import FakeDesktop

pytestmark = pytest.mark.usefixtures("socket_enabled")


async def _until(pred, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not pred():
            await asyncio.sleep(0.02)


@pytest.fixture
async def desktop(socket_enabled):
    d = FakeDesktop()
    await d.start()
    yield d
    await d.stop()


@pytest.fixture
async def node(desktop):
    n = KdeConnectNode(generate_identity(), "HA Test", listen_udp=False, broadcast_addresses=[], udp_port=desktop.udp_port)
    await n.start()
    yield n
    await n.stop()


def test_identity_matches_kdeconnect_format():
    ident = generate_identity()
    assert is_valid_device_id(ident.device_id)
    cert = x509.load_pem_x509_certificate(ident.cert_pem.encode())
    assert cert.subject.rfc4514_string() == f"OU=KDE Connect,O=KDE,CN={ident.device_id}"
    assert isinstance(cert.public_key(), ec.EllipticCurvePublicKey)
    assert len(cert.extensions) == 0
    assert "PRIVATE" not in repr(ident)


def test_verification_key_is_symmetric():
    a, b = generate_identity(), generate_identity()
    k1 = verification_key(a.cert_der, b.cert_der, 1_700_000_000)
    assert k1 == verification_key(b.cert_der, a.cert_der, 1_700_000_000)
    assert len(k1) == 8 and k1 == k1.upper()


async def test_discover_pair_and_control(node: KdeConnectNode, desktop: FakeDesktop):
    candidates = await node.discover(0.3, hosts=["127.0.0.1"])
    assert [c.device_id for c in candidates] == [desktop.device_id]
    assert desktop.announcements[0].body["tcpPort"] == node.tcp_port

    async with node.discovering():
        dev = await node.wait_for_candidate(desktop.device_id, "127.0.0.1")
        cert = await dev.request_pairing()
    assert cert == desktop.identity.cert_der
    assert abs(desktop.pair_requests[0].body["timestamp"] - __import__("time").time()) < 5

    dev = node.add_device(desktop.device_id, desktop.name, cert, "127.0.0.1")
    await _until(lambda: dev.connected)
    await _until(lambda: "VLC" in dev.players and dev.players["VLC"].title == "Song" and dev.commands and dev.locked is False)
    p = dev.players["VLC"]
    assert (p.is_playing, p.pos, p.length, p.volume) == (True, 12000, 180000, 40)
    assert dev.commands == {"k1": {"name": "Say hi", "command": "echo hi"}}

    dev.media_action("VLC", "PlayPause")
    dev.media_set_position("VLC", 30000)
    dev.run_command("k1")
    dev.set_locked(True)
    nid = dev.notify("Title", "Body", actions=["OK"])
    await _until(lambda: dev.locked is True)
    await _until(lambda: any(p.type == "kdeconnect.notification" for p in desktop.received))
    bodies = {p.type: p.body for p in desktop.received}
    assert {"player": "VLC", "SetPosition": 30000} in [p.body for p in desktop.received]
    assert bodies["kdeconnect.runcommand.request"] == {"key": "k1"}
    assert bodies["kdeconnect.notification"]["id"] == nid
    assert bodies["kdeconnect.notification"]["actions"] == ["OK"]

    actions = []
    dev.add_listener(lambda ev, data: ev is Event.NOTIFICATION_ACTION and actions.append(data))
    desktop.send("kdeconnect.notification.action", {"key": nid, "action": "OK"})
    await _until(lambda: actions)
    assert actions == [{"key": nid, "action": "OK"}]


async def test_reconnects_after_drop(node: KdeConnectNode, desktop: FakeDesktop, monkeypatch):
    monkeypatch.setattr(node_mod, "RECONNECT_MIN", 0.1)
    dev = node.add_device(desktop.device_id, desktop.name, desktop.identity.cert_der, "127.0.0.1")
    node._wake.set()
    await _until(lambda: dev.connected)
    states = []
    dev.add_listener(lambda ev, data: ev is Event.CONNECTION and states.append(data))
    desktop.disconnect()
    await _until(lambda: states[:2] == [False, True])
    assert dev.connected


async def test_pair_rejected(node: KdeConnectNode, desktop: FakeDesktop):
    desktop.accept_pair = False
    async with node.discovering():
        node.announce("127.0.0.1")
        dev = await node.wait_for_candidate(desktop.device_id, "127.0.0.1")
        with pytest.raises(PairingError):
            await dev.request_pairing()
    assert not dev.paired


async def test_rejects_changed_certificate(node: KdeConnectNode, desktop: FakeDesktop):
    other = generate_identity()
    dev = node.add_device(desktop.device_id, desktop.name, other.cert_der, "127.0.0.1")
    node.announce("127.0.0.1")
    await _until(lambda: desktop.announcements)
    await asyncio.sleep(0.5)
    assert not dev.connected


async def test_ignores_unknown_devices_outside_discovery(node: KdeConnectNode, desktop: FakeDesktop):
    node.announce("127.0.0.1")
    await _until(lambda: desktop.announcements)
    await asyncio.sleep(0.5)
    assert not node.candidates and not node.devices


async def test_unpair_from_desktop_emits_event(node: KdeConnectNode, desktop: FakeDesktop):
    dev = node.add_device(desktop.device_id, desktop.name, desktop.identity.cert_der, "127.0.0.1")
    node._wake.set()
    await _until(lambda: dev.connected)
    events = []
    dev.add_listener(lambda ev, data: events.append(ev))
    desktop.send("kdeconnect.pair", {"pair": False})
    await _until(lambda: Event.UNPAIRED in events)
    assert not dev.paired
