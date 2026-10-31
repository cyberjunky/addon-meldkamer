# Changelog

## Unreleased

- Places get their position (the town's centre, from PDOK) from "Import All Places" and the Places/Streets "Import BAG". Before, both imports left places without coordinates, so a message without a street (or with a street that couldn't be placed) only got a map pin when you had entered the place's position yourself, and `only_sensor_matches` couldn't tell whether such a message was near a zone. **Run "Import All Places" once** to fill in all places.
- Street positions from the BAG import: the Streets import now also stores each street's position (its centre, from PDOK), and messages with a known street are placed on it without an OpenCage request. OpenCage is only used for streets that aren't in the table, so street-level map pins work without an OpenCage account too. **Run the Streets "Import BAG" again for your provinces** to add positions to streets imported before; your existing streets and edits are kept (#26).
- Streets have `latitude` and `longitude`: shown on the Streets page, editable in the add/edit form, and in CSV export and import (optional columns; an import with coordinates also fills them in for streets you already have). Coordinates outside the Netherlands are rejected.
- New `geocoding.only_sensor_matches` option (default off): only call OpenCage for messages that match a sensor, so a receiver hearing the whole country doesn't use up the free tier on messages you don't follow. Other messages use cached coordinates or the city-level position from the Places table. Zone filters also count messages up to 2 km outside the zone, as the street may lie inside it. The Info tab shows how many messages skipped OpenCage (#27).
- "km from home" is now also left out for a message with a street that couldn't be geocoded (it was placed at the city centre).
- Phone layout: on screens up to 700 px wide the top bar wraps into two rows (view tabs; receiver status, messages per hour, sensor filter and the sound/speech/map/theme toggles), so the sensor filter and toggles are no longer pushed off-screen. Settings and Info show as icons; the clock is hidden (#25).

## 1.2.8

- "km from home" is only shown for messages with a street. A message naming just a city is placed at the city centre, so its distance was that of the centre (e.g. "3.9 km from home" for any Amsterdam message). Also for the `distance_km` attribute, and for messages already in the history.

## 1.2.7

- Support and Donate cards on the Info tab: documentation, reporting a problem, the version and changelog, and donating via PayPal or GitHub Sponsors.
- Removed the ppm calibration from the Settings tab. Set `ppm_correction` (and `backup_ppm_correction`) in the add-on configuration; the Info tab's reception quality shows whether a value decodes well.

## 1.2.6

- New **Vehicles** table (Settings → Database): what each vehicle number is (type and description). A vehicle only gets a type and photo when it's in this table or the message names its type ("TS 07-1782"); the type is no longer derived from the number's digits (1.2.5 did that, and regions don't apply the numbering plan consistently). Filled automatically from the capcode list (descriptions such as "TS 01-2632" or "Ambulance-07-101") and from messages that name a vehicle with its type; add, edit, export or import (CSV) your own.
- Vehicle numbers are now found in fire brigade messages: the alerted units at the end (e.g. "… Scheemda 013693 012933" → 01-3693, 01-2933). The message detail lists all units; the first one known in the Vehicles table is shown with its type and photo.
- Messages from history show their vehicles too (they weren't stored, so they only showed on live messages); this works for all existing messages.

## 1.2.5

- Real photos for vehicles without an uploaded photo: tankautospuit, autoladder, hulpverlening, haakarm, schuimblus, waterongevallen, officier van dienst, dienstbus (also used for gevaarlijke stoffen units), ambulance, traumahelikopter, KNRM reddingboot and politie. Freely licensed photos from Wikimedia Commons, cut out (transparent background) with a "MELDKAMER" licence plate, credited under the photo (see `src/vehicle_images/CREDITS.md`).
- Fire brigade numbers pick the photo from their type digit (Nationaal Nummerplan Brandweer: RR-KKTV, e.g. 11-0293 is type 9, an OvD car), so the right vehicle shows even when the message's abbreviation says otherwise.
- Your own uploaded photos (per vehicle number or category) still come first.

## 1.2.4

- The map keeps your zoom level: focusing a new incident moves the map without zooming in or out, and the last map position and zoom are restored after a reload.

## 1.2.3

- The sensor filter uses exactly the same style as the view tabs.
- Tab and filter labels are centred exactly.
- The sound, speech, auto-focus and theme buttons form one group in the tab style; enabled ones are highlighted.
- On screens up to 1400 px wide the sensor filter is a dropdown instead of a row of buttons.
- The live feed no longer freezes when the connection is silently dropped (e.g. overnight): the add-on sends a heartbeat and the dashboard reconnects by itself when it stops hearing from it. A rejected reconnect (shown as a lasting "Reconnecting…") is retried after 5 seconds.
- Loading the message list retries before showing "Failed to load messages", which now includes the reason.
- Fits smaller screens (1280–1400 px wide, up to 800 px high): compact top bar, slimmer Console side column, shorter Wallboard ticker.

## 1.2.2

- The sensor filter in the top bar is a labelled, outlined control like the view tabs, so it stands out in every view.
- Bigger Wallboard ticker (taller, 15 px message text over up to three lines) and a bigger clock.
- Removed the unused `/config` and `/share` folder mappings (Supervisor warned about the deprecated `config` mapping); `/ssl` stays for MQTT TLS certificates.

## 1.2.1

- Tabs: **Settings** (was Database; now also holds the ppm calibration) and **Info** (was Advanced), in that order after the views.
- Message detail always shows the vehicle photo slot, the date and distance from home, an "escalated" badge on the incident, the raw message, and Previous/Next to step through messages.
- Wallboard and Lanes show the detail floating in the map's corner instead of a centred popup; dialogs no longer dim the page.
- Draggable dividers between the panes of Console, Wallboard and Lanes (double-click resets).
- Console's map and detail column grows with the screen; lanes are capped so the map gets the rest.
- Messages with an A/B urgency code that also reach fire brigade capcodes go in the Ambulance lane.
- Requires Home Assistant 2026.9 or newer (map tiles always come through its map tile proxy).
- Fix: the built-in abbreviation dictionary is seeded on startup again.
- Settings and Info pages use the dashboard's look: flat panels, compact buttons, mono values, no emoji headings, and larger, more readable text.
- New screenshots.

## 1.2.0

- New compact dashboard with three views, switched with tabs in the top bar:
  - **Console**: a dense message table, with the map and the full message detail (vehicle photo, incident timeline, earlier messages at the address) docked on the right.
  - **Wallboard**: a large map with live counts, incidents sorted by priority, and a ticker of the newest messages.
  - **Lanes**: one column each for Brandweer, Ambulance, Politie and other disciplines, next to the map.
- Wallboard and Lanes open the message detail in a popup; Console shows it docked.
- The theme follows Home Assistant's light/dark mode, with a button to force light or dark.
- Fits one screen: receiver status, messages per hour, frames per minute and open incidents in the top bar; decoder and device status in a status bar.

## 1.1.1

- Prebuilt images: the add-on is now pulled from `ghcr.io/cyberjunky/addon-meldkamer` instead of being compiled on your system, so installing and updating take a minute instead of a long build.
- Based on Alpine 3.23 (Home Assistant's multi-arch base image).
- Only 64-bit systems (`amd64`, `aarch64`) are supported, the architectures Home Assistant's image builder supports.

## 1.1.0

### Breaking changes

- Sensor location filters now use Home Assistant zones: `center_lat`, `center_lon` and `radius_km` are replaced by `zone` (e.g. `zone: zone.home`), which supplies both the centre and the radius. Create a zone in Home Assistant for any other area you filtered on.
- The `distance` message attribute is replaced by `distance_km` (distance from `zone.home`). Sensor attributes still carry `distance`, now the distance to the centre of that sensor's zone.
- Sensor states are the spoken (TTS) text of the message; `body` holds the original.

### New

- `meldkamer_message` event for every message and `meldkamer_incident_escalated` when an incident escalates (`ha_events`).
- Receiver health sensors: `binary_sensor.meldkamer_receiver_online` and `sensor.meldkamer_messages_per_hour` (REST and MQTT).
- Announce messages on Home Assistant speakers with `tts.speak` (`announce` options, `announce: true` per sensor).
- Messages to the same address are grouped into incidents on the dashboard (`incident_window_minutes`).
- Repeat suppression for messages the pager network resends (`repeat_window_seconds`).
- Map history heatmap with a time slider.
- Reception quality on the Advanced tab: FLEX frames per minute, corrected bit errors, failed decodes, lost syncs.
- Automatic ppm calibration on the Advanced tab, saved to the add-on configuration.
- Bias-tee option for RTL-SDR (`receiver.bias_tee`).
- Failover to a backup `rtl_tcp` receiver (`receiver.backup_host`), and an automatic restart when the receiver stops producing frames (`receiver.offline_after_minutes`).
- README example for Home Assistant's Map card.

### Changed

- Browser speech, sensors, MQTT and speakers all use the same server-side TTS text.
- Removed unused POCSAG parsing and environment-variable configuration.
