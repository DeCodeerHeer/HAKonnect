# KDE Connect for Home Assistant

Home Assistant joins your network as a KDE Connect device, the way a phone does. Pair it once with a desktop running KDE Connect, and Home Assistant can control the desktop through KDE Connect's built-in plugins. You don't install anything on the desktop.

## Features

| Entity / action | What it does | Desktop plugin |
|---|---|---|
| `media_player` | Now playing, play/pause/next/previous/seek/volume/shuffle/repeat. Each MPRIS player is a **source**, and the entity follows whichever player is playing. | Multimedia control receiver |
| `notify` | Shows a notification on the desktop | Notification sync |
| `lock` | Locks or unlocks the screen (logind) | Lock device |
| `button` (one per command) | Runs a command you define on the desktop under *KDE Connect → Home Assistant → Run commands* | Run commands |
| `button` Ping / Ring | Sends a ping notification, or rings the desktop | Ping / Find this device |
| `sensor` Battery, `binary_sensor` Charging | Battery level and charging state. Only created after the desktop reports a battery, so laptops get them and desktop PCs don't. | Battery monitor |
| `event` Notification | Fires on every notification the desktop forwards to HA, with `app_name`, `title`, `text` and `ticker`. Shows in history and works as an automation trigger. | Send notifications |
| `binary_sensor` Connectivity | Whether the desktop is connected. kdeconnectd only runs while someone is logged in, so this also works as a presence signal. | – |
| `kdeconnect.send_notification` | Notification with action buttons. A press fires `kdeconnect_notification_action` (`key`, `action`). | Notification sync |
| `kdeconnect.share_url` / `share_text` / `run_command` / `ping` / `cancel_notification` | | Share / Run commands / Ping |

Desktop notifications reach HA only if you enable **Send notifications** for the HA device in the desktop's KDE Connect settings (it's off by default). Enable it while HA is connected: the desktop only reloads a device's plugins while that device is reachable.

Events: `kdeconnect_notification_action`, `kdeconnect_ping`, `kdeconnect_command_output`, and `kdeconnect_notification` (desktop notifications sent to HA, if you enable the desktop's "Send notifications" plugin for the HA device).

## Setup

1. Copy `custom_components/kdeconnect` into your HA config directory, or add this repository to HACS as a custom repository.
2. Allow KDE Connect through the desktop firewall: `sudo firewall-cmd --permanent --add-service=kde-connect && sudo firewall-cmd --reload`.
3. Add the integration under **Settings → Devices & services → Add integration → KDE Connect**. Leave the host empty to search the network, or enter the desktop's IP address.
4. Accept the pairing request on the desktop within 30 seconds, and check that the code it shows matches the one in HA.

Desktops that advertise themselves over mDNS also show up under **Discovered**.

### Network requirements

The desktop opens the connection: HA announces itself over UDP 1716, and the desktop connects back to HA on a TCP port between 1716 and 1764. This means:

* HA must accept incoming TCP 1716–1764. In Docker, use `network_mode: host`.
* Discovery by broadcast needs HA and the desktop in the same broadcast domain. Otherwise, enter the desktop's IP address. HA remembers the last address it saw and announces to it directly.

## Security

* HA generates its own EC P-256 key and self-signed certificate on first use and stores them in `.storage/kdeconnect.identity` (mode 0600). Nothing goes into `configuration.yaml`, and key material is never logged.
* After pairing, the desktop's certificate is pinned in the config entry. A device presenting a different certificate is rejected and has to be paired again.
* If the desktop unpairs HA, the integration starts a re-authentication flow.

## Development

The protocol layer (`custom_components/kdeconnect/protocol/`) has no Home Assistant dependency. Use the CLI harness to try it against a real desktop:

```sh
cd custom_components/kdeconnect
python -m protocol.harness --host 192.168.1.10 pair
python -m protocol.harness status       # players, commands, lock state, sinks
python -m protocol.harness ping "hello"
python -m protocol.harness notify "Title" "Body" --action Yes --action No
python -m protocol.harness listen       # print pushed events
```

The harness keeps its own identity in `~/.config/hakonnect-harness/state.json`.

Tests run against an in-process fake desktop:

```sh
pip install pytest-homeassistant-custom-component
pytest
```

To debug from the desktop side, restart kdeconnectd with `QT_LOGGING_RULES='kdeconnect.core.debug=true'` and follow `journalctl --user -f -u 'app-org.kde.kdeconnect.daemon@autostart.service'`.
