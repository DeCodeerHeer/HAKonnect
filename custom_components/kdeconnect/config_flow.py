"""Config flow: discover a KDE Connect device, then pair with it."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from ipaddress import ip_network
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.helpers.selector import SelectOptionDict, SelectSelector, SelectSelectorConfig
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import CONF_CERT, CONF_DEVICE_ID, DOMAIN
from .hub import async_get_node
from .protocol.certs import der_to_pem, verification_key
from .protocol.const import PROTOCOL_VERSION
from .protocol.device import PairingError, RemoteDevice
from .protocol.node import Candidate, KdeConnectNode
from .protocol.packets import is_valid_device_id

_LOGGER = logging.getLogger(__name__)

DISCOVERY_TIMEOUT = 4.0
_CGNAT = ip_network("100.64.0.0/10")
CONNECT_TIMEOUT = 15.0


def _pick_address(info: ZeroconfServiceInfo) -> str:
    """Prefer a private IPv4 address; VPN/IPv6 may not be reachable from HA."""
    v4 = [a for a in info.ip_addresses if a.version == 4 and not a.is_link_local]
    for addr in v4:
        if addr.is_private and addr not in _CGNAT:  # 100.64/10 is Tailscale & co.
            return str(addr)
    return str(v4[0] if v4 else info.ip_address)


class KdeConnectConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._node: KdeConnectNode | None = None
        self._release: Callable[[], None] | None = None
        self._candidates: dict[str, Candidate] = {}
        self._device_id: str | None = None
        self._name: str | None = None
        self._host: str | None = None
        self._device: RemoteDevice | None = None
        self._code = ""
        self._cert_pem: str | None = None
        self._connect_task: asyncio.Task[RemoteDevice] | None = None
        self._pair_task: asyncio.Task[bytes] | None = None

    async def _async_hold_node(self) -> KdeConnectNode:
        """Start the node and keep unpaired peers' links open for this flow."""
        if self._node is None:
            self._node = await async_get_node(self.hass)
            self._release = self._node.begin_discovery()
        return self._node

    def async_remove(self) -> None:
        for task in (self._connect_task, self._pair_task):
            if task and not task.done():
                task.cancel()
        if self._release:
            self._release()

    # -- entry points ------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            node = await self._async_hold_node()
            host = (user_input.get(CONF_HOST) or "").strip() or None
            found = await node.discover(DISCOVERY_TIMEOUT, hosts=[host] if host else [])
            configured = self._async_current_ids(include_ignore=False)
            self._candidates = {
                c.device_id: c for c in found if c.device_id not in configured and (host is None or c.host == host)
            }
            if len(self._candidates) == 1:
                return await self._async_select(next(iter(self._candidates.values())))
            if self._candidates:
                return await self.async_step_pick()
            errors["base"] = "no_devices"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Optional(CONF_HOST): str}),
            errors=errors,
        )

    async def async_step_pick(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self._async_select(self._candidates[user_input[CONF_DEVICE_ID]])
        options = [
            SelectOptionDict(value=c.device_id, label=f"{c.name} ({c.host})") for c in self._candidates.values()
        ]
        return self.async_show_form(
            step_id="pick",
            data_schema=vol.Schema({vol.Required(CONF_DEVICE_ID): SelectSelector(SelectSelectorConfig(options=options))}),
        )

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        props = discovery_info.properties
        device_id = props.get("id")
        if not is_valid_device_id(device_id):
            return self.async_abort(reason="not_supported")
        try:
            if int(props.get("protocol", 0)) < PROTOCOL_VERSION:
                return self.async_abort(reason="not_supported")
        except ValueError:
            return self.async_abort(reason="not_supported")
        # Configured devices are left alone: the node tracks the address the
        # desktop actually connects from. The desktop advertises every interface
        # (LAN, IPv6, VPN) and ip_address rotates between them, so updating the
        # entry here reloaded it on every mDNS refresh.
        await self.async_set_unique_id(device_id)
        self._abort_if_unique_id_configured()
        node = await self._async_hold_node()
        if device_id == node.device_id:
            return self.async_abort(reason="not_supported")

        host = _pick_address(discovery_info)
        self._device_id, self._host = device_id, host
        self._name = props.get("name") or device_id
        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_connect()
        self._set_confirm_only()
        return self.async_show_form(step_id="confirm", description_placeholders={"name": self._name or ""})

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._device_id = entry_data[CONF_DEVICE_ID]
        self._name = entry_data[CONF_NAME]
        self._host = entry_data.get(CONF_HOST)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            await self._async_hold_node()
            return await self.async_step_connect()
        return self.async_show_form(step_id="reauth_confirm", description_placeholders={"name": self._name or ""})

    async def _async_select(self, candidate: Candidate) -> ConfigFlowResult:
        await self.async_set_unique_id(candidate.device_id)
        self._abort_if_unique_id_configured(updates={CONF_HOST: candidate.host})
        self._device_id, self._name, self._host = candidate.device_id, candidate.name, candidate.host
        return await self.async_step_connect()

    # -- connect + pair ----------------------------------------------------

    async def async_step_connect(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        node = await self._async_hold_node()
        assert self._device_id is not None
        if self._connect_task is None:
            self._connect_task = self.hass.async_create_task(
                node.wait_for_candidate(self._device_id, self._host, CONNECT_TIMEOUT)
            )
        if not self._connect_task.done():
            return self.async_show_progress(
                step_id="connect",
                progress_action="connect",
                progress_task=self._connect_task,
                description_placeholders={"name": self._name or ""},
            )
        try:
            self._device = self._connect_task.result()
        except (TimeoutError, asyncio.CancelledError):
            return self.async_show_progress_done(next_step_id="connect_failed")
        finally:
            self._connect_task = None
        return self.async_show_progress_done(next_step_id="pair")

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._node is not None and self._device is not None
        dev, node = self._device, self._node
        if self._pair_task is None:

            def on_code(timestamp: int) -> None:
                assert dev.link is not None
                self._code = verification_key(node.identity.cert_der, dev.link.peer_cert_der, timestamp)

            self._pair_task = self.hass.async_create_task(dev.request_pairing(on_code))
            await asyncio.sleep(0)  # let the request go out so the code is known
        if not self._pair_task.done():
            return self.async_show_progress(
                step_id="pair",
                progress_action="pair",
                progress_task=self._pair_task,
                description_placeholders={"name": self._name or "", "code": self._code},
            )
        try:
            self._cert_pem = der_to_pem(self._pair_task.result())
        except (PairingError, asyncio.CancelledError) as err:
            _LOGGER.info("Pairing with %s failed: %s", self._name, err)
            return self.async_show_progress_done(next_step_id="pair_failed")
        finally:
            self._pair_task = None
        return self.async_show_progress_done(next_step_id="finish")

    async def async_step_connect_failed(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_connect()
        return self.async_show_form(step_id="connect_failed", description_placeholders={"name": self._name or ""})

    async def async_step_pair_failed(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_connect()
        return self.async_show_form(step_id="pair_failed", description_placeholders={"name": self._name or ""})

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._device is not None and self._cert_pem is not None
        data = {
            CONF_DEVICE_ID: self._device_id,
            CONF_NAME: self._device.name,
            CONF_HOST: self._device.host,
            CONF_CERT: self._cert_pem,
        }
        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data=data)
        return self.async_create_entry(title=self._device.name, data=data)
