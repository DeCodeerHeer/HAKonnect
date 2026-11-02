"""Home Assistant tests: config flow, entities, services, reauth."""

from __future__ import annotations

import asyncio
from functools import partial
from ipaddress import IPv4Address
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from custom_components.kdeconnect import config_flow
from custom_components.kdeconnect.const import CONF_DEVICE_ID, DOMAIN
from custom_components.kdeconnect.protocol import node as node_mod
from custom_components.kdeconnect.protocol.device import Event
from custom_components.kdeconnect.protocol.node import KdeConnectNode

from .fake_desktop import FakeDesktop

pytestmark = pytest.mark.usefixtures("socket_enabled")


async def _until(pred, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not pred():
            await asyncio.sleep(0.02)


@pytest.fixture
async def desktop(socket_enabled):
    d = FakeDesktop(name="Fake Desktop")
    await d.start()
    yield d
    await d.stop()


@pytest.fixture(autouse=True)
def patch_node(desktop):
    with (
        patch(
            "custom_components.kdeconnect.hub.KdeConnectNode",
            partial(KdeConnectNode, listen_udp=False, udp_port=desktop.udp_port),
        ),
        patch(
            "custom_components.kdeconnect.hub.network.async_get_ipv4_broadcast_addresses",
            return_value={IPv4Address("127.0.0.1")},
        ),
        patch.object(config_flow, "DISCOVERY_TIMEOUT", 0.3),
        patch.object(node_mod, "RECONNECT_MIN", 0.1),
    ):
        yield


async def _pair(hass: HomeAssistant, desktop: FakeDesktop, flow_id: str):
    result = await hass.config_entries.flow.async_configure(flow_id, {"host": "127.0.0.1"})
    while result["type"] is FlowResultType.SHOW_PROGRESS:
        if result["step_id"] == "pair":
            assert len(result["description_placeholders"]["code"]) == 8
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(flow_id)
    return result


async def test_user_flow_pairs_and_sets_up(hass: HomeAssistant, desktop: FakeDesktop):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await _pair(hass, desktop, result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert result["title"] == "Fake Desktop"
    assert result["data"][CONF_DEVICE_ID] == desktop.device_id
    assert "PRIVATE KEY" not in str(result["data"])
    assert len(desktop.pair_requests) == 1

    entry = result["result"]
    await hass.async_block_till_done()
    dev = entry.runtime_data
    await _until(lambda: dev.connected and dev.commands and "VLC" in dev.players and dev.players["VLC"].title)
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    ids = {e.unique_id: e.entity_id for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)}
    connected = ids[f"{desktop.device_id}_connected"]
    media = ids[f"{desktop.device_id}_media"]
    lock = ids[f"{desktop.device_id}_screen_lock"]
    cmd = ids[f"{desktop.device_id}_command_k1"]

    assert hass.states.get(connected).state == STATE_ON
    state = hass.states.get(media)
    assert state.state == "playing"
    assert state.attributes["media_title"] == "Song"
    assert state.attributes["source"] == "VLC"
    assert state.attributes["volume_level"] == 0.4
    assert hass.states.get(lock).state == "unlocked"
    assert hass.states.get(cmd).attributes["friendly_name"] == "Fake Desktop Say hi"

    await hass.services.async_call("media_player", "media_pause", {"entity_id": media}, blocking=True)
    await hass.services.async_call("lock", "lock", {"entity_id": lock}, blocking=True)
    await hass.services.async_call("button", "press", {"entity_id": cmd}, blocking=True)
    resp = await hass.services.async_call(
        DOMAIN,
        "send_notification",
        {"config_entry_id": entry.entry_id, "message": "hi", "actions": ["Yes", "No"]},
        blocking=True,
        return_response=True,
    )
    await hass.services.async_call(
        "notify", "send_message", {"entity_id": ids[f"{desktop.device_id}_notify"], "message": "yo"}, blocking=True
    )
    await _until(lambda: desktop.locked)
    await _until(lambda: sum(p.type == "kdeconnect.notification" for p in desktop.received) == 2)
    await hass.async_block_till_done()
    assert hass.states.get(lock).state == "locked"
    bodies = [(p.type, p.body) for p in desktop.received]
    assert ("kdeconnect.mpris.request", {"player": "VLC", "action": "Pause"}) in bodies
    assert ("kdeconnect.runcommand.request", {"key": "k1"}) in bodies

    events = []
    hass.bus.async_listen("kdeconnect_notification_action", events.append)
    desktop.send("kdeconnect.notification.action", {"key": resp["id"], "action": "Yes"})
    await _until(lambda: events)
    assert events[0].data["action"] == "Yes" and events[0].data["key"] == resp["id"]

    seen: list[tuple[bool, str, str, str]] = []

    def record(ev, up):
        # Entity listeners were added first and write state synchronously.
        if ev is Event.CONNECTION:
            seen.append((up, hass.states.get(connected).state, hass.states.get(media).state, hass.states.get(cmd).state))

    dev.add_listener(record)
    desktop.disconnect()
    await _until(lambda: len(seen) >= 2)  # down, then back up via the reconnect loop
    down, up = seen[0], seen[1]
    assert down[0] is False and down[1] == STATE_OFF and down[2] == STATE_UNAVAILABLE
    assert down[3] == STATE_UNAVAILABLE  # command button kept while offline
    assert up[0] is True and up[1] == STATE_ON

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_user_flow_no_devices(hass: HomeAssistant, desktop: FakeDesktop):
    await desktop.stop()
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "127.0.0.1"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_devices"}


async def test_user_flow_pair_rejected(hass: HomeAssistant, desktop: FakeDesktop):
    desktop.accept_pair = False
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await _pair(hass, desktop, result["flow_id"])
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pair_failed"


async def test_unpair_triggers_reauth(hass: HomeAssistant, desktop: FakeDesktop):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    entry = (await _pair(hass, desktop, result["flow_id"]))["result"]
    await hass.async_block_till_done()
    dev = entry.runtime_data
    await _until(lambda: dev.connected)

    desktop.send("kdeconnect.pair", {"pair": False})
    await _until(lambda: not dev.paired)
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == ["reauth"]

    result = await hass.config_entries.flow.async_configure(flows[0]["flow_id"], {})
    while result["type"] is FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(flows[0]["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert len(desktop.pair_requests) == 2
    await _until(lambda: entry.runtime_data.connected)
