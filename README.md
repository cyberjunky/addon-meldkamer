[![Donate via PayPal](https://img.shields.io/badge/Donate-PayPal-blue.svg?style=for-the-badge&logo=paypal)](https://www.paypal.me/cyberjunkynl/)
[![Sponsor on GitHub](https://img.shields.io/badge/Sponsor-GitHub-red.svg?style=for-the-badge&logo=github)](https://github.com/sponsors/cyberjunky)

# Meldkamer

P2000 FLEX pager decoder dashboard for Home Assistant — decode Dutch emergency services messages using RTL-SDR, HackRF, or network streaming.

[![Add repository to my Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fcyberjunky%2Faddon-meldkamer)

![Console view: message table, map and message detail with incident timeline](https://raw.githubusercontent.com/cyberjunky/addon-meldkamer/main/screenshots/console-dark.webp)

<details>
<summary>More screenshots (light theme, Wallboard, Lanes, Settings, Info)</summary>

**Console, light theme** — follows Home Assistant's light/dark mode
![Console view in the light theme](https://raw.githubusercontent.com/cyberjunky/addon-meldkamer/main/screenshots/console-light.webp)

**Wallboard** — large map, live counts, incidents by priority and a ticker of the newest messages
![Wallboard view](https://raw.githubusercontent.com/cyberjunky/addon-meldkamer/main/screenshots/wallboard.webp)

**Wallboard with a message selected** — the detail floats in the map's corner
![Wallboard view with the message detail](https://raw.githubusercontent.com/cyberjunky/addon-meldkamer/main/screenshots/wallboard-detail.webp)

**Lanes** — one column per discipline next to the map
![Lanes view](https://raw.githubusercontent.com/cyberjunky/addon-meldkamer/main/screenshots/lanes.webp)

**Settings** — reference data and tables
![Settings tab](https://raw.githubusercontent.com/cyberjunky/addon-meldkamer/main/screenshots/settings.webp)

**Info** — receiver, device, database, reception quality and statistics
![Info tab](https://raw.githubusercontent.com/cyberjunky/addon-meldkamer/main/screenshots/info.webp)

</details>

> The screenshots show made-up messages, capcodes and units.

## Features

- 📡 **SDR Support** — RTL-SDR, HackRF One, SoapySDR devices, or network streaming
- 🗺️ **Interactive Dashboard** — Live map, statistics, tabbed interface
- 🎯 **Sensor Filter Toggle** — Narrow the live dashboard feed to a configured sensor's criteria, or view everything
- 📍 **Geocoding** — Automatic address to coordinates (OpenCage), with city-level fallback when disabled
- 🏠 **Home Assistant Sensors** — Auto-create sensors with custom filters via REST API or MQTT, including filters on Home Assistant zones
- ⚡ **Home Assistant Events** — `meldkamer_message` for every message and `meldkamer_incident_escalated` when an incident grows, ready to use as automation triggers
- 🩺 **Receiver Health** — `binary_sensor.meldkamer_receiver_online` and `sensor.meldkamer_messages_per_hour`, so a dead SDR stick or `rtl_tcp` link doesn't go unnoticed
- 🚨 **Incidents** — Messages for the same address within a few minutes are grouped into one incident, with an alert when it escalates (GRIP level, priority, more units or disciplines)
- 🔁 **Repeat Suppression** — Messages the pager network resends are counted as repeats instead of being published again
- 🗣️ **Text-to-Speech** — Browser speech and Home Assistant speakers (`tts.speak`) read new messages aloud, using editable TTS replacement rules
- 🗺️ **Map History** — Heatmap of past incidents with a time slider
- 📶 **Reception Quality** — FLEX frames per minute, corrected bit errors and failed decodes
- 🔀 **Receiver Failover** — Switches to a backup `rtl_tcp` receiver when the active one stops producing frames
- 🚑 **Vehicle Classification** — Best-effort vehicle/unit recognition (ambulance, fire, helicopter, police, water rescue) from a Vehicles table (number → type), with stock photos per vehicle type and user-uploadable photos per vehicle or category
- 🔔 **Sound Notifications** — Configurable audio alerts for new messages
- 🔍 **Message Filtering** — Filter by text patterns and capcodes, managed live from the dashboard

## Installation

1. Click the button to add this repository to your Home Assistant:

   [![Add repository to my Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fcyberjunky%2Faddon-meldkamer)

   Or add it by hand: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, and enter `https://github.com/cyberjunky/addon-meldkamer`.
2. Find **Meldkamer** in the Add-on Store and click **Install**.
3. Plug in your SDR receiver, review the **Configuration** tab, and click **Start**.
4. Open **Meldkamer** from the sidebar.

## Network

| Network | Frequency | Protocol |
|---------|-----------|----------|
| P2000 FLEX | 169.65 MHz | FLEX |

## Web Dashboard

Access via Home Assistant sidebar → Meldkamer:

- **Console / Wallboard / Lanes** — Three live views of the same feed, switched with tabs in the top bar:
  - **Console** — dense message table, with the map and the selected message's full detail (vehicle photo, incident timeline, earlier messages at the address) docked on the right
  - **Wallboard** — large map with live counts, incidents sorted by priority and a ticker of the newest messages; for an always-on screen
  - **Lanes** — one column per discipline (Brandweer, Ambulance, Politie, other) next to the map

  The message detail shows the full message, abbreviations, vehicle photo, incident timeline and earlier messages at the address, with Previous/Next to step through the list. Console docks it under the map; on the Wallboard and Lanes it floats in the map's corner. Drag the dividers between the panes to resize them (double-click resets). The sensor filter, sound, speech and auto-focus toggles sit in the top bar. The theme follows Home Assistant's light/dark mode; the 🌓 button switches between automatic, light and dark.
- **Settings** — Card-based data management for every table below
- **Info** — Receiver, device and database status, reception quality and message statistics

### Settings Tab

A fresh install starts with an **empty** database (aside from the built-in P2000 abbreviation dictionary and TTS/ignore-text defaults) — message parsing, badges, geocoding, and map pins all improve as you populate it. Capcodes, Places, and Streets are one-click imports that fetch live, free, public Dutch data over the internet, no API key needed:

- **Capcodes** — "Import Bommel" pulls ~10,000 capcodes from all 25 regions directly from [p2000.bommel.net](https://p2000.bommel.net/)
- **Places** — "Import All Places" / "Import BAG" pull city and street data from the [PDOK Locatieserver](https://www.pdok.nl/) (Dutch government address/place registry)
- **Streets** — "Import BAG" pulls official street names per province, also from PDOK

Each table gets its own card with row counts and Manage/Import actions:

| Table | Description | Import Options |
|-------|-------------|----------------|
| 📟 Capcodes | Emergency service identifiers | CSV, Bommel |
| 🏙️ Places | City/town lookup | CSV, BAG, all-places |
| 🛤️ Streets | Street address geocoding | CSV, BAG |
| 📖 Texts | P2000 abbreviation dictionary | Built-in, CSV |
| 📨 Messages | Received P2000 messages | — (filled automatically) |
| 📍 Geocodes | OpenCage address lookup cache | — (filled automatically) |
| 🗣️ TTS Replacements | Regex rules for speakable text-to-speech | CSV |
| 🙈 Ignore Text | Wildcard patterns to drop matching messages | CSV |
| 🚫 Ignore Capcodes | Capcodes to drop | CSV |
| 📷 Vehicle Photos | Photos you've uploaded per vehicle number or category | — (upload via message detail or +Add) |
| 🚒 Vehicles | What each vehicle number is (type, description) | Capcode list, messages naming a vehicle ("TS 07-1782"), +Add, CSV import |

Click "Manage" on any card to view/edit/delete that table's rows.

## Configuration

### Receiver Settings

| Option | Description |
| ------ | ----------- |
| `receiver.type` | Receiver type: `rtl-sdr`, `hackrf`, `soapysdr`, `network` |
| `receiver.network_host` | IP address for network receiver (e.g., `192.168.1.100`) |
| `receiver.network_port` | Port for network receiver (default: `1234`) |
| `receiver.sdr_gain` | SDR device gain (0-100, for RTL-SDR) |
| `receiver.ppm_correction` | Frequency correction in PPM for crystal oscillator drift (see [Reception Quality](#reception-quality)) |
| `receiver.bias_tee` | Power an active antenna or LNA over the coax (`rtl-sdr` only, e.g. RTL-SDR Blog V3/V4) |
| `receiver.offline_after_minutes` | Minutes without FLEX frames before the receiver counts as offline and the decoder fails over / restarts (default: `10`) |
| `receiver.backup_host` | Optional backup `rtl_tcp` server, used when the active receiver goes quiet |
| `receiver.backup_port` | Port of the backup `rtl_tcp` server (default: `1234`) |
| `receiver.backup_ppm_correction` | PPM correction of the backup receiver's stick |

**Receiver Types:**
- **rtl-sdr** — RTL-SDR dongle, no device probing (uses `rtl_fm`) — default
- **hackrf** — HackRF One (uses `rx_fm` with HackRF driver)
- **soapysdr** — SoapySDR auto-detect (uses `rx_fm`)
- **network** — Connect to remote `rtl_tcp` server

### HackRF Settings

The HackRF One has three separate gain stages:

| Option | Range | Description |
| ------ | ----- | ----------- |
| `hackrf_lna_gain` | 0-40 dB | Low Noise Amplifier gain (steps of 8 dB) |
| `hackrf_vga_gain` | 0-62 dB | Variable Gain Amplifier (steps of 2 dB) |
| `hackrf_amp_enable` | true/false | Enable RF amplifier (+14 dB when enabled) |

**Recommended P2000 Settings:**
```yaml
receiver:
  type: hackrf
  hackrf_lna_gain: 40
  hackrf_vga_gain: 30
  hackrf_amp_enable: true
```

**Tuning Tips:**
- Start with recommended settings and adjust if needed
- Increase VGA gain if signal is weak but clean
- Reduce gains if you see overload/distortion
- Use `ppm_correction` if messages aren't decoding

### Network Receiver Setup

To use a remote RTL-SDR via network:

1. On the remote machine:
   ```bash
   rtl_tcp -a 0.0.0.0 -p 1234
   ```

2. In Meldkamer config:
   ```yaml
   receiver:
     type: network
     network_host: "192.168.1.100"
     network_port: 1234
   ```

### Receiver Failover

With `backup_host` set, the decoder watches for FLEX frames. When the active receiver produces none for `offline_after_minutes` (or its pipeline keeps failing), it switches to the other one, and back again if that one goes quiet too. Without a backup it restarts the active receiver instead. The active receiver is shown on the Info tab.

```yaml
receiver:
  type: rtl-sdr
  backup_host: "192.168.1.100"
  backup_port: 1234
  offline_after_minutes: 10
```

### Reception Quality

The Info tab shows FLEX frames per minute for the last hour, messages per hour, bit errors fixed by error correction per frame, failed decodes and lost synchronisations. Fewer corrected bits and failed decodes mean a cleaner signal, which helps when tuning gain, ppm and the antenna. `rtl_fm` and `rx_fm` don't report an RF signal level, so none is shown.

### Geocoding

| Option | Description |
| ------ | ----------- |
| `geocoding.enabled` | Enable/disable geocoding |
| `geocoding.opencage_token` | OpenCage API key for address lookup |

### Filters

Global message filters are managed live from the dashboard's **Settings** tab (no restart needed) rather than the add-on config:

- **Ignore Text** — wildcard patterns (e.g. `*TESTOPROEP*`); a message is dropped if its body matches any enabled pattern. Seeded by default with `*TESTOPROEP*` and `*MOB*` (system test calls and mobilofoon checks).
- **Ignore Capcodes** — exact capcodes to drop; a message is only dropped when **every** capcode on it is on this list (so a single ignored capcode can't kill a multi-capcode group call).

Both support add/edit/delete/enable-toggle/export from their own "Manage" page, the same way TTS Replacements does.

### Messages

| Option | Description |
| ------ | ----------- |
| `repeat_window_seconds` | An identical message received again within this many seconds counts as a repeat (shown as 🔁 on the dashboard) instead of a new message. `0` disables it (default: `60`) |
| `incident_window_minutes` | Messages for the same street and city within this many minutes of each other form one incident (default: `15`) |

On the dashboard, an incident's messages are shown as one card (🚨 with the message count); its details list every message. When a new message arrives for an incident on screen, a toast alerts you.

### Sensor Integration

Meldkamer can create Home Assistant sensors that automatically update when P2000 messages match your filters. Sensors are published directly via the Home Assistant REST API — **no MQTT broker or credentials needed!**

An optional MQTT publishing path (see [MQTT](#mqtt) below) can run alongside the REST API sensors.

1. Enable `Sensors Enabled` in the app configuration
2. Define sensors in the `Sensors` section
3. Sensors appear automatically in Home Assistant on startup

```yaml
sensors_enabled: true

sensors:
  # All messages in your area
  - name: "Omgeving"
    icon: "mdi:radio-tower"

  # Fire department in specific cities
  - name: "Brandweer Oegstgeest"
    disciplines: "Brandweer"
    cities: "Oegstgeest, Leiden"

  # Ambulance calls inside your home zone, announced on your speakers
  - name: "Ambulance Nearby"
    disciplines: "Ambulance, Ambulancezorg"
    zone: zone.home
    announce: true

  # Specific station capcodes
  - name: "My Fire Station"
    capcodes: "002029583, 001520021"

  # Urgent incidents inside a zone you created in Home Assistant
  - name: "Schiphol Airport"
    icon: "mdi:airplane"
    zone: zone.schiphol
    priorities: "P1, A1"

  # High priority alerts
  - name: "Priority 1 Alerts"
    priorities: "P1, A1"
```

> [!NOTE]
> Entity IDs are auto-generated from sensor names: lowercase ASCII letters, digits and underscores (accents are transliterated). Example: "Ambulance Nearby" → `sensor.ambulance_nearby`, "Café IJsselmonde" → `sensor.cafe_ijsselmonde`. A sensor whose name yields no letters/digits, or whose ID duplicates another sensor's, is skipped with an error in the log.

> [!IMPORTANT]
> All filter fields use **comma-separated strings** (e.g., `"Ambulance, Brandweer"`). Leave fields empty if not needed.

### Sensor Filter Types

All filters within a sensor use **AND** logic — message must match ALL filters.

| Filter | Type | Description | Example |
|--------|------|-------------|---------|
| `disciplines` | string | Comma-separated disciplines | `"Ambulance, Brandweer"` |
| `cities` | string | Comma-separated cities (case-insensitive) | `"Oegstgeest, Leiden"` |
| `capcodes` | string | Comma-separated capcodes (leading zeros optional) | `"002029583, 001520021"` |
| `priorities` | string | Comma-separated priorities: `P1`/`A1` = 1, `P2`/`A2` = 2, `P3`/`B1`–`B3` = 3, `P4` = 4 (a plain number works too). For GRIP use `text_contains: "GRIP"` | `"P1, A1"` |
| `zone` | string | Only messages inside this Home Assistant zone (its location and radius). Create zones under Settings → Areas, labels & zones → Zones | `zone.home` |
| `text_contains` | string | Keyword search in message body | `"woningbrand"` |
| `icon` | string | MDI icon override | `"mdi:fire-truck"` |
| `announce` | bool | Speak matching messages on Home Assistant speakers (see [Announcements](#announcements)) | `true` |

### Sensor State & Attributes

**State**: The spoken form of the message (TTS replacement rules applied), truncated to 255 characters (Home Assistant's state limit); the full text is in the `tts` and `body` attributes
```
Ambulance zonder spoed HA Doude v Troostwijk/Goeskens Frankenslag Den Haag
```

**Attributes** (available in automations):
```yaml
friendly_name: "Ambulance Nearby"
icon: "mdi:radio-tower"
timestamp: "2026-02-06T14:16:57"
priority: "A2"
discipline: "Ambulance"
region: "Haaglanden"
capcodes: ["002029583", "001520021"]
address: "Frankenslag, 2582 's-Gravenhage, Netherlands"
street: "Frankenslag"
city: "'s-Gravenhage"
postalcode: "2582"
latitude: 52.0973458
longitude: 4.2776374
location_accuracy: "street"
distance_km: 3.42        # from Home Assistant's home zone
distance: 1.27           # from the centre of this sensor's zone
incident_id: "3f9c2a1b7d4e"
tts: "Ambulance zonder spoed HA Doude v Troostwijk/Goeskens Frankenslag Den Haag"
```

### Receiver Health Sensors

With `sensors_enabled`, Meldkamer also publishes (every minute, and over MQTT with discovery when MQTT is enabled):

| Entity | Description |
|--------|-------------|
| `binary_sensor.meldkamer_receiver_online` | `on` while FLEX frames arrive, `off` after `receiver.offline_after_minutes` without any |
| `sensor.meldkamer_messages_per_hour` | Messages decoded in the last hour |

### Home Assistant Events

With `ha_events: true` (default) Meldkamer fires on the Home Assistant event bus:

- **`meldkamer_message`** for every new message, with all message fields (`body`, `tts`, `priority`, `discipline`, `capcodes`, `region`, `city`, `street`, `address`, `latitude`, `longitude`, `distance_km`, `incident_id`, …) plus `sensors`, the names of the sensors it matched.
- **`meldkamer_incident_escalated`** when a later message for an incident raises the GRIP level or priority, or alerts more units or disciplines: `incident_id`, `address`, `disciplines`, `units`, `priority`, `grip`, `message_count`, `reasons` and the new message's `body`.

```yaml
automation:
  - alias: "P2000 GRIP nearby"
    triggers:
      - trigger: event
        event_type: meldkamer_incident_escalated
    conditions:
      - condition: template
        value_template: "{{ trigger.event.data.grip >= 1 }}"
    actions:
      - action: notify.mobile_app_phone
        data:
          title: "GRIP {{ trigger.event.data.grip }}"
          message: "{{ trigger.event.data.address }}: {{ trigger.event.data.reasons | join(', ') }}"
```

```yaml
automation:
  - alias: "P2000 P1 within 5 km"
    triggers:
      - trigger: event
        event_type: meldkamer_message
    conditions:
      - condition: template
        value_template: >
          {{ trigger.event.data.priority == 1 and trigger.event.data.distance_km is not none
             and trigger.event.data.distance_km < 5 }}
    actions:
      - action: notify.mobile_app_phone
        data:
          message: "{{ trigger.event.data.tts }}"
```

### Announcements

Speak messages on Home Assistant speakers with `tts.speak`, using the same TTS replacement rules as the dashboard. Only messages matching a sensor with `announce: true` are spoken.

```yaml
announce:
  tts_entity: tts.google_translate_nl_nl
  media_players: "media_player.kitchen, media_player.living_room"
  language: nl
sensors:
  - name: "Thuis"
    zone: zone.home
    announce: true
```

### Map Card

The sensors carry `latitude`/`longitude` attributes, so Home Assistant's standard Map card shows where their latest message was:

```yaml
type: map
title: P2000
hours_to_show: 6
entities:
  - entity: sensor.thuis
  - entity: sensor.brandweer
  - entity: zone.home
```

### MQTT

An optional MQTT publishing path can be enabled alongside (or instead of) the REST API sensors above. It reuses the same `sensors` list/filters.

```yaml
sensors_enabled: true
sensors:
  - name: "P2000 Brandweer"
    disciplines: "Brandweer"
    # Optional: a stable identifier for this sensor's MQTT topics/unique_id.
    # Falls back to the auto-generated entity_id if not set.
    mqtt_id: "2005"

mqtt:
  enabled: true
  host: ""              # leave empty to auto-discover the Mosquitto add-on via Supervisor
  port: 1883
  user: ""
  password: ""
  client_id: p2000_rtlsdr
  base_topic: p2000_rtlsdr
  ha_autodiscovery: true
  ha_autodiscovery_topic: homeassistant
  retain: false
  tls_enabled: false
```

| Option | Description |
| ------ | ----------- |
| `mqtt.enabled` | Enable MQTT publishing (additive to REST API sensors) |
| `mqtt.host` / `mqtt.port` | Broker address. Leave `host` empty to auto-discover the Home Assistant Mosquitto add-on |
| `mqtt.user` / `mqtt.password` | Broker credentials, if required |
| `mqtt.client_id` | MQTT client id |
| `mqtt.base_topic` | Availability topic prefix (`{base_topic}/status`) |
| `mqtt.ha_autodiscovery` | Publish Home Assistant MQTT discovery config for each sensor |
| `mqtt.retain` | Retain state/attribute messages on the broker |
| `mqtt.tls_enabled` | Enable TLS to the broker (see `tls_ca`, `tls_cert`, `tls_keyfile`, `tls_insecure`). Put your own certificate files in Home Assistant's `/ssl` folder and use those paths, e.g. `/ssl/ca.crt` |

> [!NOTE]
> State/attribute/discovery topics are published under `homeassistant/sensor/p2000_rtlsdr/...` (this prefix is fixed, independent of `base_topic`). The sensor's `mqtt_id` (or its auto-generated `entity_id` if not set) forms the last topic segment and the discovery `unique_id`.

### Automation Examples

#### Notification for nearby ambulance calls

```yaml
automation:
  - alias: "P2000 Ambulance Alert"
    triggers:
      - trigger: state
        entity_id: sensor.ambulance_nearby
    conditions:
      - condition: template
        value_template: "{{ trigger.to_state.state != trigger.from_state.state }}"
    actions:
      - action: notify.mobile_app
        data:
          title: "🚑 Ambulance Alert"
          message: "{{ trigger.to_state.state }}"
```

#### Persistent notification with address details

```yaml
automation:
  - alias: "P2000 Station Alert"
    triggers:
      - trigger: state
        entity_id: sensor.my_fire_station
    actions:
      - action: persistent_notification.create
        data:
          title: "🚒 Station Called Out"
          message: >
            **{{ trigger.to_state.attributes.discipline }}** — {{ trigger.to_state.attributes.priority }}
            📍 {{ trigger.to_state.attributes.address }}
            {{ trigger.to_state.state }}
```

### Other Settings

| Option | Description |
| ------ | ----------- |
| `log_level` | Logging verbosity: `debug`, `info`, `warning`, `error` |

## Ports

| Port | Purpose |
|------|---------|
| 5000 | TCP — Integration connection |
| 8099 | HTTP — Web dashboard (ingress) |

## Requirements

- Home Assistant with Supervisor, on a 64-bit system (`amd64` or `aarch64`, e.g. a Raspberry Pi 4/5 running 64-bit Home Assistant OS)
- RTL-SDR, HackRF One, or network access to rtl_tcp server
- For zone filters, `distance_km`, events, announcements and health sensors: the add-on's Home Assistant API access (enabled by default)
- Home Assistant 2026.9 or newer: the dashboard map loads its OpenStreetMap tiles through Home Assistant's built-in map tile proxy

## Credits

- [multimon-ng](https://github.com/EliasOenal/multimon-ng) — FLEX decoder
- [rtl-sdr](https://osmocom.org/projects/rtl-sdr) — RTL-SDR drivers
- [SoapySDR](https://github.com/pothosware/SoapySDR) — SDR abstraction layer
- [Leaflet](https://leafletjs.com/) — Interactive maps
- [OpenStreetMap](https://www.openstreetmap.org/) — Map tiles
- [Bommel](https://p2000.bommel.net/) — P2000 Capcode Database
- [BAG](https://bag.basisregistraties.nl/) — Basisregistratie Adressen en Gebouwen
- [PDOK Locatieserver](https://www.pdok.nl/) — Dutch address/place lookup for BAG imports

## Support This Project

If you find this add-on useful, please consider supporting its development:

[![Donate via PayPal](https://img.shields.io/badge/Donate-PayPal-blue.svg?style=for-the-badge&logo=paypal)](https://www.paypal.me/cyberjunkynl/)
[![Sponsor on GitHub](https://img.shields.io/badge/Sponsor-GitHub-red.svg?style=for-the-badge&logo=github)](https://github.com/sponsors/cyberjunky)

<a href="https://discord.gg/hUBQxRhJCU">
  <img src="https://raw.githubusercontent.com/cyberjunky/cyberjunky/main/assets/junkyard-discord.svg"
       alt="Join The Junkyard on Discord" width="600">
</a>

- Star this repository
- [Report issues](https://github.com/cyberjunky/addon-meldkamer/issues)
- Share with other Home Assistant users
