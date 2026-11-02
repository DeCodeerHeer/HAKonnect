"""Standalone CLI harness, no Home Assistant needed.

Run from custom_components/kdeconnect:

    python -m protocol.harness --host 192.168.1.10 pair
    python -m protocol.harness ping "hello"
    python -m protocol.harness status
    python -m protocol.harness listen

State (our key/cert and the pinned peer) lives in
~/.config/hakonnect-harness/state.json with 0600 permissions.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import sys
from pathlib import Path

from .certs import LocalIdentity, der_to_pem, fingerprint, generate_identity, verification_key
from .device import Event, PairingError
from .node import KdeConnectNode

STATE_PATH = Path(os.environ.get("HAKONNECT_STATE", "~/.config/hakonnect-harness/state.json")).expanduser()


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = STATE_PATH.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(STATE_PATH)


def get_identity(state: dict) -> LocalIdentity:
    if "identity" not in state:
        state["identity"] = generate_identity().as_dict()
        save_state(state)
    return LocalIdentity.from_dict(state["identity"])


async def connect_paired(node: KdeConnectNode, state: dict, timeout: float):
    peer = state.get("peer")
    if not peer:
        sys.exit("Not paired yet; run the 'pair' command first.")
    import ssl

    dev = node.add_device(peer["device_id"], peer["name"], ssl.PEM_cert_to_DER_cert(peer["cert_pem"]), peer["host"])
    connected = asyncio.Event()
    dev.add_listener(lambda ev, data: ev is Event.CONNECTION and data and connected.set())
    node.announce(peer["host"])
    try:
        await asyncio.wait_for(connected.wait(), timeout)
    except TimeoutError:
        sys.exit(f"{peer['name']} didn't connect within {timeout}s")
    return dev


async def cmd_pair(node: KdeConnectNode, state: dict, args) -> None:
    print(f"Announcing to {args.host or 'broadcast'} ...")
    async with node.discovering():
        found: asyncio.Future = asyncio.get_running_loop().create_future()

        def on_candidate(c):
            if (not args.host or c.host == args.host) and not found.done():
                found.set_result(c)

        node.add_candidate_listener(on_candidate)
        for _ in range(10):
            node.announce(*([args.host] if args.host else []), broadcast=not args.host)
            try:
                cand = await asyncio.wait_for(asyncio.shield(found), 1.0)
                break
            except TimeoutError:
                continue
        else:
            sys.exit("No KDE Connect device connected back. Check firewall (kde-connect service) and the host.")

        dev = node.candidates[cand.device_id]
        print(f"Connected to {cand.name} ({cand.device_id}, {cand.device_type}) at {cand.host}")

        def show_code(ts: int) -> None:
            code = verification_key(node.identity.cert_der, dev.link.peer_cert_der, ts)
            print(f"Pairing requested. Accept on the desktop; it should show code {code}")

        try:
            cert_der = await dev.request_pairing(show_code)
        except PairingError as err:
            sys.exit(f"Pairing failed: {err}")

    state["peer"] = {
        "device_id": dev.device_id,
        "name": dev.name,
        "host": dev.host,
        "cert_pem": der_to_pem(cert_der),
    }
    save_state(state)
    print(f"Paired. Pinned peer cert sha256={fingerprint(cert_der)}")


async def cmd_ping(node, state, args) -> None:
    dev = await connect_paired(node, state, args.timeout)
    print("ping sent" if dev.ping(args.message) else "send failed")
    await asyncio.sleep(0.5)


async def cmd_notify(node, state, args) -> None:
    dev = await connect_paired(node, state, args.timeout)
    nid = dev.notify(args.title, args.text, actions=args.action or None)
    print(f"notification {nid} sent; waiting 20s for actions" if args.action else f"notification {nid} sent")
    if args.action:
        dev.add_listener(lambda ev, data: ev is Event.NOTIFICATION_ACTION and print("action:", data))
        await asyncio.sleep(20)
    await asyncio.sleep(0.5)


async def cmd_status(node, state, args) -> None:
    dev = await connect_paired(node, state, args.timeout)
    await asyncio.sleep(2.0)  # let the initial state requests come back
    print(f"device:   {dev.name} {dev.device_id} @ {dev.host}")
    print(f"peer caps in:  {sorted(dev.link.identity.incoming)}")
    print(f"peer caps out: {sorted(dev.link.identity.outgoing)}")
    print(f"locked:   {dev.locked}")
    print(f"commands: {json.dumps(dev.commands)}")
    for p in dev.players.values():
        print(f"player:   {p.name!r} playing={p.is_playing} {p.artist} - {p.title} [{p.pos}/{p.length} ms] vol={p.volume}")
    for s in dev.sinks:
        print(f"sink:     {s.get('name')} {s.get('description')!r} vol={s.get('volume')}/{s.get('maxVolume')} muted={s.get('muted')}")


async def cmd_media(node, state, args) -> None:
    dev = await connect_paired(node, state, args.timeout)
    await asyncio.sleep(1.0)
    player = args.player or next(iter(dev.players), None)
    if player is None:
        sys.exit("no MPRIS players on the desktop")
    print(f"{args.action} -> {player}")
    dev.media_action(player, args.action)
    await asyncio.sleep(1.0)


async def cmd_run(node, state, args) -> None:
    dev = await connect_paired(node, state, args.timeout)
    await asyncio.sleep(1.0)
    if args.key not in dev.commands:
        sys.exit(f"unknown command key; available: {json.dumps(dev.commands)}")
    dev.add_listener(lambda ev, data: ev is Event.COMMAND_OUTPUT and print("output:", data))
    dev.run_command(args.key)
    await asyncio.sleep(2.0)


async def cmd_lock(node, state, args) -> None:
    dev = await connect_paired(node, state, args.timeout)
    dev.set_locked(args.state == "on")
    await asyncio.sleep(1.5)
    print(f"locked: {dev.locked}")


async def cmd_listen(node, state, args) -> None:
    dev = await connect_paired(node, state, args.timeout)

    def on_event(ev, data):
        extra = ""
        if ev is Event.PLAYER and data in dev.players:
            p = dev.players[data]
            extra = f" playing={p.is_playing} {p.artist} - {p.title} pos={p.pos}"
        print(f"[{ev}] {data!r}{extra}", flush=True)

    dev.add_listener(on_event)
    print("listening; Ctrl-C to stop (reconnects automatically)")
    await asyncio.Event().wait()


async def main_async(args) -> None:
    state = load_state()
    identity = get_identity(state)
    node = KdeConnectNode(identity, args.name, listen_udp=args.listen_udp)
    await node.start()
    print(f"local id {identity.device_id}, TCP {node.tcp_port}")
    try:
        await args.func(node, state, args)
    finally:
        await node.stop()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m protocol.harness")
    parser.add_argument("--host", help="desktop IP (default: broadcast when pairing)")
    parser.add_argument("--name", default=f"HA harness {socket.gethostname()}"[:32].replace(".", ""))
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--listen-udp", action="store_true", help="also listen on UDP 1716 (not when kdeconnectd runs locally)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(required=True)

    sub.add_parser("pair").set_defaults(func=cmd_pair)
    p = sub.add_parser("ping")
    p.add_argument("message", nargs="?")
    p.set_defaults(func=cmd_ping)
    p = sub.add_parser("notify")
    p.add_argument("title")
    p.add_argument("text")
    p.add_argument("--action", action="append")
    p.set_defaults(func=cmd_notify)
    sub.add_parser("status").set_defaults(func=cmd_status)
    p = sub.add_parser("media")
    p.add_argument("action", choices=["Play", "Pause", "PlayPause", "Stop", "Next", "Previous"])
    p.add_argument("--player")
    p.set_defaults(func=cmd_media)
    p = sub.add_parser("run")
    p.add_argument("key")
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("lock")
    p.add_argument("state", choices=["on", "off"])
    p.set_defaults(func=cmd_lock)
    sub.add_parser("listen").set_defaults(func=cmd_listen)

    args = parser.parse_args()
    sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    state = load_state()
    if args.host is None and "peer" in state and args.func is not cmd_pair:
        args.host = state["peer"]["host"]
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
