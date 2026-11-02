"""Process-wide KDE Connect node shared by all config entries and flows."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components import network
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STORAGE_KEY_IDENTITY, STORAGE_VERSION
from .protocol.certs import LocalIdentity, generate_identity
from .protocol.node import KdeConnectNode
from .protocol.packets import filter_name

_LOGGER = logging.getLogger(__name__)

_LOCK = f"{DOMAIN}_node_lock"


async def _load_identity(hass: HomeAssistant) -> LocalIdentity:
    # private=True writes .storage/kdeconnect.identity with 0600 permissions.
    store: Store[dict[str, str]] = Store(hass, STORAGE_VERSION, STORAGE_KEY_IDENTITY, private=True, atomic_writes=True)
    if (data := await store.async_load()) is not None:
        return LocalIdentity.from_dict(data)
    identity = await hass.async_add_executor_job(generate_identity)
    await store.async_save(identity.as_dict())
    _LOGGER.info("Generated KDE Connect identity %s", identity.device_id)
    return identity


async def async_get_node(hass: HomeAssistant) -> KdeConnectNode:
    """Return the running node, starting it on first use."""
    lock: asyncio.Lock = hass.data.setdefault(_LOCK, asyncio.Lock())
    async with lock:
        if (node := hass.data.get(DOMAIN)) is not None:
            return node

        identity = await _load_identity(hass)
        broadcasts = [str(a) for a in await network.async_get_ipv4_broadcast_addresses(hass)]
        name = filter_name(hass.config.location_name or "Home Assistant") or "Home Assistant"
        node = KdeConnectNode(identity, name, "desktop", broadcast_addresses=broadcasts or ["255.255.255.255"])
        await node.start()
        hass.data[DOMAIN] = node

        async def _stop(_: Event) -> None:
            await async_stop_node(hass)

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop)
        return node


async def async_stop_node(hass: HomeAssistant) -> None:
    if (node := hass.data.pop(DOMAIN, None)) is not None:
        await node.stop()
